"""M1 EMA Micro Pullback Strategy.

Ultra-short-term pullback scalping on the 1-minute chart. Uses EMA(5)/EMA(10)
crossover to identify micro-trends, then enters on pullbacks to the slow EMA.

ATR-dynamic SL/TP adapts to current volatility regime via ADX.
Only trades during peak liquidity (London/NY sessions) when spreads are tight.

Entry: EMA(5) cross + pullback to EMA(10) + bounce candle confirmation
Exit: ATR-dynamic SL/TP scaled by ADX regime
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from src.analysis.strategies.scalping_base import ScalpingStrategyBase
from src.core.models import StrategySignal

logger = logging.getLogger(__name__)


class M1EmaMicroStrategy:
    """EMA(5/10) micro pullback on M1."""

    def __init__(
        self,
        fast_ema: int = 5,
        slow_ema: int = 10,
        tp_pips: float = 6.0,
        sl_buffer_pips: float = 3.0,
        max_trades_per_day: int = 40,
        atr_period: int = 7,
        adx_period: int = 14,
    ) -> None:
        self._fast_ema = fast_ema
        self._slow_ema = slow_ema
        self._tp_pips = tp_pips
        self._sl_buffer = sl_buffer_pips
        self._max_trades = max_trades_per_day
        self._atr_period = atr_period
        self._adx_period = adx_period
        self._daily_trades: dict[str, tuple[str, int]] = {}
        # Track state: waiting for pullback after crossover
        self._bullish_cross: dict[str, bool] = {}
        self._bearish_cross: dict[str, bool] = {}

    async def scan(
        self,
        symbol: str,
        m1_bars: pd.DataFrame,
        point_size: float = 0.01,
        as_of: datetime | None = None,
    ) -> StrategySignal | None:
        if m1_bars is None or len(m1_bars) < 20:
            return None

        now = as_of or datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        hour = now.hour

        # Session filter: London/NY peak hours only (09:00-20:00 UTC)
        if hour < 9 or hour >= 20:
            return None

        # Daily limit
        entry = self._daily_trades.get(symbol, ("", 0))
        if entry[0] == today_str and entry[1] >= self._max_trades:
            return None

        close = m1_bars["close"]
        high = m1_bars["high"]
        low = m1_bars["low"]

        # Calculate EMAs
        fast = ta.ema(close, length=self._fast_ema)
        slow = ta.ema(close, length=self._slow_ema)

        if fast is None or slow is None:
            return None

        # Calculate ATR and ADX for dynamic SL/TP
        atr = ta.atr(high, low, close, length=self._atr_period)
        adx_df = ta.adx(high, low, close, length=self._adx_period)

        if atr is None or adx_df is None:
            return None

        curr_atr = float(atr.iloc[-1])
        curr_adx = float(adx_df.iloc[-1, 0])  # ADX column

        if curr_atr <= 0:
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
        bullish_cross = prev_fast <= prev_slow and curr_fast > curr_slow
        bearish_cross = prev_fast >= prev_slow and curr_fast < curr_slow

        if bullish_cross:
            self._bullish_cross[symbol] = True
            self._bearish_cross[symbol] = False
        elif bearish_cross:
            self._bearish_cross[symbol] = True
            self._bullish_cross[symbol] = False

        # ATR-dynamic SL/TP based on ADX regime
        sl_mult, tp_mult = ScalpingStrategyBase._atr_dynamic_sl_tp(curr_atr, curr_adx)

        # BUY: After bullish cross, price pulled back to slow EMA, now bouncing
        if self._bullish_cross.get(symbol, False):
            # Check for pullback: close touched or went below slow EMA
            touched_slow = curr_low <= curr_slow
            # Check for bounce: current close above slow EMA and above prev close
            bouncing = curr_close > curr_slow and curr_close > prev_close

            if touched_slow and bouncing:
                sl = curr_close - sl_mult * curr_atr
                tp = curr_close + tp_mult * curr_atr

                self._bullish_cross[symbol] = False
                self._daily_trades[symbol] = (today_str, entry[1] + 1 if entry[0] == today_str else 1)

                logger.info(
                    "M1 EMA Micro [%s]: BUY @ %.5f (pullback bounce off EMA10)",
                    symbol, curr_close,
                )
                return StrategySignal(
                    symbol=symbol, action="BUY", entry_price=curr_close,
                    stop_loss=sl, take_profit=tp, confidence=0.55,
                    reason="M1 EMA micro pullback BUY",
                    strategy_name="m1_ema_micro",
                )

        # SELL: After bearish cross, price pulled back to slow EMA, now dropping
        if self._bearish_cross.get(symbol, False):
            touched_slow = curr_high >= curr_slow
            dropping = curr_close < curr_slow and curr_close < prev_close

            if touched_slow and dropping:
                sl = curr_close + sl_mult * curr_atr
                tp = curr_close - tp_mult * curr_atr

                self._bearish_cross[symbol] = False
                self._daily_trades[symbol] = (today_str, entry[1] + 1 if entry[0] == today_str else 1)

                logger.info(
                    "M1 EMA Micro [%s]: SELL @ %.5f (pullback drop from EMA10)",
                    symbol, curr_close,
                )
                return StrategySignal(
                    symbol=symbol, action="SELL", entry_price=curr_close,
                    stop_loss=sl, take_profit=tp, confidence=0.55,
                    reason="M1 EMA micro pullback SELL",
                    strategy_name="m1_ema_micro",
                )

        return None
