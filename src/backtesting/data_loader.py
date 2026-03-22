"""Data loading for backtesting — CSV files, MT5 download, M15→H1 resampling.

Provides historical OHLCV data in the same DataFrame format as the live
MT5 client: columns [time, open, high, low, close, tick_volume, real_volume, spread].
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


def load_from_csv(path: str) -> pd.DataFrame:
    """Load OHLCV data from a CSV file.

    Expected columns: time, open, high, low, close, tick_volume
    Optional: real_volume, spread

    The 'time' column is parsed as datetime. Returns DataFrame sorted by time.
    """
    df = pd.read_csv(path)

    # Normalize column names to lowercase
    df.columns = [c.lower().strip() for c in df.columns]

    # Parse time column
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], utc=True)
    elif "datetime" in df.columns:
        df.rename(columns={"datetime": "time"}, inplace=True)
        df["time"] = pd.to_datetime(df["time"], utc=True)
    elif "date" in df.columns:
        df.rename(columns={"date": "time"}, inplace=True)
        df["time"] = pd.to_datetime(df["time"], utc=True)

    # Ensure required columns
    for col in ["open", "high", "low", "close"]:
        if col not in df.columns:
            raise ValueError(f"CSV missing required column: {col}")

    # Add optional columns with defaults
    if "tick_volume" not in df.columns:
        df["tick_volume"] = 0
    if "real_volume" not in df.columns:
        df["real_volume"] = 0
    if "spread" not in df.columns:
        df["spread"] = 0

    df = df.sort_values("time").reset_index(drop=True)

    logger.info("Loaded %d bars from %s (%s → %s)",
                len(df), path, df["time"].iloc[0], df["time"].iloc[-1])
    return df


def load_from_mt5(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    host: str = "localhost",
    port: int = 8001,
) -> pd.DataFrame:
    """Download historical data from MT5 via RPyC classic connection.

    Uses the same rpyc.classic.connect() pattern as the live MT5Client
    to access conn.modules['MetaTrader5'].copy_rates_range().
    """
    import rpyc

    logger.info("Downloading %s %s from MT5 (%s:%d) %s → %s",
                symbol, timeframe, host, port, start, end)

    conn = rpyc.classic.connect(host, port)
    mt5 = conn.modules["MetaTrader5"]

    if not mt5.initialize():
        error = mt5.last_error()
        conn.close()
        raise RuntimeError(f"MT5 initialize() failed: {error}")

    # Ensure symbol is visible (required for copy_rates_range)
    mt5.symbol_select(symbol, True)

    # Map timeframe string to MT5 constant
    tf_map = {
        "M1": 1, "M5": 5, "M15": 15, "M30": 30,
        "H1": 16385, "H4": 16388, "D1": 16408, "W1": 32769,
    }
    tf_val = tf_map.get(timeframe)
    if tf_val is None:
        conn.close()
        raise ValueError(f"Unknown timeframe: {timeframe}")

    # Use copy_rates_from_pos (more reliable than copy_rates_range on demo)
    # Estimate bar count: M15 = 96 bars/day, H1 = 24 bars/day
    bars_per_day = {"M1": 1440, "M5": 288, "M15": 96, "M30": 48,
                    "H1": 24, "H4": 6, "D1": 1, "W1": 0.2}
    days = (end - start).days
    estimated_bars = int(days * bars_per_day.get(timeframe, 96) * 1.2)  # 20% buffer
    estimated_bars = min(estimated_bars, 100000)  # MT5 cap

    logger.info("Requesting %d bars of %s %s", estimated_bars, symbol, timeframe)
    rates = mt5.copy_rates_from_pos(symbol, tf_val, 0, estimated_bars)

    try:
        rates_native = rpyc.classic.obtain(rates)
    except Exception:
        rates_native = rates

    conn.close()

    if rates_native is None or len(rates_native) == 0:
        raise RuntimeError(f"No data returned from MT5 for {symbol} {timeframe}")

    df = pd.DataFrame(rates_native)

    # MT5 returns 'time' as unix timestamp — convert to datetime
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)

    if "real_volume" not in df.columns:
        df["real_volume"] = 0
    if "tick_volume" not in df.columns:
        df["tick_volume"] = 0
    if "spread" not in df.columns:
        df["spread"] = 0

    df = df.sort_values("time").reset_index(drop=True)

    # Filter to requested date range
    df = df[(df["time"] >= start) & (df["time"] <= end)].reset_index(drop=True)

    logger.info("Downloaded %d bars of %s %s (filtered to range)", len(df), symbol, timeframe)
    return df


def resample_m15_to_h1(m15_df: pd.DataFrame) -> pd.DataFrame:
    """Resample M15 bars to H1 bars using standard OHLCV aggregation.

    Groups every 4 M15 bars into 1 H1 bar. Only includes completed
    H1 bars (all 4 M15 bars present).
    """
    df = m15_df.copy()
    df = df.set_index("time")

    h1 = df.resample("1h").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "tick_volume": "sum",
        "real_volume": "sum",
        "spread": "mean",
    }).dropna(subset=["open"])

    h1 = h1.reset_index()
    h1 = h1.rename(columns={"index": "time"}) if "index" in h1.columns else h1

    logger.info("Resampled %d M15 bars → %d H1 bars", len(m15_df), len(h1))
    return h1


def save_cache(df: pd.DataFrame, path: str) -> None:
    """Save DataFrame to CSV for reuse."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    logger.info("Cached %d bars to %s", len(df), path)


def load_or_download(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    cache_dir: str = "data/backtest_cache",
    mt5_host: str = "localhost",
    mt5_port: int = 8001,
) -> pd.DataFrame:
    """Load from cache if available, otherwise download from MT5 and cache."""
    cache_path = (
        Path(cache_dir)
        / f"{symbol}_{timeframe}_{start:%Y%m%d}_{end:%Y%m%d}.csv"
    )

    if cache_path.exists():
        logger.info("Loading cached data from %s", cache_path)
        return load_from_csv(str(cache_path))

    df = load_from_mt5(symbol, timeframe, start, end, mt5_host, mt5_port)
    save_cache(df, str(cache_path))
    return df
