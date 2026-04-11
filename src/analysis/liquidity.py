"""Liquidity Sweep Detector -- identifies stop hunts on XAUUSD.

A liquidity sweep happens when price breaks a swing high/low (hunting stops)
then quickly reverses.  This is an institutional footprint -- smart money
sweeps retail stops before moving price in their intended direction.

After a bullish sweep (price dips below swing low then reverses UP):
    Strong BUY signal (institutions accumulated at swept level).

After a bearish sweep (price spikes above swing high then reverses DOWN):
    Strong SELL signal (institutions distributed at swept level).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class SweepResult:
    """Result of liquidity sweep detection."""

    bullish_sweep: bool = False   # swept below swing low, reversing up
    bearish_sweep: bool = False   # swept above swing high, reversing down
    sweep_level: float = 0.0     # the price level that was swept
    strength: float = 0.0        # 0-1 confidence (deeper sweep + rejection = stronger)
    wick_depth: float = 0.0      # how far past the level price went


class LiquiditySweepDetector:
    """Detects stop hunts / liquidity sweeps."""

    def __init__(
        self,
        lookback: int = 20,
        sweep_buffer_pips: float = 1.0,
        min_wick_ratio: float = 0.5,
    ) -> None:
        self._lookback = lookback
        self._buffer = sweep_buffer_pips
        self._min_wick_ratio = min_wick_ratio

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(self, df: pd.DataFrame, point_size: float = 0.01) -> SweepResult:
        """Detect liquidity sweeps in the most recent bars.

        Logic
        -----
        1. Find the swing low / swing high in the lookback window
           (excluding the last 2 bars).
        2. Check if the *second-to-last* bar broke past the swing level.
        3. Check if the *last* bar reversed (closed back inside the range).
        4. Measure wick depth and rejection quality.
        """
        if df is None or len(df) < self._lookback + 2:
            return SweepResult()

        high = df["high"].values
        low = df["low"].values
        close = df["close"].values
        open_ = df["open"].values

        volume = (
            df["tick_volume"].values
            if "tick_volume" in df.columns
            else np.ones(len(df))
        )

        # Window for swing detection (exclude last 2 bars -- sweep candidates)
        window_end = len(df) - 2
        window_start = max(0, window_end - self._lookback)

        if window_end <= window_start + 3:
            return SweepResult()

        swing_high = float(np.max(high[window_start:window_end]))
        swing_low = float(np.min(low[window_start:window_end]))

        # Sweep bar (second-to-last) and confirmation bar (last)
        si = len(df) - 2  # sweep index
        ci = len(df) - 1  # confirmation index

        s_high, s_low = float(high[si]), float(low[si])
        s_close, s_open = float(close[si]), float(open_[si])
        s_vol = float(volume[si])

        c_close = float(close[ci])
        c_high, c_low = float(high[ci]), float(low[ci])

        avg_vol = float(np.mean(volume[window_start:window_end])) or 1.0
        buffer = self._buffer * point_size

        # --- Bullish sweep: dipped below swing low then reversed UP ---
        result = self._check_bullish(
            swing_low, buffer, s_high, s_low, s_close, s_open, s_vol,
            c_close, avg_vol,
        )
        if result is not None:
            return result

        # --- Bearish sweep: spiked above swing high then reversed DOWN ---
        result = self._check_bearish(
            swing_high, buffer, s_high, s_low, s_close, s_open, s_vol,
            c_close, avg_vol,
        )
        if result is not None:
            return result

        return SweepResult()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _check_bullish(
        self, swing_low: float, buffer: float,
        s_high: float, s_low: float, s_close: float, s_open: float,
        s_vol: float, c_close: float, avg_vol: float,
    ) -> SweepResult | None:
        if s_low >= swing_low - buffer:
            return None

        wick_below = swing_low - s_low
        total_range = s_high - s_low
        if total_range <= 0:
            return None

        rejected = s_close > swing_low - buffer * 2
        confirmed = c_close > s_close and c_close > swing_low
        if not (rejected and confirmed):
            return None

        wick_ratio = wick_below / total_range
        if wick_ratio < self._min_wick_ratio * 0.5:  # relaxed for gold
            return None

        vol_ratio = s_vol / avg_vol if avg_vol > 0 else 1.0
        strength = min(1.0, wick_ratio * 0.5 + min(vol_ratio, 2.0) * 0.3 + 0.2)

        logger.debug(
            "Bullish sweep: low=%.2f below swing=%.2f, wick=%.2f, str=%.2f",
            s_low, swing_low, wick_below, strength,
        )
        return SweepResult(
            bullish_sweep=True, sweep_level=swing_low,
            wick_depth=wick_below, strength=strength,
        )

    def _check_bearish(
        self, swing_high: float, buffer: float,
        s_high: float, s_low: float, s_close: float, s_open: float,
        s_vol: float, c_close: float, avg_vol: float,
    ) -> SweepResult | None:
        if s_high <= swing_high + buffer:
            return None

        wick_above = s_high - swing_high
        total_range = s_high - s_low
        if total_range <= 0:
            return None

        rejected = s_close < swing_high + buffer * 2
        confirmed = c_close < s_close and c_close < swing_high
        if not (rejected and confirmed):
            return None

        wick_ratio = wick_above / total_range
        if wick_ratio < self._min_wick_ratio * 0.5:
            return None

        vol_ratio = s_vol / avg_vol if avg_vol > 0 else 1.0
        strength = min(1.0, wick_ratio * 0.5 + min(vol_ratio, 2.0) * 0.3 + 0.2)

        logger.debug(
            "Bearish sweep: high=%.2f above swing=%.2f, wick=%.2f, str=%.2f",
            s_high, swing_high, wick_above, strength,
        )
        return SweepResult(
            bearish_sweep=True, sweep_level=swing_high,
            wick_depth=wick_above, strength=strength,
        )
