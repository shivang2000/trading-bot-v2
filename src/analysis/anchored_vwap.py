"""Anchored VWAP — dynamic support/resistance from anchor points.

Unlike standard VWAP (reset at session open), Anchored VWAP starts
from significant price events: swing highs, swing lows, session opens.
Multiple AVWAPs create a web of dynamic S/R levels.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


class AnchoredVWAP:
    """Calculate VWAP anchored to specific price events."""

    def __init__(self, lookback: int = 50) -> None:
        self._lookback = lookback

    def calculate(self, df: pd.DataFrame, anchor_idx: int) -> float | None:
        """Calculate current VWAP value from anchor point.

        Returns the VWAP value at the last bar, or None if insufficient data.
        """
        if anchor_idx < 0 or anchor_idx >= len(df):
            return None
        sliced = df.iloc[anchor_idx:]
        if len(sliced) < 2:
            return None

        high = sliced["high"].values
        low = sliced["low"].values
        close = sliced["close"].values
        volume = (
            sliced["tick_volume"].values.astype(float)
            if "tick_volume" in sliced.columns
            else np.ones(len(sliced))
        )

        typical = (high + low + close) / 3.0
        cum_tp_vol = np.cumsum(typical * volume)
        cum_vol = np.cumsum(volume)
        cum_vol_safe = np.where(cum_vol == 0, 1, cum_vol)
        vwap = cum_tp_vol / cum_vol_safe

        return float(vwap[-1])

    def find_anchors(self, df: pd.DataFrame) -> list[int]:
        """Find significant anchor points in the data.

        Returns indices of:
        - Highest high in lookback (swing high anchor)
        - Lowest low in lookback (swing low anchor)
        - Most recent session boundary (00:00, 08:00, 13:00 UTC)
        """
        if len(df) < 10:
            return []

        end = len(df)
        start = max(0, end - self._lookback)
        window = df.iloc[start:end]

        anchors = []

        # Swing high anchor
        high_idx = int(window["high"].values.argmax()) + start
        anchors.append(high_idx)

        # Swing low anchor
        low_idx = int(window["low"].values.argmin()) + start
        if low_idx != high_idx:
            anchors.append(low_idx)

        # Session boundary anchors
        if "time" in df.columns:
            times = pd.to_datetime(df["time"])
            session_hours = [0, 8, 13]
            for i in range(end - 1, max(start, end - 100), -1):
                if times.iloc[i].hour in session_hours and times.iloc[i].minute < 10:
                    anchors.append(i)
                    break

        return sorted(set(anchors))

    def get_nearest_levels(self, df: pd.DataFrame, current_price: float) -> dict:
        """Get nearest AVWAP levels above and below current price.

        Returns: {
            "avwap_above": float or None,
            "avwap_below": float or None,
            "distance_above": float,
            "distance_below": float,
        }
        """
        anchors = self.find_anchors(df)
        if not anchors:
            return {
                "avwap_above": None,
                "avwap_below": None,
                "distance_above": float("inf"),
                "distance_below": float("inf"),
            }

        levels = []
        for idx in anchors:
            vwap = self.calculate(df, idx)
            if vwap is not None:
                levels.append(vwap)

        above = [l for l in levels if l > current_price]
        below = [l for l in levels if l <= current_price]

        avwap_above = min(above) if above else None
        avwap_below = max(below) if below else None

        return {
            "avwap_above": avwap_above,
            "avwap_below": avwap_below,
            "distance_above": (avwap_above - current_price) if avwap_above else float("inf"),
            "distance_below": (current_price - avwap_below) if avwap_below else float("inf"),
        }
