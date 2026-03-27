"""Shared base class for all scalping strategies.

Provides common functionality:
- Daily trade limit tracking
- Session hour filtering
- ATR-dynamic SL/TP calculation based on ADX regime
- Candlestick pattern detection (pin bars, engulfing)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import pandas as pd

from src.core.models import StrategySignal

logger = logging.getLogger(__name__)


class ScalpingStrategyBase(ABC):
    """Abstract base for scalping strategies with shared helpers."""

    def __init__(self, max_trades_per_day: int = 30) -> None:
        self._max_trades_per_day = max_trades_per_day
        self._daily_trades: dict[str, tuple[str, int]] = {}

    def _check_daily_limit(self, symbol: str, as_of: datetime) -> bool:
        """Return True if daily trade limit NOT yet reached."""
        today_str = as_of.strftime("%Y-%m-%d")
        entry = self._daily_trades.get(symbol, ("", 0))
        if entry[0] == today_str and entry[1] >= self._max_trades_per_day:
            return False
        return True

    def _increment_daily_count(self, symbol: str, as_of: datetime) -> None:
        """Increment the daily trade counter."""
        today_str = as_of.strftime("%Y-%m-%d")
        entry = self._daily_trades.get(symbol, ("", 0))
        if entry[0] == today_str:
            self._daily_trades[symbol] = (today_str, entry[1] + 1)
        else:
            self._daily_trades[symbol] = (today_str, 1)

    @staticmethod
    def _check_session(as_of: datetime, allowed_hours: list[int]) -> bool:
        """Return True if current hour is in allowed hours list."""
        return as_of.hour in allowed_hours

    @staticmethod
    def _atr_dynamic_sl_tp(atr: float, adx: float) -> tuple[float, float]:
        """Return (sl_multiplier, tp_multiplier) based on ADX regime.

        Low volatility (ADX < 20):  SL = 1.0x ATR, TP = 1.5x ATR
        Normal (ADX 20-30):         SL = 1.5x ATR, TP = 3.0x ATR
        High volatility (ADX > 30): SL = 2.0x ATR, TP = 4.0x ATR
        """
        if adx < 20:
            return 1.0, 1.5
        elif adx <= 30:
            return 1.5, 3.0
        else:
            return 2.0, 4.0

    @staticmethod
    def detect_candle_pattern(
        o: float, h: float, l: float, c: float,
        po: float, ph: float, pl: float, pc: float,
    ) -> str:
        """Detect candlestick patterns from current and previous bar OHLC.

        Returns one of: "pin_bar_bull", "pin_bar_bear",
                        "engulfing_bull", "engulfing_bear", or ""
        """
        body = abs(c - o)
        total_range = h - l

        if total_range <= 0:
            return ""

        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l

        # Pin bar: one wick > 2x body, other wick < 0.5x body
        if body > 0:
            if lower_wick > 2 * body and upper_wick < 0.5 * body and c > o:
                return "pin_bar_bull"
            if upper_wick > 2 * body and lower_wick < 0.5 * body and c < o:
                return "pin_bar_bear"

        # Engulfing: current body completely engulfs previous body
        prev_body_high = max(po, pc)
        prev_body_low = min(po, pc)
        curr_body_high = max(o, c)
        curr_body_low = min(o, c)

        if c > o and pc < po:  # Current green engulfs previous red
            if curr_body_high > prev_body_high and curr_body_low < prev_body_low:
                return "engulfing_bull"
        if c < o and pc > po:  # Current red engulfs previous green
            if curr_body_high > prev_body_high and curr_body_low < prev_body_low:
                return "engulfing_bear"

        return ""

    @abstractmethod
    async def scan(self, symbol: str, **kwargs) -> StrategySignal | None:
        """Scan bars and return a signal or None."""
        ...
