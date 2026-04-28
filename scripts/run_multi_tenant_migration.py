"""Apply the multi-tenant schema migration to data/trading_bot_v2.db.

SQLite cannot add UNIQUE constraints or CHECK columns via ALTER, so this
runner inspects PRAGMA table_info first and only issues the ALTERs whose
columns are missing. Idempotent — safe to run multiple times.

Usage:
    python -m scripts.run_multi_tenant_migration [path/to/db.sqlite]

Run the static SQL file BEFORE this script (it creates tables + seeds
reference rows; this script only patches existing tables):

    sqlite3 data/trading_bot_v2.db < scripts/init_multi_tenant_schema.sql
    python -m scripts.run_multi_tenant_migration
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DEFAULT_DB = Path("data/trading_bot_v2.db")

# (table, column, ddl_to_add)
TRADES_COLUMNS: list[tuple[str, str, str]] = [
    ("trades", "account_id",        "ALTER TABLE trades ADD COLUMN account_id TEXT"),
    ("trades", "system_id",         "ALTER TABLE trades ADD COLUMN system_id INTEGER"),
    ("trades", "signal_source",     "ALTER TABLE trades ADD COLUMN signal_source TEXT"),
    ("trades", "strategy_name",     "ALTER TABLE trades ADD COLUMN strategy_name TEXT"),
    ("trades", "parsed_signal_id",  "ALTER TABLE trades ADD COLUMN parsed_signal_id INTEGER"),
    ("trades", "magic",             "ALTER TABLE trades ADD COLUMN magic INTEGER"),
    ("trades", "commission",        "ALTER TABLE trades ADD COLUMN commission NUMERIC DEFAULT 0"),
    ("trades", "swap",              "ALTER TABLE trades ADD COLUMN swap NUMERIC DEFAULT 0"),
    ("channel_stats", "account_id",
     "ALTER TABLE channel_stats ADD COLUMN account_id TEXT"),
    ("channel_stats", "expectancy",
     "ALTER TABLE channel_stats ADD COLUMN expectancy NUMERIC DEFAULT 0"),
    ("channel_stats", "sample_size",
     "ALTER TABLE channel_stats ADD COLUMN sample_size INTEGER"),
]


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    if not db_path.exists():
        print(f"DB not found: {db_path}. Create it first with the bot's"
              f" TrackingDB.connect() or by running the bot once.")
        return 1

    print(f"Patching {db_path} (multi-tenant migration)...")
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        for table, column, ddl in TRADES_COLUMNS:
            # Only ALTER if both the table and column are missing
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchall()
            if not tables:
                print(f"  skip: table {table!r} does not exist yet")
                continue
            if column_exists(conn, table, column):
                print(f"  skip: {table}.{column} already present")
                continue
            print(f"  add:  {ddl}")
            conn.execute(ddl)

        # Composite UNIQUE on trades(account_id, mt5_ticket) — index, not constraint
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_acct_ticket "
                "ON trades(account_id, mt5_ticket)"
            )
            print("  ok:   idx_trades_acct_ticket (account_id, mt5_ticket)")
        except sqlite3.OperationalError as e:
            print(f"  warn: could not create idx_trades_acct_ticket: {e}")

        conn.commit()
    print("Done. Verify:")
    print(f"    sqlite3 {db_path} '.schema trades'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
