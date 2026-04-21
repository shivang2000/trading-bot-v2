"""M5 180° Bar Reversal Strategy.

Detects consecutive bars where the second completely overpowers the first —
a "180-degree turn." Fat red bar followed by fatter green bar (bull 180),
or fat green bar followed by fatter red bar (bear 180).

Claimed 82% win rate across all markets and timeframes.

Entry: On completion of the 180° pattern.
SL: Below signal bar low (bull) or above signal bar high (bear).
TP: ATR-dynamic (2x-3x the signal bar range).
Session: 7:00-21:00 UTC. Max 30 trades/day.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from src.core.models import StrategySignal
from src.analysis.strategies.scalping_base import ScalpingStrategyBase

logger = logging.getLogger(__name__)

ACTIVE_HOURS = list(range(7, 22))


class M5180ReversalStrategy(ScalpingStrategyBase):
    """180° bar reversal pattern detector on M5.

    Bull 180: Red bar → larger green bar that exceeds red's high.
    Bear 180: Green bar → larger red bar that exceeds green's low.
    The reversal bar must be "fatter" (larger range) than the setup bar.
    """

    def __init__(
        self,
        min_bar_range_atr_mult: float = 0.3,
        tp_range_mult: float = 2.5,
        entry_pct: float = 1.0,
        atr_period: int = 14,
        use_rsi_filter: bool = True,
        max_trades_per_day: int = 30,
    ) -> None:
        super().__init__(max_trades_per_day=max_trades_per_day)
        self._min_bar_range_atr_mult = min_bar_range_atr_mult
        self._tp_range_mult = tp_range_mult
        self._entry_pct = entry_pct  # 1.0 = enter at close, 0.8 = enter at 80% of bar
        self._atr_period = atr_period
        self._use_rsi_filter = use_rsi_filter

    async def scan(
        self,
        symbol: str,
        m5_bars: pd.DataFrame,
        point_size: float = 0.01,
        as_of: datetime | None = None,
        **kwargs,
    ) -> StrategySignal | None:
        """Scan M5 bars for 180° reversal pattern."""
        min_bars = self._atr_period + 5
        if m5_bars is None or len(m5_bars) < min_bars:
            return None

        now = as_of or datetime.now(timezone.utc)

        if not self._check_session(now, ACTIVE_HOURS):
            return None
        if not self._check_daily_limit(symbol, now):
            return None

        # Get last 2 bars
        prev = m5_bars.iloc[-2]
        curr = m5_bars.iloc[-1]

        prev_open = float(prev["open"])
        prev_close = float(prev["close"])
        prev_high = float(prev["high"])
        prev_low = float(prev["low"])
        prev_range = prev_high - prev_low

        curr_open = float(curr["open"])
        curr_close = float(curr["close"])
        curr_high = float(curr["high"])
        curr_low = float(curr["low"])
        curr_range = curr_high - curr_low

        # ATR for minimum bar size filter
        atr = ta.atr(
            m5_bars["high"], m5_bars["low"], m5_bars["close"],
            length=self._atr_period,
        )
        if atr is None or atr.empty:
            return None
        curr_atr = float(atr.iloc[-1])
        if curr_atr <= 0:
            return None

        # Both bars must have meaningful range (not dojis/noise)
        min_range = self._min_bar_range_atr_mult * curr_atr
        if prev_range < min_range or curr_range < min_range:
            return None

        # Detect pattern
        prev_is_red = prev_close < prev_open
        prev_is_green = prev_close > prev_open
        curr_is_red = curr_close < curr_open
        curr_is_green = curr_close > curr_open

        direction = None

        # Bull 180: Red bar → fatter green bar that exceeds red's high
        if prev_is_red and curr_is_green:
            if curr_range > prev_range and curr_high > prev_high:
                direction = "BUY"

        # Bear 180: Green bar → fatter red bar that exceeds green's low
        elif prev_is_green and curr_is_red:
            if curr_range > prev_range and curr_low < prev_low:
                direction = "SELL"

        if direction is None:
            return None

        # RSI filter
        if self._use_rsi_filter and not self._check_rsi_filter(m5_bars, direction):
            return None

        # Calculate entry, SL, TP
        if direction == "BUY":
            entry = curr_close
            sl = curr_low - point_size  # below the reversal bar
            tp_dist = curr_range * self._tp_range_mult
            tp = entry + tp_dist
        else:
            entry = curr_close
            sl = curr_high + point_size  # above the reversal bar
            tp_dist = curr_range * self._tp_range_mult
            tp = entry - tp_dist

        # Sanity check
        if direction == "BUY" and sl >= entry:
            return None
        if direction == "SELL" and sl <= entry:
            return None

        self._increment_daily_count(symbol, now)

        confidence = 0.70

        logger.info(
            "180° Reversal [%s]: %s @ %.5f (prev_range=%.2f, curr_range=%.2f, SL=%.5f, TP=%.5f)",
            symbol, direction, entry, prev_range, curr_range, sl, tp,
        )

        return StrategySignal(
            symbol=symbol,
            action=direction,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            confidence=confidence,
            reason=f"180° {direction} (prev={prev_range:.2f}, curr={curr_range:.2f})",
            strategy_name="m5_180_reversal",
        )
