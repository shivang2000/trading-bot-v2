"""SQLite database for signal and trade tracking.

Stores raw Telegram messages, parsed signals, executed trades, and
channel performance stats. This data is the foundation for the future
learning system that will identify which channels and signals perform best.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    channel_name TEXT,
    message_id INTEGER,
    message_text TEXT,
    has_image BOOLEAN DEFAULT FALSE,
    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS parsed_signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_message_id INTEGER REFERENCES raw_messages(id),
    is_signal BOOLEAN,
    is_amendment BOOLEAN DEFAULT FALSE,
    action TEXT,
    symbol TEXT,
    entry_price REAL,
    stop_loss REAL,
    take_profit REAL,
    parser_confidence REAL,
    parse_model TEXT,
    parsed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER REFERENCES parsed_signals(id),
    channel_id TEXT,
    mt5_ticket INTEGER,
    action TEXT,
    symbol TEXT,
    volume REAL,
    entry_price REAL,
    stop_loss REAL,
    take_profit REAL,
    opened_at TIMESTAMP,
    closed_at TIMESTAMP,
    close_price REAL,
    pnl REAL,
    pnl_pips REAL,
    close_reason TEXT
);

CREATE TABLE IF NOT EXISTS channel_stats (
    channel_id TEXT PRIMARY KEY,
    total_signals INTEGER DEFAULT 0,
    executed_signals INTEGER DEFAULT 0,
    winning_trades INTEGER DEFAULT 0,
    losing_trades INTEGER DEFAULT 0,
    total_pnl REAL DEFAULT 0,
    win_rate REAL DEFAULT 0,
    avg_pnl REAL DEFAULT 0,
    last_updated TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_parsed_signals_raw_msg
    ON parsed_signals(raw_message_id);
CREATE INDEX IF NOT EXISTS idx_raw_messages_channel
    ON raw_messages(channel_id);
CREATE INDEX IF NOT EXISTS idx_trades_channel ON trades(channel_id);
CREATE INDEX IF NOT EXISTS idx_trades_opened ON trades(opened_at);

-- Bot-opened positions (tracks what WE opened, survives restart)
CREATE TABLE IF NOT EXISTS bot_positions (
    mt5_ticket INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    volume REAL,
    open_price REAL,
    stop_loss REAL,
    take_profit REAL,
    opened_at TIMESTAMP,
    source TEXT,
    status TEXT DEFAULT 'open'
);

-- Trailing stop state (survives restart)
CREATE TABLE IF NOT EXISTS trailing_stops (
    mt5_ticket INTEGER PRIMARY KEY,
    current_sl REAL NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Strategy state machines (survives restart)
CREATE TABLE IF NOT EXISTS strategy_state (
    symbol TEXT NOT NULL,
    strategy TEXT NOT NULL,
    state TEXT NOT NULL,
    direction TEXT,
    breakout_level REAL,
    pullback_count INTEGER DEFAULT 0,
    window_candles INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (symbol, strategy)
);

-- Daily counters (survives restart)
CREATE TABLE IF NOT EXISTS daily_state (
    date TEXT PRIMARY KEY,
    trade_count INTEGER DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_bot_positions_status ON bot_positions(status);
"""


class TrackingDB:
    """Async SQLite database for tracking signals and trades."""

    def __init__(self, db_path: str = "data/trading_bot_v2.db") -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open the database and create tables if they don't exist."""
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        logger.info("Tracking database connected: %s", self._db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    # --- Raw Messages ---

    async def store_raw_message(
        self,
        channel_id: str,
        channel_name: str,
        message_id: int,
        message_text: str,
        has_image: bool = False,
    ) -> int:
        """Store a raw Telegram message. Returns the row ID."""
        cursor = await self._db.execute(
            """INSERT INTO raw_messages
               (channel_id, channel_name, message_id, message_text, has_image)
               VALUES (?, ?, ?, ?, ?)""",
            (channel_id, channel_name, message_id, message_text, has_image),
        )
        await self._db.commit()
        return cursor.lastrowid

    # --- Parsed Signals ---

    async def store_parsed_signal(
        self,
        raw_message_id: int,
        is_signal: bool,
        is_amendment: bool = False,
        action: str | None = None,
        symbol: str | None = None,
        entry_price: float | None = None,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        parser_confidence: float | None = None,
        parse_model: str | None = None,
    ) -> int:
        """Store a parsed signal result. Returns the row ID."""
        cursor = await self._db.execute(
            """INSERT INTO parsed_signals
               (raw_message_id, is_signal, is_amendment, action, symbol,
                entry_price, stop_loss, take_profit, parser_confidence, parse_model)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                raw_message_id, is_signal, is_amendment, action, symbol,
                entry_price, stop_loss, take_profit, parser_confidence, parse_model,
            ),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def get_recent_signals(
        self, channel_id: str, minutes: int = 5
    ) -> list[dict]:
        """Get signals from a channel within the last N minutes."""
        cursor = await self._db.execute(
            """SELECT ps.*, rm.channel_id, rm.message_text
               FROM parsed_signals ps
               JOIN raw_messages rm ON ps.raw_message_id = rm.id
               WHERE rm.channel_id = ?
                 AND ps.is_signal = TRUE
                 AND ps.parsed_at >= datetime('now', ?)
               ORDER BY ps.parsed_at DESC""",
            (channel_id, f"-{minutes} minutes"),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # --- Trades ---

    async def store_trade(
        self,
        signal_id: int | None,
        channel_id: str,
        mt5_ticket: int,
        action: str,
        symbol: str,
        volume: float,
        entry_price: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> int:
        """Store a new trade. Returns the row ID."""
        cursor = await self._db.execute(
            """INSERT INTO trades
               (signal_id, channel_id, mt5_ticket, action, symbol,
                volume, entry_price, stop_loss, take_profit, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                signal_id, channel_id, mt5_ticket, action, symbol,
                volume, entry_price, stop_loss, take_profit, datetime.utcnow(),
            ),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def close_trade(
        self,
        trade_id: int | None = None,
        mt5_ticket: int | None = None,
        close_price: float = 0.0,
        pnl: float = 0.0,
        pnl_pips: float = 0.0,
        close_reason: str = "",
    ) -> None:
        """Record a trade closure. Lookup by trade_id or mt5_ticket."""
        if trade_id is not None:
            await self._db.execute(
                """UPDATE trades
                   SET closed_at = ?, close_price = ?, pnl = ?,
                       pnl_pips = ?, close_reason = ?
                   WHERE id = ? AND closed_at IS NULL""",
                (datetime.utcnow(), close_price, pnl, pnl_pips, close_reason, trade_id),
            )
        elif mt5_ticket is not None:
            await self._db.execute(
                """UPDATE trades
                   SET closed_at = ?, close_price = ?, pnl = ?,
                       pnl_pips = ?, close_reason = ?
                   WHERE mt5_ticket = ? AND closed_at IS NULL""",
                (datetime.utcnow(), close_price, pnl, pnl_pips, close_reason, mt5_ticket),
            )
        await self._db.commit()

    async def get_open_trades(self) -> list[dict]:
        """Get all trades that haven't been closed yet."""
        cursor = await self._db.execute(
            "SELECT * FROM trades WHERE closed_at IS NULL"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_trade_by_ticket(self, mt5_ticket: int) -> dict | None:
        """Look up a trade by MT5 ticket number."""
        cursor = await self._db.execute(
            "SELECT * FROM trades WHERE mt5_ticket = ?", (mt5_ticket,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # --- Channel Stats ---

    async def update_channel_stats(self, channel_id: str | None = None) -> None:
        """Recompute channel statistics from trade history.

        If channel_id is None, updates stats for all channels.
        """
        if channel_id is None:
            cursor = await self._db.execute(
                "SELECT DISTINCT channel_id FROM trades WHERE channel_id IS NOT NULL"
            )
            rows = await cursor.fetchall()
            for row in rows:
                await self._update_single_channel_stats(dict(row)["channel_id"])
        else:
            await self._update_single_channel_stats(channel_id)

    async def _update_single_channel_stats(self, channel_id: str) -> None:
        """Recompute stats for a single channel."""
        cursor = await self._db.execute(
            """SELECT
                 COUNT(*) as total,
                 SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                 SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losses,
                 COALESCE(SUM(pnl), 0) as total_pnl,
                 COALESCE(AVG(pnl), 0) as avg_pnl
               FROM trades
               WHERE channel_id = ? AND closed_at IS NOT NULL""",
            (channel_id,),
        )
        row = await cursor.fetchone()
        stats = dict(row)

        total = stats["total"] or 0
        wins = stats["wins"] or 0
        win_rate = (wins / total * 100) if total > 0 else 0

        sig_cursor = await self._db.execute(
            """SELECT COUNT(*) as cnt FROM parsed_signals ps
               JOIN raw_messages rm ON ps.raw_message_id = rm.id
               WHERE rm.channel_id = ? AND ps.is_signal = TRUE""",
            (channel_id,),
        )
        sig_row = await sig_cursor.fetchone()
        total_signals = dict(sig_row)["cnt"]

        await self._db.execute(
            """INSERT INTO channel_stats
                 (channel_id, total_signals, executed_signals,
                  winning_trades, losing_trades, total_pnl,
                  win_rate, avg_pnl, last_updated)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(channel_id) DO UPDATE SET
                 total_signals = excluded.total_signals,
                 executed_signals = excluded.executed_signals,
                 winning_trades = excluded.winning_trades,
                 losing_trades = excluded.losing_trades,
                 total_pnl = excluded.total_pnl,
                 win_rate = excluded.win_rate,
                 avg_pnl = excluded.avg_pnl,
                 last_updated = excluded.last_updated""",
            (
                channel_id, total_signals, total,
                wins, stats["losses"] or 0,
                stats["total_pnl"], win_rate, stats["avg_pnl"],
                datetime.utcnow(),
            ),
        )
        await self._db.commit()

    async def get_channel_stats(
        self, channel_id: str | None = None
    ) -> list[dict]:
        """Get channel performance stats."""
        if channel_id:
            cursor = await self._db.execute(
                "SELECT * FROM channel_stats WHERE channel_id = ?",
                (channel_id,),
            )
        else:
            cursor = await self._db.execute("SELECT * FROM channel_stats")
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # --- Bot Positions (our own trade tracking) ---

    async def save_bot_position(
        self, ticket: int, symbol: str, side: str, volume: float,
        open_price: float, sl: float | None, tp: float | None,
        source: str = "",
    ) -> None:
        """Record a position the bot opened."""
        await self._db.execute(
            """INSERT OR REPLACE INTO bot_positions
               (mt5_ticket, symbol, side, volume, open_price, stop_loss,
                take_profit, opened_at, source, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
            (ticket, symbol, side, volume, open_price, sl, tp,
             datetime.utcnow(), source),
        )
        await self._db.commit()

    async def close_bot_position(
        self, ticket: int, close_price: float = 0.0, pnl: float = 0.0,
        reason: str = "",
    ) -> None:
        """Mark a bot position as closed."""
        await self._db.execute(
            "UPDATE bot_positions SET status = 'closed' WHERE mt5_ticket = ?",
            (ticket,),
        )
        await self._db.commit()
        # Also close in trades table
        await self.close_trade(
            mt5_ticket=ticket, close_price=close_price,
            pnl=pnl, close_reason=reason,
        )

    async def get_open_bot_positions(self) -> list[dict]:
        """Get all positions the bot currently has open."""
        cursor = await self._db.execute(
            "SELECT * FROM bot_positions WHERE status = 'open'"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # --- Trailing Stop Persistence ---

    async def save_trailing_stop(self, ticket: int, sl: float) -> None:
        """Persist a trailing stop level."""
        await self._db.execute(
            """INSERT OR REPLACE INTO trailing_stops (mt5_ticket, current_sl, updated_at)
               VALUES (?, ?, ?)""",
            (ticket, sl, datetime.utcnow()),
        )
        await self._db.commit()

    async def get_trailing_stops(self) -> dict[int, float]:
        """Load all persisted trailing stop levels."""
        cursor = await self._db.execute("SELECT mt5_ticket, current_sl FROM trailing_stops")
        rows = await cursor.fetchall()
        return {row["mt5_ticket"]: row["current_sl"] for row in rows}

    async def delete_trailing_stop(self, ticket: int) -> None:
        """Remove trailing stop when position closes."""
        await self._db.execute(
            "DELETE FROM trailing_stops WHERE mt5_ticket = ?", (ticket,)
        )
        await self._db.commit()

    # --- Strategy State Persistence ---

    async def save_strategy_state(
        self, symbol: str, strategy: str, state: str,
        direction: str = "", breakout_level: float = 0.0,
        pullback_count: int = 0, window_candles: int = 0,
    ) -> None:
        """Persist a strategy's state machine for a symbol."""
        await self._db.execute(
            """INSERT OR REPLACE INTO strategy_state
               (symbol, strategy, state, direction, breakout_level,
                pullback_count, window_candles, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (symbol, strategy, state, direction, breakout_level,
             pullback_count, window_candles, datetime.utcnow()),
        )
        await self._db.commit()

    async def get_strategy_states(self, strategy: str) -> dict[str, dict]:
        """Load all persisted states for a strategy. Returns {symbol: state_dict}."""
        cursor = await self._db.execute(
            "SELECT * FROM strategy_state WHERE strategy = ?", (strategy,)
        )
        rows = await cursor.fetchall()
        return {
            row["symbol"]: {
                "state": row["state"],
                "direction": row["direction"],
                "breakout_level": row["breakout_level"],
                "pullback_count": row["pullback_count"],
                "window_candles": row["window_candles"],
            }
            for row in rows
        }

    # --- Daily State Persistence ---

    async def increment_daily_trades(self) -> int:
        """Increment today's trade count. Returns new count."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        await self._db.execute(
            """INSERT INTO daily_state (date, trade_count, updated_at)
               VALUES (?, 1, ?)
               ON CONFLICT(date) DO UPDATE SET
                 trade_count = trade_count + 1,
                 updated_at = ?""",
            (today, datetime.utcnow(), datetime.utcnow()),
        )
        await self._db.commit()
        cursor = await self._db.execute(
            "SELECT trade_count FROM daily_state WHERE date = ?", (today,)
        )
        row = await cursor.fetchone()
        return row["trade_count"] if row else 1

    async def get_daily_trade_count(self) -> int:
        """Get today's trade count."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        cursor = await self._db.execute(
            "SELECT trade_count FROM daily_state WHERE date = ?", (today,)
        )
        row = await cursor.fetchone()
        return row["trade_count"] if row else 0

    async def get_daily_stats(self) -> dict:
        """Get today's trading statistics."""
        cursor = await self._db.execute(
            """SELECT
                 COUNT(*) as total_trades,
                 SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                 SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) as losing_trades,
                 COALESCE(SUM(pnl), 0) as total_pnl
               FROM trades
               WHERE date(opened_at) = date('now')
                 AND closed_at IS NOT NULL"""
        )
        row = await cursor.fetchone()
        return dict(row) if row else {
            "total_trades": 0, "winning_trades": 0, "losing_trades": 0, "total_pnl": 0
        }
