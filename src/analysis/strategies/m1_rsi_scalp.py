"""M1 Fast RSI + Stochastic Confluence Scalp.

RSI(5) extreme + Stochastic(5,3,1) crossover + EMA trend alignment.
MTF: M5 EMA(50) slope flat for MR, M15 BB position confluence.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from src.analysis.strategies.scalping_base import ScalpingStrategyBase
from src.core.models import StrategySignal

logger = logging.getLogger(__name__)

# Overlap hours 13-17 get priority; extended session 8-22
_PRIORITY_HOURS = list(range(13, 18))
_SESSION_HOURS = list(range(8, 23))


class M1RsiScalpStrategy(ScalpingStrategyBase):
    """Fast RSI + Stochastic confluence scalp on M1.

    Entry:
        BUY  - RSI(5) < 15 oversold + Stoch %K crosses above %D in OS zone
               + EMA(9) > EMA(21) trend alignment + 2 consecutive confirming candles.
        SELL - RSI(5) > 85 overbought + Stoch %K crosses below %D in OB zone
               + EMA(9) < EMA(21) trend alignment + 2 consecutive confirming candles.

    Anti-whipsaw: require 2 consecutive confirming candles before entry.
    SL: ATR(7) x 1.0, TP: ATR(7) x 2.0 (1:2 RR).
    Session: overlap priority 13-17, extended 8-22. Max 50 trades/day.
    MTF: M5 EMA(50) slope must be flat (mean-reversion context).
    """

    def __init__(
        self,
        rsi_period: int = 5,
        rsi_ob: float = 85.0,
        rsi_os: float = 15.0,
        stoch_k: int = 5,
        stoch_d: int = 3,
        stoch_smooth: int = 1,
        fast_ema: int = 9,
        slow_ema: int = 21,
        atr_period: int = 7,
        sl_atr_mult: float = 1.0,
        tp_atr_mult: float = 2.0,
        max_trades_per_day: int = 50,
    ) -> None:
        super().__init__(max_trades_per_day=max_trades_per_day)
        self._rsi_period = rsi_period
        self._rsi_ob = rsi_ob
        self._rsi_os = rsi_os
        self._stoch_k = stoch_k
        self._stoch_d = stoch_d
        self._stoch_smooth = stoch_smooth
        self._fast_ema = fast_ema
        self._slow_ema = slow_ema
        self._atr_period = atr_period
        self._sl_mult = sl_atr_mult
        self._tp_mult = tp_atr_mult

    # ------------------------------------------------------------------
    # MTF filters
    # ------------------------------------------------------------------

    @staticmethod
    def _m5_ema_slope_flat(m5_bars: pd.DataFrame | None) -> bool:
        """M5 EMA(50) slope must be flat (mean-reversion regime).

        "Flat" = absolute slope over last 3 bars < 0.1 * ATR(14).
        """
        if m5_bars is None or len(m5_bars) < 55:
            return True  # skip filter if no data

        ema50 = ta.ema(m5_bars["close"], length=50)
        atr14 = ta.atr(
            m5_bars["high"], m5_bars["low"], m5_bars["close"], length=14,
        )
        if ema50 is None or atr14 is None:
            return True
        if ema50.isna().iloc[-1] or atr14.isna().iloc[-1]:
            return True

        slope = abs(float(ema50.iloc[-1]) - float(ema50.iloc[-3]))
        threshold = 0.1 * float(atr14.iloc[-1])
        return slope < threshold

    @staticmethod
    def _m15_bb_confluence(m15_bars: pd.DataFrame | None, direction: str) -> bool:
        """M15 Bollinger Band position confluence.

        BUY  - price near lower band (close < mid-band).
        SELL - price near upper band (close > mid-band).
        """
        if m15_bars is None or len(m15_bars) < 21:
            return True

        bb = ta.bbands(m15_bars["close"], length=20, std=2.0)
        if bb is None or bb.empty:
            return True

        close = float(m15_bars["close"].iloc[-1])
        mid = float(bb.iloc[-1, 1])  # BBM_20_2.0

        if direction == "BUY":
            return close < mid
        return close > mid

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _two_consecutive_confirming(
        m1_bars: pd.DataFrame, direction: str,
    ) -> bool:
        """Require 2 consecutive candles confirming the direction.

        BUY  - last 2 closes each higher than their open.
        SELL - last 2 closes each lower than their open.
        """
        if len(m1_bars) < 3:
            return False

        c1_open = float(m1_bars["open"].iloc[-2])
        c1_close = float(m1_bars["close"].iloc[-2])
        c2_open = float(m1_bars["open"].iloc[-1])
        c2_close = float(m1_bars["close"].iloc[-1])

        if direction == "BUY":
            return c1_close > c1_open and c2_close > c2_open
        return c1_close < c1_open and c2_close < c2_open

    # ------------------------------------------------------------------
    # Core scan
    # ------------------------------------------------------------------

    async def scan(
        self,
        symbol: str,
        m1_bars: pd.DataFrame,
        point_size: float = 0.01,
        as_of: datetime | None = None,
        m5_bars: pd.DataFrame | None = None,
        m15_bars: pd.DataFrame | None = None,
        **kwargs,
    ) -> StrategySignal | None:
        """Scan M1 bars for RSI + Stochastic confluence entry."""
        min_len = max(self._slow_ema, self._rsi_period, self._stoch_k) + 10
        if m1_bars is None or len(m1_bars) < min_len:
            return None

        now = as_of or datetime.now(timezone.utc)

        if not self._check_session(now, _SESSION_HOURS):
            return None
        if not self._check_daily_limit(symbol, now):
            return None

        # --- Indicators ---
        rsi = ta.rsi(m1_bars["close"], length=self._rsi_period)
        stoch = ta.stoch(
            m1_bars["high"], m1_bars["low"], m1_bars["close"],
            k=self._stoch_k, d=self._stoch_d, smooth_k=self._stoch_smooth,
        )
        ema_fast = ta.ema(m1_bars["close"], length=self._fast_ema)
        ema_slow = ta.ema(m1_bars["close"], length=self._slow_ema)
        atr = ta.atr(
            m1_bars["high"], m1_bars["low"], m1_bars["close"],
            length=self._atr_period,
        )

        if rsi is None or stoch is None or ema_fast is None or ema_slow is None:
            return None
        if atr is None or atr.isna().iloc[-1]:
            return None

        curr_rsi = float(rsi.iloc[-1])
        prev_rsi = float(rsi.iloc[-2])
        curr_atr = float(atr.iloc[-1])
        if curr_atr <= 0:
            return None

        # Stochastic columns: STOCHk_5_3_1, STOCHd_5_3_1
        stoch_k_col = stoch.columns[0]
        stoch_d_col = stoch.columns[1]
        curr_k = float(stoch[stoch_k_col].iloc[-1])
        prev_k = float(stoch[stoch_k_col].iloc[-2])
        curr_d = float(stoch[stoch_d_col].iloc[-1])
        prev_d = float(stoch[stoch_d_col].iloc[-2])

        curr_ema_f = float(ema_fast.iloc[-1])
        curr_ema_s = float(ema_slow.iloc[-1])
        curr_close = float(m1_bars["close"].iloc[-1])

        sl_dist = self._sl_mult * curr_atr
        tp_dist = self._tp_mult * curr_atr

        # Confidence boost during overlap hours
        base_conf = 0.60 if now.hour in _PRIORITY_HOURS else 0.55

        # --- BUY (oversold reversal) ---
        rsi_os = curr_rsi < self._rsi_os or prev_rsi < self._rsi_os
        stoch_cross_up = prev_k < prev_d and curr_k > curr_d and curr_k < 25
        ema_bullish = curr_ema_f > curr_ema_s

        if rsi_os and stoch_cross_up and ema_bullish:
            if not self._two_consecutive_confirming(m1_bars, "BUY"):
                return None
            if not self._m5_ema_slope_flat(m5_bars):
                return None
            if not self._m15_bb_confluence(m15_bars, "BUY"):
                return None

            sl = curr_close - sl_dist
            tp = curr_close + tp_dist
            self._increment_daily_count(symbol, now)

            logger.info(
                "RSI Scalp [%s]: BUY @ %.5f  (RSI=%.1f, Stoch K cross D in OS)",
                symbol, curr_close, curr_rsi,
            )
            return StrategySignal(
                symbol=symbol,
                action="BUY",
                entry_price=curr_close,
                stop_loss=sl,
                take_profit=tp,
                confidence=base_conf,
                reason=f"RSI({self._rsi_period}) OS + Stoch cross-up + EMA alignment",
                strategy_name="m1_rsi_scalp",
            )

        # --- SELL (overbought reversal) ---
        rsi_ob = curr_rsi > self._rsi_ob or prev_rsi > self._rsi_ob
        stoch_cross_dn = prev_k > prev_d and curr_k < curr_d and curr_k > 75
        ema_bearish = curr_ema_f < curr_ema_s

        if rsi_ob and stoch_cross_dn and ema_bearish:
            if not self._two_consecutive_confirming(m1_bars, "SELL"):
                return None
            if not self._m5_ema_slope_flat(m5_bars):
                return None
            if not self._m15_bb_confluence(m15_bars, "SELL"):
                return None

            sl = curr_close + sl_dist
            tp = curr_close - tp_dist
            self._increment_daily_count(symbol, now)

            logger.info(
                "RSI Scalp [%s]: SELL @ %.5f  (RSI=%.1f, Stoch K cross D in OB)",
                symbol, curr_close, curr_rsi,
            )
            return StrategySignal(
                symbol=symbol,
                action="SELL",
                entry_price=curr_close,
                stop_loss=sl,
                take_profit=tp,
                confidence=base_conf,
                reason=f"RSI({self._rsi_period}) OB + Stoch cross-down + EMA alignment",
                strategy_name="m1_rsi_scalp",
            )

        return None
