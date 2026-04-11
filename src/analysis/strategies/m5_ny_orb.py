"""M5 New York Opening Range Breakout Strategy.

Proven since 1990 (Toby Crabel). Defines the range from the first N minutes
of the NY cash session and trades the first breakout.

Configurable parameters:
- range_minutes: 15, 30, or 60 minute range window
- tp_multiplier: 1.5x, 2.0x, or 2.5x range width for TP
- retrace_entry: wait for price to retrace into the range before entering
- retrace_pct: how deep into the range to wait (0.5 = mid-range)

Session: 14:30-21:00 UTC only. Max 2 trades/day (one per direction).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

from src.core.models import StrategySignal
from src.analysis.strategies.scalping_base import ScalpingStrategyBase

logger = logging.getLogger(__name__)

ACTIVE_HOURS = list(range(14, 22))
RANGE_START_HOUR = 14
RANGE_START_MIN = 30
SESSION_END_HOUR = 21


class M5NyOrbStrategy(ScalpingStrategyBase):
    """NY Opening Range Breakout on M5.

    Builds the high/low range from the first N minutes of the NY session,
    then trades the first breakout in either direction. Supports both
    immediate breakout and retrace entry modes.
    """

    def __init__(
        self,
        range_minutes: int = 15,
        tp_multiplier: float = 2.0,
        min_range_points: float = 5.0,
        retrace_entry: bool = True,
        retrace_pct: float = 0.5,
        retrace_timeout_bars: int = 24,
        max_trades_per_day: int = 2,
        use_rsi_filter: bool = True,
    ) -> None:
        super().__init__(max_trades_per_day=max_trades_per_day)
        self._range_minutes = range_minutes
        self._tp_multiplier = tp_multiplier
        self._min_range_points = min_range_points
        self._retrace_entry = retrace_entry
        self._retrace_pct = retrace_pct
        self._retrace_timeout_bars = retrace_timeout_bars
        self._use_rsi_filter = use_rsi_filter

        # Compute range end time from start + range_minutes
        total_start_min = RANGE_START_HOUR * 60 + RANGE_START_MIN
        total_end_min = total_start_min + range_minutes
        self._range_end_hour = total_end_min // 60
        self._range_end_min = total_end_min % 60

        # State per symbol
        self._range_high: dict[str, float] = {}
        self._range_low: dict[str, float] = {}
        self._range_built: dict[str, bool] = {}
        self._long_taken: dict[str, bool] = {}
        self._short_taken: dict[str, bool] = {}
        self._last_reset_day: dict[str, str] = {}
        # Retrace state
        self._breakout_direction: dict[str, str | None] = {}
        self._breakout_bar_count: dict[str, int] = {}
        self._retrace_pending: dict[str, bool] = {}

    def _reset_state(self, symbol: str, day_key: str) -> None:
        """Reset state for a new session."""
        self._range_high[symbol] = 0.0
        self._range_low[symbol] = float("inf")
        self._range_built[symbol] = False
        self._long_taken[symbol] = False
        self._short_taken[symbol] = False
        self._last_reset_day[symbol] = day_key
        self._breakout_direction[symbol] = None
        self._breakout_bar_count[symbol] = 0
        self._retrace_pending[symbol] = False

    def _should_reset(self, symbol: str, now: datetime) -> bool:
        """Reset on new day or at session end (21:00 UTC)."""
        day_key = now.strftime("%Y-%m-%d")
        if self._last_reset_day.get(symbol) != day_key:
            return True
        if now.hour >= SESSION_END_HOUR and self._range_built.get(symbol, False):
            return True
        return False

    def _is_range_building_period(self, now: datetime) -> bool:
        """Check if we're in the range-building window."""
        minutes = now.hour * 60 + now.minute
        start = RANGE_START_HOUR * 60 + RANGE_START_MIN
        end = self._range_end_hour * 60 + self._range_end_min
        return start <= minutes < end

    async def scan(
        self,
        symbol: str,
        m5_bars: pd.DataFrame,
        point_size: float = 0.01,
        as_of: datetime | None = None,
        **kwargs,
    ) -> StrategySignal | None:
        """Scan M5 bars for NY Opening Range Breakout signal."""
        if m5_bars is None or len(m5_bars) < 20:
            return None

        now = as_of or datetime.now(timezone.utc)

        if self._should_reset(symbol, now):
            self._reset_state(symbol, now.strftime("%Y-%m-%d"))

        if not self._check_session(now, ACTIVE_HOURS):
            return None
        if not self._check_daily_limit(symbol, now):
            return None

        curr_high = float(m5_bars["high"].iloc[-1])
        curr_low = float(m5_bars["low"].iloc[-1])
        curr_close = float(m5_bars["close"].iloc[-1])

        # Phase 1: Build the range
        if self._is_range_building_period(now):
            self._range_high[symbol] = max(
                self._range_high.get(symbol, 0.0), curr_high
            )
            self._range_low[symbol] = min(
                self._range_low.get(symbol, float("inf")), curr_low
            )
            return None

        # Phase 2: Mark range as built
        if not self._range_built.get(symbol, False):
            rh = self._range_high.get(symbol, 0.0)
            rl = self._range_low.get(symbol, float("inf"))
            if rh > 0 and rl < float("inf") and rh > rl:
                self._range_built[symbol] = True
                logger.info(
                    "NY ORB [%s]: Range built (%.0fmin) high=%.2f low=%.2f (width=%.2f)",
                    symbol, self._range_minutes, rh, rl, rh - rl,
                )
            else:
                return None

        range_high = self._range_high[symbol]
        range_low = self._range_low[symbol]
        range_width = range_high - range_low

        if range_width < self._min_range_points * point_size:
            return None

        # Phase 3: Check for retrace entry completion
        if self._retrace_entry and self._retrace_pending.get(symbol, False):
            return self._check_retrace(
                symbol, curr_close, m5_bars, point_size, now,
                range_high, range_low, range_width,
            )

        # Phase 4: Check for breakout
        if not self._long_taken.get(symbol, False) and curr_close > range_high:
            if self._retrace_pending.get(symbol, False):
                return None  # already waiting for other direction retrace
            return self._handle_breakout(
                symbol, "BUY", curr_close, m5_bars, point_size, now,
                range_high, range_low, range_width,
            )

        if not self._short_taken.get(symbol, False) and curr_close < range_low:
            if self._retrace_pending.get(symbol, False):
                return None
            return self._handle_breakout(
                symbol, "SELL", curr_close, m5_bars, point_size, now,
                range_high, range_low, range_width,
            )

        return None

    def _handle_breakout(
        self, symbol: str, direction: str, curr_close: float,
        m5_bars: pd.DataFrame, point_size: float, now: datetime,
        range_high: float, range_low: float, range_width: float,
    ) -> StrategySignal | None:
        """Handle a breakout — either enter immediately or start retrace wait."""
        if self._retrace_entry:
            # Start retrace wait
            self._breakout_direction[symbol] = direction
            self._breakout_bar_count[symbol] = 0
            self._retrace_pending[symbol] = True
            logger.info(
                "NY ORB [%s]: %s breakout detected, waiting for retrace (%.0f%%)",
                symbol, direction, self._retrace_pct * 100,
            )
            return None

        # Immediate entry
        return self._emit_signal(
            symbol, direction, curr_close, m5_bars, point_size, now,
            range_high, range_low, range_width,
        )

    def _check_retrace(
        self, symbol: str, curr_close: float, m5_bars: pd.DataFrame,
        point_size: float, now: datetime,
        range_high: float, range_low: float, range_width: float,
    ) -> StrategySignal | None:
        """Check if price has retraced enough for entry."""
        direction = self._breakout_direction[symbol]
        self._breakout_bar_count[symbol] += 1

        # Timeout check (clamp to session end)
        if self._breakout_bar_count[symbol] > self._retrace_timeout_bars:
            logger.debug("NY ORB [%s]: Retrace timeout, cancelling", symbol)
            self._retrace_pending[symbol] = False
            return None

        # Invalidation: price hit opposite side of range
        if direction == "BUY" and curr_close < range_low:
            logger.debug("NY ORB [%s]: BUY retrace invalidated (below range)", symbol)
            self._retrace_pending[symbol] = False
            return None
        if direction == "SELL" and curr_close > range_high:
            logger.debug("NY ORB [%s]: SELL retrace invalidated (above range)", symbol)
            self._retrace_pending[symbol] = False
            return None

        # Check if price retraced enough
        if direction == "BUY":
            retrace_level = range_high + range_width * (1 - self._retrace_pct)
            # Price must come back down to retrace_level then bounce
            if curr_close <= retrace_level and curr_close > range_low:
                # Retraced — enter at current close
                self._retrace_pending[symbol] = False
                return self._emit_signal(
                    symbol, "BUY", curr_close, m5_bars, point_size, now,
                    range_high, range_low, range_width,
                )
        else:
            retrace_level = range_low - range_width * (1 - self._retrace_pct)
            if curr_close >= retrace_level and curr_close < range_high:
                self._retrace_pending[symbol] = False
                return self._emit_signal(
                    symbol, "SELL", curr_close, m5_bars, point_size, now,
                    range_high, range_low, range_width,
                )

        return None

    def _emit_signal(
        self, symbol: str, direction: str, curr_close: float,
        m5_bars: pd.DataFrame, point_size: float, now: datetime,
        range_high: float, range_low: float, range_width: float,
    ) -> StrategySignal | None:
        """Emit the actual trading signal."""
        if self._use_rsi_filter and not self._check_rsi_filter(m5_bars, direction):
            logger.debug("NY ORB [%s]: %s blocked by RSI filter", symbol, direction)
            return None

        if direction == "BUY":
            sl = range_low
            tp = curr_close + self._tp_multiplier * range_width
            self._long_taken[symbol] = True
        else:
            sl = range_high
            tp = curr_close - self._tp_multiplier * range_width
            self._short_taken[symbol] = True

        self._increment_daily_count(symbol, now)

        entry_mode = "retrace" if self._retrace_entry else "immediate"
        logger.info(
            "NY ORB [%s]: %s %s @ %.2f (range=%.2f-%.2f, SL=%.2f, TP=%.2f, %dmin range, %.1fx TP)",
            symbol, direction, entry_mode, curr_close, range_low, range_high,
            sl, tp, self._range_minutes, self._tp_multiplier,
        )

        return StrategySignal(
            symbol=symbol,
            action=direction,
            entry_price=curr_close,
            stop_loss=sl,
            take_profit=tp,
            confidence=0.65,
            reason=f"NY ORB {direction} {entry_mode} ({self._range_minutes}min, {self._tp_multiplier}x TP)",
            strategy_name="m5_ny_orb",
        )
