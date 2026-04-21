"""M5 EMA 8/33 Dominance Candle Strategy.

Uses EMA(8) and EMA(33) alignment for trend direction, then enters
on a "dominance candle" — a large-body candle with small wicks that
shows strong conviction in the trend direction.

Entry: When dominance candle closes in trend direction with EMA alignment.
SL: Below dominance candle low (BUY) or above high (SELL).
TP: ATR-dynamic (2x ATR from entry).
Session: 7:00-21:00 UTC. Max 25 trades/day.
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


class M5Ema833Strategy(ScalpingStrategyBase):
    """EMA 8/33 with dominance candle entry on M5."""

    def __init__(
        self,
        fast_ema: int = 8,
        slow_ema: int = 33,
        dominance_body_pct: float = 0.70,
        min_body_atr_mult: float = 0.5,
        tp_atr_mult: float = 2.0,
        atr_period: int = 14,
        use_rsi_filter: bool = True,
        max_trades_per_day: int = 25,
    ) -> None:
        super().__init__(max_trades_per_day=max_trades_per_day)
        self._fast_ema = fast_ema
        self._slow_ema = slow_ema
        self._dominance_body_pct = dominance_body_pct
        self._min_body_atr_mult = min_body_atr_mult
        self._tp_atr_mult = tp_atr_mult
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
        """Scan M5 bars for EMA 8/33 dominance candle setup."""
        min_bars = max(self._slow_ema, self._atr_period) + 5
        if m5_bars is None or len(m5_bars) < min_bars:
            return None

        now = as_of or datetime.now(timezone.utc)

        if not self._check_session(now, ACTIVE_HOURS):
            return None
        if not self._check_daily_limit(symbol, now):
            return None

        close = m5_bars["close"]
        high = m5_bars["high"]
        low = m5_bars["low"]

        # Calculate EMAs
        ema_fast = ta.ema(close, length=self._fast_ema)
        ema_slow = ta.ema(close, length=self._slow_ema)
        if ema_fast is None or ema_slow is None:
            return None

        fast_val = float(ema_fast.iloc[-1])
        slow_val = float(ema_slow.iloc[-1])

        # EMA alignment — determine trend
        if fast_val > slow_val:
            trend = "BUY"
        elif fast_val < slow_val:
            trend = "SELL"
        else:
            return None

        # Current bar analysis
        curr = m5_bars.iloc[-1]
        curr_open = float(curr["open"])
        curr_close = float(curr["close"])
        curr_high = float(curr["high"])
        curr_low = float(curr["low"])
        curr_range = curr_high - curr_low

        if curr_range <= 0:
            return None

        body = abs(curr_close - curr_open)
        body_pct = body / curr_range

        # Dominance candle check: large body relative to range
        if body_pct < self._dominance_body_pct:
            return None

        # ATR for minimum size and TP
        atr = ta.atr(high, low, close, length=self._atr_period)
        if atr is None or atr.empty:
            return None
        curr_atr = float(atr.iloc[-1])
        if curr_atr <= 0:
            return None

        # Body must be significant (not tiny candle)
        if body < self._min_body_atr_mult * curr_atr:
            return None

        # Candle direction must match trend
        candle_bullish = curr_close > curr_open
        candle_bearish = curr_close < curr_open

        if trend == "BUY" and not candle_bullish:
            return None
        if trend == "SELL" and not candle_bearish:
            return None

        # Price must be on correct side of EMAs
        if trend == "BUY" and curr_close < fast_val:
            return None
        if trend == "SELL" and curr_close > fast_val:
            return None

        # RSI filter
        if self._use_rsi_filter and not self._check_rsi_filter(m5_bars, trend):
            return None

        # Entry, SL, TP
        entry = curr_close
        if trend == "BUY":
            sl = curr_low - point_size
            tp = entry + curr_atr * self._tp_atr_mult
        else:
            sl = curr_high + point_size
            tp = entry - curr_atr * self._tp_atr_mult

        if trend == "BUY" and sl >= entry:
            return None
        if trend == "SELL" and sl <= entry:
            return None

        self._increment_daily_count(symbol, now)

        confidence = 0.65

        logger.info(
            "EMA 833 [%s]: %s @ %.5f (EMA8=%.2f, EMA33=%.2f, body%%=%.0f%%, SL=%.5f, TP=%.5f)",
            symbol, trend, entry, fast_val, slow_val, body_pct * 100, sl, tp,
        )

        return StrategySignal(
            symbol=symbol,
            action=trend,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            confidence=confidence,
            reason=f"EMA833 {trend} dominance (body={body_pct:.0%}, EMA8={'>' if trend == 'BUY' else '<'}EMA33)",
            strategy_name="m5_ema_833",
        )
