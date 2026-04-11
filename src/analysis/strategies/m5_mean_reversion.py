"""M5 Mean Reversion RSI Extreme Strategy.

Catches overextension snapbacks on the 5-minute chart. Gold oscillates
around its mean on short timeframes — extreme RSI readings indicate the
price has stretched too far and is likely to snap back.

Uses EMA(20) slope filter to avoid trading mean reversion in strong trends.
ATR-dynamic SL/TP adapts to current volatility regime via ADX.

Entry: RSI(7) extreme + flat EMA(20) slope (no strong trend)
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

# EMA(20) slope threshold: if abs(slope) exceeds this, trend is too strong
# for mean reversion. Slope = (ema[-1] - ema[-5]) / ema[-5]
_EMA_SLOPE_THRESHOLD = 0.002


class M5MeanReversionStrategy:
    """RSI extreme mean reversion on M5."""

    def __init__(
        self,
        rsi_period: int = 7,
        rsi_oversold: float = 15.0,
        rsi_overbought: float = 85.0,
        tp_pips: float = 25.0,
        sl_pips: float = 18.0,
        max_trades_per_day: int = 30,
        atr_period: int = 10,
        adx_period: int = 14,
    ) -> None:
        self._rsi_period = rsi_period
        self._rsi_os = rsi_oversold
        self._rsi_ob = rsi_overbought
        self._tp_pips = tp_pips
        self._sl_pips = sl_pips
        self._max_trades = max_trades_per_day
        self._atr_period = atr_period
        self._adx_period = adx_period
        self._daily_trades: dict[str, tuple[str, int]] = {}

    async def scan(
        self,
        symbol: str,
        m5_bars: pd.DataFrame,
        point_size: float = 0.01,
        as_of: datetime | None = None,
    ) -> StrategySignal | None:
        if m5_bars is None or len(m5_bars) < 30:
            return None

        now = as_of or datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        hour = now.hour

        # Session filter: London/NY only (08:00-21:00 UTC)
        if hour < 8 or hour >= 21:
            return None

        # Daily trade limit
        entry = self._daily_trades.get(symbol, ("", 0))
        if entry[0] == today_str and entry[1] >= self._max_trades:
            return None

        close = m5_bars["close"]
        high = m5_bars["high"]
        low = m5_bars["low"]

        # Calculate RSI(7) — fast RSI for extremes
        rsi = ta.rsi(close, length=self._rsi_period)
        ema20 = ta.ema(close, length=20)

        if rsi is None or ema20 is None:
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

        curr_rsi = float(rsi.iloc[-1])
        prev_rsi = float(rsi.iloc[-2])
        curr_close = float(close.iloc[-1])
        curr_ema20 = float(ema20.iloc[-1])

        # EMA(20) slope check over last 5 bars — skip mean reversion in strong trends
        if len(ema20) > 5:
            ema_5_ago = float(ema20.iloc[-5])
            if ema_5_ago > 0:
                ema_slope = (curr_ema20 - ema_5_ago) / ema_5_ago
                if abs(ema_slope) > _EMA_SLOPE_THRESHOLD:
                    return None

        # ATR-dynamic SL/TP based on ADX regime
        sl_mult, tp_mult = ScalpingStrategyBase._atr_dynamic_sl_tp(curr_atr, curr_adx)

        # BUY: RSI drops below oversold extreme (mean reversion — buy the dip)
        if curr_rsi < self._rsi_os:
            sl = curr_close - sl_mult * curr_atr
            tp = curr_close + tp_mult * curr_atr
            self._daily_trades[symbol] = (today_str, entry[1] + 1 if entry[0] == today_str else 1)
            logger.info(
                "M5 Mean Rev [%s]: BUY @ %.5f (RSI=%.1f, extreme oversold)",
                symbol, curr_close, curr_rsi,
            )
            return StrategySignal(
                symbol=symbol, action="BUY", entry_price=curr_close,
                stop_loss=sl, take_profit=tp, confidence=0.65,
                reason=f"M5 mean reversion BUY (RSI {curr_rsi:.0f} < {self._rsi_os})",
                strategy_name="m5_mean_reversion",
            )

        # SELL: RSI rises above overbought extreme (mean reversion — sell the spike)
        if curr_rsi > self._rsi_ob:
            sl = curr_close + sl_mult * curr_atr
            tp = curr_close - tp_mult * curr_atr
            self._daily_trades[symbol] = (today_str, entry[1] + 1 if entry[0] == today_str else 1)
            logger.info(
                "M5 Mean Rev [%s]: SELL @ %.5f (RSI=%.1f, extreme overbought)",
                symbol, curr_close, curr_rsi,
            )
            return StrategySignal(
                symbol=symbol, action="SELL", entry_price=curr_close,
                stop_loss=sl, take_profit=tp, confidence=0.65,
                reason=f"M5 mean reversion SELL (RSI {curr_rsi:.0f} > {self._rsi_ob})",
                strategy_name="m5_mean_reversion",
            )

        return None
