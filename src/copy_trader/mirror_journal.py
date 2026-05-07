"""Persistent journal for copy-trader.

Two purposes:
1. Audit log of every mirror event (open, modify, close) — for forensic
   review of P&L deltas between source and dest accounts.
2. Mirror-map state persistence — so a bot restart doesn't double-mirror
   already-replicated positions.

SQLite, single-file, no external deps. Schema is forward-compatible —
new columns added with sensible defaults.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS mirror_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,        -- ISO8601 UTC
    event_type      TEXT NOT NULL,        -- OPEN | MODIFY | CLOSE | RECONCILE | ERROR
    src_ticket      INTEGER,
    dest_ticket     INTEGER,
    symbol          TEXT,
    side            TEXT,                 -- BUY | SELL
    volume          REAL,
    price           REAL,
    sl              REAL,
    tp              REAL,
    latency_ms      INTEGER,              -- detect→fill round trip on dest
    success         INTEGER NOT NULL,     -- 1 = success, 0 = error
    error_message   TEXT,
    raw_response    TEXT                  -- broker JSON response (debug)
);

CREATE INDEX IF NOT EXISTS idx_mirror_events_src_ticket ON mirror_events(src_ticket);
CREATE INDEX IF NOT EXISTS idx_mirror_events_ts ON mirror_events(ts);

CREATE TABLE IF NOT EXISTS mirror_map (
    src_ticket   INTEGER PRIMARY KEY,
    dest_ticket  INTEGER NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    volume       REAL NOT NULL,
    src_open_price REAL,
    dest_open_price REAL,
    src_sl       REAL,
    src_tp       REAL,
    opened_at    TEXT NOT NULL,
    last_modified_at TEXT
);

CREATE TABLE IF NOT EXISTS ignored_source_tickets (
    src_ticket   INTEGER PRIMARY KEY,
    seen_at      TEXT NOT NULL,
    reason       TEXT
);
"""


class MirrorJournal:
    """SQLite-backed audit + state store for the copy-trader.

    Synchronous (sqlite3) — copy-trader poll loop is async but each DB
    write is microseconds; the GIL release during sqlite ops is fine here.
    """

    def __init__(self, db_path: str = "data/mirror_journal.db") -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        logger.info("MirrorJournal initialised at %s", db_path)

    # ----- Event log -----

    def log_event(
        self,
        event_type: str,
        success: bool,
        *,
        src_ticket: Optional[int] = None,
        dest_ticket: Optional[int] = None,
        symbol: Optional[str] = None,
        side: Optional[str] = None,
        volume: Optional[float] = None,
        price: Optional[float] = None,
        sl: Optional[float] = None,
        tp: Optional[float] = None,
        latency_ms: Optional[int] = None,
        error_message: Optional[str] = None,
        raw_response: Optional[dict] = None,
    ) -> None:
        self._conn.execute(
            """INSERT INTO mirror_events
               (ts, event_type, src_ticket, dest_ticket, symbol, side, volume,
                price, sl, tp, latency_ms, success, error_message, raw_response)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                datetime.now(timezone.utc).isoformat(),
                event_type, src_ticket, dest_ticket, symbol, side, volume,
                price, sl, tp, latency_ms, 1 if success else 0,
                error_message,
                json.dumps(raw_response) if raw_response is not None else None,
            ),
        )

    def recent_events(self, limit: int = 50) -> list[dict]:
        cur = self._conn.execute(
            "SELECT * FROM mirror_events ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(row) for row in cur.fetchall()]

    # ----- Mirror map -----

    def upsert_mapping(
        self,
        *,
        src_ticket: int,
        dest_ticket: int,
        symbol: str,
        side: str,
        volume: float,
        src_open_price: float,
        dest_open_price: float,
        src_sl: Optional[float],
        src_tp: Optional[float],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """INSERT OR REPLACE INTO mirror_map
               (src_ticket, dest_ticket, symbol, side, volume,
                src_open_price, dest_open_price, src_sl, src_tp,
                opened_at, last_modified_at)
               VALUES (?,?,?,?,?,?,?,?,?,
                       COALESCE((SELECT opened_at FROM mirror_map WHERE src_ticket=?), ?),
                       ?)""",
            (
                src_ticket, dest_ticket, symbol, side, volume,
                src_open_price, dest_open_price, src_sl, src_tp,
                src_ticket, now,
                now,
            ),
        )

    def update_mapping_sl_tp(self, src_ticket: int, sl: Optional[float], tp: Optional[float]) -> None:
        self._conn.execute(
            "UPDATE mirror_map SET src_sl=?, src_tp=?, last_modified_at=? WHERE src_ticket=?",
            (sl, tp, datetime.now(timezone.utc).isoformat(), src_ticket),
        )

    def remove_mapping(self, src_ticket: int) -> None:
        self._conn.execute("DELETE FROM mirror_map WHERE src_ticket=?", (src_ticket,))

    def load_all_mappings(self) -> dict[int, dict]:
        cur = self._conn.execute("SELECT * FROM mirror_map")
        return {row["src_ticket"]: dict(row) for row in cur.fetchall()}

    # ----- Ignored tickets (boot snapshot) -----

    def add_ignored(self, src_ticket: int, reason: str = "boot_snapshot") -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO ignored_source_tickets (src_ticket, seen_at, reason) VALUES (?,?,?)",
            (src_ticket, datetime.now(timezone.utc).isoformat(), reason),
        )

    def is_ignored(self, src_ticket: int) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM ignored_source_tickets WHERE src_ticket=?", (src_ticket,)
        )
        return cur.fetchone() is not None

    def load_ignored(self) -> set[int]:
        cur = self._conn.execute("SELECT src_ticket FROM ignored_source_tickets")
        return {row["src_ticket"] for row in cur.fetchall()}

    # ----- Maintenance -----

    def prune_old_events(self, retention_days: int) -> int:
        cur = self._conn.execute(
            "DELETE FROM mirror_events WHERE ts < datetime('now', '-' || ? || ' days')",
            (retention_days,),
        )
        return cur.rowcount

    def close(self) -> None:
        self._conn.close()
