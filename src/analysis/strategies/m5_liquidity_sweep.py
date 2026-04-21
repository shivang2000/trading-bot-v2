"""M5 Liquidity Sweep Reversal Strategy.

Detects when price sweeps (wicks through) a support/resistance level
but closes back inside the range — indicating institutional stop-hunt
followed by reversal. Distinguishes sweeps (reversal) from runs (continuation).

Entry: After sweep confirmed + engulfing candle on retest.
SL: Beyond the swept level.
TP: Next S/R level or ATR-dynamic.
Session: 7:00-21:00 UTC. Max 10 trades/day.
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


class M5LiquiditySweepStrategy(ScalpingStrategyBase):
    """Liquidity sweep reversal detector on M5.

    Identifies institutional stop-hunts at S/R levels:
    - SWEEP: wick penetrates level, body closes back inside → reversal entry
    - RUN: body closes impulsively beyond level → skip (continuation)
    """

    def __init__(
        self,
        sr_lookback: int = 100,
        sr_touch_threshold: int = 3,
        sr_tolerance_pct: float = 0.1,
        sweep_min_wick_pct: float = 0.3,
        tp_atr_mult: float = 2.5,
        atr_period: int = 14,
        use_rsi_filter: bool = True,
        max_trades_per_day: int = 10,
    ) -> None:
        super().__init__(max_trades_per_day=max_trades_per_day)
        self._sr_lookback = sr_lookback
        self._sr_touch_threshold = sr_touch_threshold
        self._sr_tolerance_pct = sr_tolerance_pct
        self._sweep_min_wick_pct = sweep_min_wick_pct
        self._tp_atr_mult = tp_atr_mult
        self._atr_period = atr_period
        self._use_rsi_filter = use_rsi_filter

        # Cache S/R levels per symbol (recalculate every 20 bars)
        self._sr_cache: dict[str, tuple[list[float], int]] = {}

    def _get_sr_levels(self, symbol: str, bars: pd.DataFrame) -> list[float]:
        """Get S/R levels, cached and refreshed every 20 bars."""
        bar_count = len(bars)
        cached = self._sr_cache.get(symbol)
        if cached and abs(bar_count - cached[1]) < 20:
            return cached[0]

        levels = self._detect_sr_levels(
            bars,
            lookback=self._sr_lookback,
            touch_threshold=self._sr_touch_threshold,
            tolerance_pct=self._sr_tolerance_pct,
        )
        self._sr_cache[symbol] = (levels, bar_count)
        return levels

    async def scan(
        self,
        symbol: str,
        m5_bars: pd.DataFrame,
        point_size: float = 0.01,
        as_of: datetime | None = None,
        **kwargs,
    ) -> StrategySignal | None:
        """Scan for liquidity sweep at S/R levels."""
        min_bars = max(self._sr_lookback, self._atr_period) + 10
        if m5_bars is None or len(m5_bars) < min_bars:
            return None

        now = as_of or datetime.now(timezone.utc)

        if not self._check_session(now, ACTIVE_HOURS):
            return None
        if not self._check_daily_limit(symbol, now):
            return None

        # Get S/R levels
        sr_levels = self._get_sr_levels(symbol, m5_bars)
        if not sr_levels:
            return None

        # Current bar
        curr = m5_bars.iloc[-1]
        curr_open = float(curr["open"])
        curr_close = float(curr["close"])
        curr_high = float(curr["high"])
        curr_low = float(curr["low"])
        curr_range = curr_high - curr_low

        if curr_range <= 0:
            return None

        # ATR
        atr = ta.atr(
            m5_bars["high"], m5_bars["low"], m5_bars["close"],
            length=self._atr_period,
        )
        if atr is None or atr.empty:
            return None
        curr_atr = float(atr.iloc[-1])
        if curr_atr <= 0:
            return None

        tolerance = curr_atr * 0.3  # proximity threshold to S/R

        # Check each S/R level for sweep
        for level in sr_levels[:5]:  # check top 5 most-touched levels
            # BEARISH SWEEP (wick above resistance, body below) → SELL
            if curr_high > level + tolerance and curr_close < level:
                upper_wick = curr_high - max(curr_open, curr_close)
                if upper_wick / curr_range < self._sweep_min_wick_pct:
                    continue  # wick not prominent enough

                # Body closed back below → this is a SWEEP, not a RUN
                direction = "SELL"

                if self._use_rsi_filter and not self._check_rsi_filter(m5_bars, direction):
                    continue

                sl = curr_high + point_size
                tp = curr_close - curr_atr * self._tp_atr_mult

                self._increment_daily_count(symbol, now)
                logger.info(
                    "Liq Sweep [%s]: SELL sweep above %.2f (high=%.2f, close=%.2f, SL=%.2f, TP=%.2f)",
                    symbol, level, curr_high, curr_close, sl, tp,
                )

                return StrategySignal(
                    symbol=symbol,
                    action="SELL",
                    entry_price=curr_close,
                    stop_loss=sl,
                    take_profit=tp,
                    confidence=0.70,
                    reason=f"Liq sweep SELL above {level:.2f} (wick={upper_wick:.2f})",
                    strategy_name="m5_liquidity_sweep",
                )

            # BULLISH SWEEP (wick below support, body above) → BUY
            if curr_low < level - tolerance and curr_close > level:
                lower_wick = min(curr_open, curr_close) - curr_low
                if lower_wick / curr_range < self._sweep_min_wick_pct:
                    continue

                direction = "BUY"

                if self._use_rsi_filter and not self._check_rsi_filter(m5_bars, direction):
                    continue

                sl = curr_low - point_size
                tp = curr_close + curr_atr * self._tp_atr_mult

                self._increment_daily_count(symbol, now)
                logger.info(
                    "Liq Sweep [%s]: BUY sweep below %.2f (low=%.2f, close=%.2f, SL=%.2f, TP=%.2f)",
                    symbol, level, curr_low, curr_close, sl, tp,
                )

                return StrategySignal(
                    symbol=symbol,
                    action="BUY",
                    entry_price=curr_close,
                    stop_loss=sl,
                    take_profit=tp,
                    confidence=0.70,
                    reason=f"Liq sweep BUY below {level:.2f} (wick={lower_wick:.2f})",
                    strategy_name="m5_liquidity_sweep",
                )

        return None
