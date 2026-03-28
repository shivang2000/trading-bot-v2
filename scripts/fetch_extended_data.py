#!/usr/bin/env python3
"""Fetch extended historical data from EC2 MT5 instance.

Downloads M5, M15, H1 data for multiple symbols and saves to local CSVs.
Handles MT5's 100K bar limit by splitting into date ranges and combining.

Usage:
    python scripts/fetch_extended_data.py --symbol XAUUSD --timeframe M5 \
        --start 2022-01-01 --end 2026-03-29 --host <ec2-ip> --port 8001

    python scripts/fetch_extended_data.py --all  # fetch all symbols + timeframes
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-8s] %(message)s")
logger = logging.getLogger("fetch_data")


def fetch_bars(host, port, symbol, timeframe, start, end):
    """Fetch bars from MT5 via RPyC. Handles 100K bar limit by chunking."""
    import rpyc

    logger.info("Connecting to MT5 at %s:%d", host, port)
    conn = rpyc.classic.connect(host, port)
    mt5 = conn.modules["MetaTrader5"]

    if not mt5.initialize():
        error = mt5.last_error()
        conn.close()
        raise RuntimeError(f"MT5 init failed: {error}")

    mt5.symbol_select(symbol, True)

    tf_map = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 16385, "H4": 16388, "D1": 16408}
    tf_val = tf_map.get(timeframe)
    if tf_val is None:
        conn.close()
        raise ValueError(f"Unknown timeframe: {timeframe}")

    bars_per_day = {"M1": 1440, "M5": 288, "M15": 96, "M30": 48, "H1": 24, "H4": 6, "D1": 1}
    max_bars_per_request = 99000

    all_dfs = []
    chunk_start = start

    while chunk_start < end:
        # Calculate chunk size based on max bars
        days_per_chunk = max_bars_per_request / bars_per_day.get(timeframe, 288)
        chunk_end = min(chunk_start + timedelta(days=days_per_chunk), end)

        logger.info("  Fetching %s %s: %s -> %s", symbol, timeframe,
                     chunk_start.strftime("%Y-%m-%d"), chunk_end.strftime("%Y-%m-%d"))

        try:
            rates = mt5.copy_rates_range(symbol, tf_val, chunk_start, chunk_end)
            rates_native = rpyc.classic.obtain(rates) if rates is not None else None

            if rates_native is not None and len(rates_native) > 0:
                df = pd.DataFrame(rates_native)
                if "time" in df.columns:
                    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
                all_dfs.append(df)
                logger.info("    Got %d bars", len(df))
            else:
                logger.warning("    No data for this chunk")
        except Exception as e:
            logger.warning("    Chunk failed: %s", e)

        chunk_start = chunk_end

    conn.close()

    if not all_dfs:
        raise RuntimeError(f"No data fetched for {symbol} {timeframe}")

    combined = pd.concat(all_dfs, ignore_index=True)
    combined = combined.drop_duplicates(subset=["time"]).sort_values("time").reset_index(drop=True)

    # Ensure standard columns
    for col in ["tick_volume", "real_volume", "spread"]:
        if col not in combined.columns:
            combined[col] = 0

    logger.info("Total: %d bars (%s -> %s)", len(combined),
                 combined["time"].iloc[0], combined["time"].iloc[-1])
    return combined


def main():
    parser = argparse.ArgumentParser(description="Fetch extended data from EC2 MT5")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--timeframe", default="M5", choices=["M1", "M5", "M15", "H1"])
    parser.add_argument("--start", default="2022-01-01")
    parser.add_argument("--end", default="2026-03-29")
    parser.add_argument("--host", required=True, help="EC2 host IP")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--output", help="Output CSV path (auto-generated if not set)")
    parser.add_argument("--all", action="store_true",
                        help="Fetch all symbols (XAUUSD, XAGUSD) and timeframes (M5, M15, H1)")
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    cache_dir = Path("data/backtest_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)

    if args.all:
        configs = [
            ("XAUUSD", "M5"), ("XAUUSD", "M15"), ("XAUUSD", "H1"),
            ("XAGUSD", "M5"), ("XAGUSD", "M15"), ("XAGUSD", "H1"),
        ]
    else:
        configs = [(args.symbol, args.timeframe)]

    for symbol, tf in configs:
        output = args.output or str(cache_dir / f"{symbol}_{tf}_extended.csv")
        try:
            df = fetch_bars(args.host, args.port, symbol, tf, start, end)
            df.to_csv(output, index=False)
            logger.info("Saved %d bars to %s", len(df), output)
        except Exception as e:
            logger.error("Failed to fetch %s %s: %s", symbol, tf, e)


if __name__ == "__main__":
    main()
