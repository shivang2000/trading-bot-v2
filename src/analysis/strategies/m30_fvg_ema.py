"""M30 Fair Value Gap + EMA Stacking Strategy.

Highly selective strategy (8-10 trades per year per pair) with claimed
90% win rate and 1:20+ risk-to-reward ratio.

Requires:
1. EMAs (5, 9, 13, 21) perfectly stacked (all aligned in trend direction)
2. Fair Value Gap (FVG) forms during specific time window (8:00-11:30 UTC)
3. Doji candle forms with wick through the FVG
4. Entry on close of doji candle

SL: 10 pips (forex) or below entry candle (gold).
TP: Daily chart structure level (very wide target).
Timeframe: M30.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from src.core.models import StrategySignal
from src.analysis.strategies.scalping_base import ScalpingStrategyBase

logger = logging.getLogger(__name__)

# 3:00 - 6:30 AM NY time = 8:00 - 11:30 UTC
ACTIVE_HOURS = list(range(8, 12))


class M30FvgEmaStrategy(ScalpingStrategyBase):
    """Fair Value Gap + EMA stacking on M30.

    Extremely selective: only trades when 4 EMAs are perfectly stacked
    AND a Fair Value Gap forms during the early London/pre-NY window
    AND a doji candle tests the FVG level.
    """

    def __init__(
        self,
        ema_periods: tuple[int, ...] = (5, 9, 13, 21),
        fvg_min_size_atr_mult: float = 0.5,
        doji_body_pct: float = 0.25,
        sl_pips: float = 10.0,
        tp_atr_mult: float = 8.0,
        atr_period: int = 14,
        max_trades_per_day: int = 2,
    ) -> None:
        super().__init__(max_trades_per_day=max_trades_per_day)
        self._ema_periods = ema_periods
        self._fvg_min_size_atr_mult = fvg_min_size_atr_mult
        self._doji_body_pct = doji_body_pct
        self._sl_pips = sl_pips
        self._tp_atr_mult = tp_atr_mult
        self._atr_period = atr_period

    def _check_ema_stacking(self, bars: pd.DataFrame) -> str | None:
        """Check if EMAs are perfectly stacked. Returns 'BUY' or 'SELL' or None."""
        emas = []
        for period in self._ema_periods:
            ema = ta.ema(bars["close"], length=period)
            if ema is None or ema.empty:
                return None
            emas.append(float(ema.iloc[-1]))

        # Bullish stacking: EMA5 > EMA9 > EMA13 > EMA21
        if all(emas[i] > emas[i + 1] for i in range(len(emas) - 1)):
            return "BUY"

        # Bearish stacking: EMA5 < EMA9 < EMA13 < EMA21
        if all(emas[i] < emas[i + 1] for i in range(len(emas) - 1)):
            return "SELL"

        return None

    def _find_fvg(
        self, bars: pd.DataFrame, direction: str, atr: float,
    ) -> float | None:
        """Find most recent Fair Value Gap in the last 10 bars.

        FVG (bullish): gap between bar[i-2].high and bar[i].low (bar[i-1] gapped up)
        FVG (bearish): gap between bar[i-2].low and bar[i].high (bar[i-1] gapped down)
        Returns the midpoint of the FVG or None.
        """
        min_size = atr * self._fvg_min_size_atr_mult

        for i in range(len(bars) - 1, max(len(bars) - 10, 2), -1):
            if direction == "BUY":
                gap_top = float(bars["low"].iloc[i])
                gap_bottom = float(bars["high"].iloc[i - 2])
                if gap_top > gap_bottom and (gap_top - gap_bottom) >= min_size:
                    return (gap_top + gap_bottom) / 2.0
            else:
                gap_top = float(bars["low"].iloc[i - 2])
                gap_bottom = float(bars["high"].iloc[i])
                if gap_top > gap_bottom and (gap_top - gap_bottom) >= min_size:
                    return (gap_top + gap_bottom) / 2.0

        return None

    def _is_doji(self, bar) -> bool:
        """Check if a bar is a doji (small body relative to range)."""
        o, c, h, l = float(bar["open"]), float(bar["close"]), float(bar["high"]), float(bar["low"])
        total_range = h - l
        if total_range <= 0:
            return False
        body = abs(c - o)
        return (body / total_range) <= self._doji_body_pct

    async def scan(
        self,
        symbol: str,
        m5_bars: pd.DataFrame,
        point_size: float = 0.01,
        as_of: datetime | None = None,
        **kwargs,
    ) -> StrategySignal | None:
        """Scan for FVG + EMA stacking setup.

        Note: This strategy is designed for M30 but accepts M5 bars
        and works on the M5 level. For true M30 behavior, pass M30 bars
        or resample M5 to M30 in the engine.
        """
        min_bars = max(max(self._ema_periods), self._atr_period) + 15
        if m5_bars is None or len(m5_bars) < min_bars:
            return None

        now = as_of or datetime.now(timezone.utc)

        # Strict time window: 8:00-11:30 UTC (3:00-6:30 AM NY)
        if not self._check_session(now, ACTIVE_HOURS):
            return None
        if not self._check_daily_limit(symbol, now):
            return None

        # 1. Check EMA stacking
        direction = self._check_ema_stacking(m5_bars)
        if direction is None:
            return None

        # 2. ATR
        atr = ta.atr(
            m5_bars["high"], m5_bars["low"], m5_bars["close"],
            length=self._atr_period,
        )
        if atr is None or atr.empty:
            return None
        curr_atr = float(atr.iloc[-1])
        if curr_atr <= 0:
            return None

        # 3. Find FVG
        fvg_level = self._find_fvg(m5_bars, direction, curr_atr)
        if fvg_level is None:
            return None

        # 4. Current bar must be a doji with wick through FVG
        curr = m5_bars.iloc[-1]
        if not self._is_doji(curr):
            return None

        curr_high = float(curr["high"])
        curr_low = float(curr["low"])
        curr_close = float(curr["close"])

        # Wick must touch FVG level
        if direction == "BUY" and curr_low > fvg_level:
            return None  # wick didn't reach FVG
        if direction == "SELL" and curr_high < fvg_level:
            return None

        # 5. Calculate SL/TP
        sl_dist = self._sl_pips * point_size
        tp_dist = curr_atr * self._tp_atr_mult

        if direction == "BUY":
            sl = curr_close - sl_dist
            tp = curr_close + tp_dist
        else:
            sl = curr_close + sl_dist
            tp = curr_close - tp_dist

        if direction == "BUY" and sl >= curr_close:
            return None
        if direction == "SELL" and sl <= curr_close:
            return None

        self._increment_daily_count(symbol, now)

        logger.info(
            "FVG+EMA [%s]: %s @ %.5f (FVG=%.2f, EMAs stacked, doji confirmed, SL=%.5f, TP=%.5f)",
            symbol, direction, curr_close, fvg_level, sl, tp,
        )

        return StrategySignal(
            symbol=symbol,
            action=direction,
            entry_price=curr_close,
            stop_loss=sl,
            take_profit=tp,
            confidence=0.80,
            reason=f"FVG+EMA {direction} (FVG@{fvg_level:.2f}, EMAs stacked, doji)",
            strategy_name="m30_fvg_ema",
        )
