"""Volume Profile — POC, VAH, VAL from price-volume distribution.

POC (Point of Control) = price with highest traded volume
VAH (Value Area High) = upper boundary of 70% volume zone
VAL (Value Area Low) = lower boundary of 70% volume zone

These are the strongest S/R levels because they represent where
the most trading activity occurred. ~80% of naked POCs get revisited.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class VolumeProfileResult:
    """Volume profile levels."""

    poc: float = 0.0  # Point of Control (highest volume price)
    vah: float = 0.0  # Value Area High
    val: float = 0.0  # Value Area Low
    total_volume: float = 0.0


class VolumeProfile:
    """Session-based Volume Profile calculator."""

    def __init__(self, num_bins: int = 50, value_area_pct: float = 0.70) -> None:
        self._num_bins = num_bins
        self._va_pct = value_area_pct

    def calculate(self, df: pd.DataFrame) -> VolumeProfileResult:
        """Calculate volume profile for the given data.

        Bins the price range and sums volume at each level.
        POC = bin with highest volume.
        Value Area = 70% of total volume centered around POC.
        """
        if df is None or len(df) < 5:
            return VolumeProfileResult()

        high = df["high"].values
        low = df["low"].values
        close = df["close"].values
        volume = (
            df["tick_volume"].values.astype(float)
            if "tick_volume" in df.columns
            else np.ones(len(df))
        )

        price_min = float(np.min(low))
        price_max = float(np.max(high))

        if price_max <= price_min:
            return VolumeProfileResult(poc=float(close[-1]))

        # Create price bins
        bin_edges = np.linspace(price_min, price_max, self._num_bins + 1)
        bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
        bin_volumes = np.zeros(self._num_bins)

        # Distribute each bar's volume across the bins it spans
        for i in range(len(df)):
            bar_low = float(low[i])
            bar_high = float(high[i])
            bar_vol = float(volume[i])

            # Find which bins this bar spans
            low_bin = np.searchsorted(bin_edges, bar_low, side="right") - 1
            high_bin = np.searchsorted(bin_edges, bar_high, side="right") - 1
            low_bin = max(0, min(low_bin, self._num_bins - 1))
            high_bin = max(0, min(high_bin, self._num_bins - 1))

            n_bins_spanned = high_bin - low_bin + 1
            if n_bins_spanned > 0:
                vol_per_bin = bar_vol / n_bins_spanned
                bin_volumes[low_bin : high_bin + 1] += vol_per_bin

        total_vol = float(np.sum(bin_volumes))
        if total_vol <= 0:
            return VolumeProfileResult(poc=float(close[-1]))

        # POC = bin with highest volume
        poc_idx = int(np.argmax(bin_volumes))
        poc = float(bin_centers[poc_idx])

        # Value Area: expand from POC until 70% of total volume is included
        va_target = total_vol * self._va_pct
        va_vol = float(bin_volumes[poc_idx])
        va_low_idx = poc_idx
        va_high_idx = poc_idx

        while va_vol < va_target:
            # Expand in the direction with more volume
            expand_low = bin_volumes[va_low_idx - 1] if va_low_idx > 0 else 0
            expand_high = (
                bin_volumes[va_high_idx + 1] if va_high_idx < self._num_bins - 1 else 0
            )

            if expand_low == 0 and expand_high == 0:
                break

            if expand_low >= expand_high and va_low_idx > 0:
                va_low_idx -= 1
                va_vol += float(bin_volumes[va_low_idx])
            elif va_high_idx < self._num_bins - 1:
                va_high_idx += 1
                va_vol += float(bin_volumes[va_high_idx])
            else:
                break

        vah = float(bin_centers[min(va_high_idx, self._num_bins - 1)])
        val = float(bin_centers[max(va_low_idx, 0)])

        return VolumeProfileResult(poc=poc, vah=vah, val=val, total_volume=total_vol)

    def get_session_profile(
        self, df: pd.DataFrame, lookback_bars: int = 200
    ) -> VolumeProfileResult:
        """Calculate volume profile for the most recent N bars."""
        window = df.tail(lookback_bars) if len(df) > lookback_bars else df
        return self.calculate(window)

    def get_previous_session_poc(
        self, df: pd.DataFrame, session_bars: int = 288
    ) -> float | None:
        """Get POC from the previous session (previous 288 M5 bars = 1 day).

        This is a strong S/R level for the current session.
        """
        if len(df) < session_bars * 2:
            return None
        prev_session = df.iloc[-(session_bars * 2) : -session_bars]
        result = self.calculate(prev_session)
        return result.poc if result.poc > 0 else None
