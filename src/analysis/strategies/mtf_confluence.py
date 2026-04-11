"""Multi-Timeframe Confluence Strategies.

Uses higher timeframe context (H1/H4) to filter lower timeframe entries (M5/M1).
Only takes M5/M1 scalping entries when they align with H1 structure.

Strategy B1: H1 S/R + M5 RSI Bounce
  - Detect H1 support/resistance levels (swing highs/lows)
  - Only enter M5 RSI extreme signals when price is near H1 S/R

Strategy B4: H1 Trend + M1 Micro
  - Only take M1 EMA micro pullbacks when H1 EMA(50) confirms the trend direction
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pandas_ta as ta

from src.core.models import StrategySignal

logger = logging.getLogger(__name__)


def detect_sr_levels(h1_bars: pd.DataFrame, lookback: int = 50, tolerance_pct: float = 0.002) -> list[float]:
    """Detect H1 support/resistance levels from swing highs/lows.

    Returns a list of price levels where price has bounced multiple times.
    """
    if h1_bars is None or len(h1_bars) < lookback:
        return []

    highs = h1_bars["high"].tail(lookback).values
    lows = h1_bars["low"].tail(lookback).values

    # Find swing highs (higher than both neighbors)
    levels = []
    for i in range(2, len(highs) - 2):
        if highs[i] > highs[i - 1] and highs[i] > highs[i + 1] and highs[i] > highs[i - 2] and highs[i] > highs[i + 2]:
            levels.append(float(highs[i]))
        if lows[i] < lows[i - 1] and lows[i] < lows[i + 1] and lows[i] < lows[i - 2] and lows[i] < lows[i + 2]:
            levels.append(float(lows[i]))

    # Cluster nearby levels
    if not levels:
        return []

    levels.sort()
    clustered = [levels[0]]
    for lvl in levels[1:]:
        if abs(lvl - clustered[-1]) / clustered[-1] < tolerance_pct:
            clustered[-1] = (clustered[-1] + lvl) / 2  # merge
        else:
            clustered.append(lvl)

    return clustered


def is_near_sr(price: float, sr_levels: list[float], tolerance_pct: float = 0.003) -> tuple[bool, str]:
    """Check if price is near any S/R level. Returns (is_near, level_type)."""
    for lvl in sr_levels:
        dist_pct = abs(price - lvl) / lvl
        if dist_pct < tolerance_pct:
            level_type = "support" if price >= lvl else "resistance"
            return True, level_type
    return False, ""


class H1SrM5RsiBounceStrategy:
    """B1: Only take M5 RSI extreme entries near H1 S/R levels."""

    def __init__(
        self,
        rsi_period: int = 7,
        rsi_oversold: float = 20.0,
        rsi_overbought: float = 80.0,
        tp_pips: float = 20.0,
        sl_pips: float = 12.0,
        sr_tolerance_pct: float = 0.003,
        max_trades_per_day: int = 3,
    ) -> None:
        self._rsi_period = rsi_period
        self._rsi_os = rsi_oversold
        self._rsi_ob = rsi_overbought
        self._tp_pips = tp_pips
        self._sl_pips = sl_pips
        self._sr_tol = sr_tolerance_pct
        self._max_trades = max_trades_per_day
        self._daily_trades: dict[str, tuple[str, int]] = {}
        self._sr_cache: dict[str, tuple[list[float], datetime]] = {}

    async def scan(
        self,
        symbol: str,
        m5_bars: pd.DataFrame,
        h1_bars: pd.DataFrame,
        point_size: float = 0.01,
        as_of: datetime | None = None,
    ) -> StrategySignal | None:
        if m5_bars is None or len(m5_bars) < 30 or h1_bars is None or len(h1_bars) < 50:
            return None

        now = as_of or datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        hour = now.hour

        if hour < 8 or hour >= 21:
            return None

        entry = self._daily_trades.get(symbol, ("", 0))
        if entry[0] == today_str and entry[1] >= self._max_trades:
            return None

        # Get/cache H1 S/R levels (refresh every hour)
        cache = self._sr_cache.get(symbol)
        if cache is None or (now - cache[1]).total_seconds() > 3600:
            sr_levels = detect_sr_levels(h1_bars)
            self._sr_cache[symbol] = (sr_levels, now)
        else:
            sr_levels = cache[0]

        if not sr_levels:
            return None

        close = m5_bars["close"]
        curr_close = float(close.iloc[-1])

        # Check if near S/R
        near, level_type = is_near_sr(curr_close, sr_levels, self._sr_tol)
        if not near:
            return None

        # Calculate RSI
        rsi = ta.rsi(close, length=self._rsi_period)
        if rsi is None:
            return None

        curr_rsi = float(rsi.iloc[-1])
        tp_dist = self._tp_pips * point_size
        sl_dist = self._sl_pips * point_size

        # BUY at support with RSI oversold
        if curr_rsi < self._rsi_os and level_type == "support":
            self._daily_trades[symbol] = (today_str, entry[1] + 1 if entry[0] == today_str else 1)
            logger.info("H1 S/R + M5 RSI [%s]: BUY @ %.2f (RSI=%.0f, near support)", symbol, curr_close, curr_rsi)
            return StrategySignal(
                symbol=symbol, action="BUY", entry_price=curr_close,
                stop_loss=curr_close - sl_dist, take_profit=curr_close + tp_dist,
                confidence=0.70, reason=f"H1 support + M5 RSI {curr_rsi:.0f}",
            )

        # SELL at resistance with RSI overbought
        if curr_rsi > self._rsi_ob and level_type == "resistance":
            self._daily_trades[symbol] = (today_str, entry[1] + 1 if entry[0] == today_str else 1)
            logger.info("H1 S/R + M5 RSI [%s]: SELL @ %.2f (RSI=%.0f, near resistance)", symbol, curr_close, curr_rsi)
            return StrategySignal(
                symbol=symbol, action="SELL", entry_price=curr_close,
                stop_loss=curr_close + sl_dist, take_profit=curr_close - tp_dist,
                confidence=0.70, reason=f"H1 resistance + M5 RSI {curr_rsi:.0f}",
            )

        return None


class H1TrendM1MicroStrategy:
    """B4: M1 EMA micro pullback only when H1 trend confirms direction."""

    def __init__(
        self,
        fast_ema: int = 5,
        slow_ema: int = 10,
        h1_ema: int = 50,
        tp_pips: float = 8.0,
        sl_pips: float = 6.0,
        max_trades_per_day: int = 5,
    ) -> None:
        self._fast = fast_ema
        self._slow = slow_ema
        self._h1_ema = h1_ema
        self._tp_pips = tp_pips
        self._sl_pips = sl_pips
        self._max_trades = max_trades_per_day
        self._daily_trades: dict[str, tuple[str, int]] = {}
        self._bullish_cross: dict[str, bool] = {}
        self._bearish_cross: dict[str, bool] = {}

    async def scan(
        self,
        symbol: str,
        m1_bars: pd.DataFrame,
        h1_bars: pd.DataFrame,
        point_size: float = 0.01,
        as_of: datetime | None = None,
    ) -> StrategySignal | None:
        if m1_bars is None or len(m1_bars) < 20 or h1_bars is None or len(h1_bars) < 55:
            return None

        now = as_of or datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        hour = now.hour

        if hour < 9 or hour >= 20:
            return None

        entry = self._daily_trades.get(symbol, ("", 0))
        if entry[0] == today_str and entry[1] >= self._max_trades:
            return None

        # H1 trend direction
        h1_ema50 = ta.ema(h1_bars["close"], length=self._h1_ema)
        if h1_ema50 is None:
            return None
        h1_trend_up = float(h1_ema50.iloc[-1]) > float(h1_ema50.iloc[-5])

        close = m1_bars["close"]
        low = m1_bars["low"]
        high = m1_bars["high"]

        fast = ta.ema(close, length=self._fast)
        slow = ta.ema(close, length=self._slow)
        if fast is None or slow is None:
            return None

        curr_fast = float(fast.iloc[-1])
        prev_fast = float(fast.iloc[-2])
        curr_slow = float(slow.iloc[-1])
        prev_slow = float(slow.iloc[-2])
        curr_close = float(close.iloc[-1])
        prev_close = float(close.iloc[-2])
        curr_low = float(low.iloc[-1])
        curr_high = float(high.iloc[-1])

        # Detect crossovers
        if prev_fast <= prev_slow and curr_fast > curr_slow:
            self._bullish_cross[symbol] = True
            self._bearish_cross[symbol] = False
        elif prev_fast >= prev_slow and curr_fast < curr_slow:
            self._bearish_cross[symbol] = True
            self._bullish_cross[symbol] = False

        tp_dist = self._tp_pips * point_size
        sl_dist = self._sl_pips * point_size

        # BUY: bullish cross + pullback bounce + H1 trending UP
        if self._bullish_cross.get(symbol, False) and h1_trend_up:
            if curr_low <= curr_slow and curr_close > curr_slow and curr_close > prev_close:
                self._bullish_cross[symbol] = False
                self._daily_trades[symbol] = (today_str, entry[1] + 1 if entry[0] == today_str else 1)
                recent_low = float(low.iloc[-5:].min())
                return StrategySignal(
                    symbol=symbol, action="BUY", entry_price=curr_close,
                    stop_loss=recent_low - sl_dist, take_profit=curr_close + tp_dist,
                    confidence=0.65, reason="H1 trend UP + M1 pullback bounce",
                )

        # SELL: bearish cross + pullback drop + H1 trending DOWN
        if self._bearish_cross.get(symbol, False) and not h1_trend_up:
            if curr_high >= curr_slow and curr_close < curr_slow and curr_close < prev_close:
                self._bearish_cross[symbol] = False
                self._daily_trades[symbol] = (today_str, entry[1] + 1 if entry[0] == today_str else 1)
                recent_high = float(high.iloc[-5:].max())
                return StrategySignal(
                    symbol=symbol, action="SELL", entry_price=curr_close,
                    stop_loss=recent_high + sl_dist, take_profit=curr_close - tp_dist,
                    confidence=0.65, reason="H1 trend DOWN + M1 pullback drop",
                )

        return None
