"""M1 Heikin Ashi + EMA Channel Momentum Strategy.

HA smoothed candles + MA(55) high/low channel for entries.
MTF: M5 EMA(200) direction + M5 ADX(14) > 20 + M15 RSI not extreme.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from src.analysis.strategies.scalping_base import ScalpingStrategyBase
from src.core.models import StrategySignal

logger = logging.getLogger(__name__)

_SESSION_HOURS = list(range(9, 21))  # 9-20 UTC inclusive


def _heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    """Convert OHLC DataFrame to Heikin Ashi candles.

    Returns a DataFrame with columns: ha_open, ha_high, ha_low, ha_close.
    """
    ha = pd.DataFrame(index=df.index)
    ha["ha_close"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4.0

    ha_open = [float(df["open"].iloc[0])]
    for i in range(1, len(df)):
        ha_open.append((ha_open[i - 1] + float(ha["ha_close"].iloc[i - 1])) / 2.0)
    ha["ha_open"] = ha_open

    ha["ha_high"] = pd.concat(
        [df["high"], ha["ha_open"], ha["ha_close"]], axis=1
    ).max(axis=1)
    ha["ha_low"] = pd.concat(
        [df["low"], ha["ha_open"], ha["ha_close"]], axis=1
    ).min(axis=1)

    return ha


def _is_strong_green(ha_row: pd.Series) -> bool:
    """HA green candle with no (or negligible) lower wick."""
    if ha_row["ha_close"] <= ha_row["ha_open"]:
        return False
    body = abs(ha_row["ha_close"] - ha_row["ha_open"])
    lower_wick = min(ha_row["ha_open"], ha_row["ha_close"]) - ha_row["ha_low"]
    return body > 0 and lower_wick < 0.05 * body


def _is_strong_red(ha_row: pd.Series) -> bool:
    """HA red candle with no (or negligible) upper wick."""
    if ha_row["ha_close"] >= ha_row["ha_open"]:
        return False
    body = abs(ha_row["ha_close"] - ha_row["ha_open"])
    upper_wick = ha_row["ha_high"] - max(ha_row["ha_open"], ha_row["ha_close"])
    return body > 0 and upper_wick < 0.05 * body


def _is_indecision(ha_row: pd.Series) -> bool:
    """HA candle showing indecision (color flip / doji-like)."""
    body = abs(ha_row["ha_close"] - ha_row["ha_open"])
    total = ha_row["ha_high"] - ha_row["ha_low"]
    if total <= 0:
        return True
    return body / total < 0.25


class M1HeikinAshiMomentumStrategy(ScalpingStrategyBase):
    """Heikin Ashi smoothed candles + MA(55) channel on M1.

    Entry:
        BUY  - HA strong green (no lower wick) + close breaks above upper channel.
        SELL - HA strong red   (no upper wick) + close breaks below lower channel.

    Exit:
        HA colour flip / indecision candle.

    SL: 1.5x ATR(7), TP: 2x ATR(7).
    Session: 9-20 UTC, max 40 trades/day.
    MTF: M5 EMA(200) direction must agree, M5 ADX(14) > 20.
    """

    def __init__(
        self,
        channel_period: int = 55,
        atr_period: int = 7,
        sl_atr_mult: float = 1.5,
        tp_atr_mult: float = 2.0,
        max_trades_per_day: int = 40,
    ) -> None:
        super().__init__(max_trades_per_day=max_trades_per_day)
        self._channel_period = channel_period
        self._atr_period = atr_period
        self._sl_mult = sl_atr_mult
        self._tp_mult = tp_atr_mult

    # ------------------------------------------------------------------
    # MTF filters
    # ------------------------------------------------------------------

    @staticmethod
    def _m5_trend_agrees(m5_bars: pd.DataFrame, direction: str) -> bool:
        """M5 EMA(200) direction must agree + ADX(14) > 20."""
        if m5_bars is None or len(m5_bars) < 201:
            return True  # skip filter when data unavailable

        ema200 = ta.ema(m5_bars["close"], length=200)
        if ema200 is None or ema200.isna().all():
            return True

        curr_ema = float(ema200.iloc[-1])
        prev_ema = float(ema200.iloc[-2])

        adx_df = ta.adx(m5_bars["high"], m5_bars["low"], m5_bars["close"], length=14)
        if adx_df is None or adx_df.empty:
            return True
        adx_val = float(adx_df.iloc[-1, 0])  # ADX_14
        if adx_val < 20:
            return False

        if direction == "BUY":
            return curr_ema > prev_ema
        return curr_ema < prev_ema

    @staticmethod
    def _m15_rsi_not_extreme(m15_bars: pd.DataFrame | None) -> bool:
        """M15 RSI(14) should not be at extreme (< 20 or > 80)."""
        if m15_bars is None or len(m15_bars) < 15:
            return True

        rsi = ta.rsi(m15_bars["close"], length=14)
        if rsi is None or rsi.isna().all():
            return True

        val = float(rsi.iloc[-1])
        return 20 <= val <= 80

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
        """Scan M1 bars for Heikin Ashi momentum entry."""
        if m1_bars is None or len(m1_bars) < self._channel_period + 5:
            return None

        now = as_of or datetime.now(timezone.utc)

        if not self._check_session(now, _SESSION_HOURS):
            return None
        if not self._check_daily_limit(symbol, now):
            return None

        # --- Indicators ---
        ha = _heikin_ashi(m1_bars)
        upper_channel = ta.sma(m1_bars["high"], length=self._channel_period)
        lower_channel = ta.sma(m1_bars["low"], length=self._channel_period)
        atr = ta.atr(
            m1_bars["high"], m1_bars["low"], m1_bars["close"],
            length=self._atr_period,
        )

        if upper_channel is None or lower_channel is None or atr is None:
            return None
        if upper_channel.isna().iloc[-1] or lower_channel.isna().iloc[-1]:
            return None

        curr_atr = float(atr.iloc[-1])
        if curr_atr <= 0:
            return None

        curr_upper = float(upper_channel.iloc[-1])
        curr_lower = float(lower_channel.iloc[-1])
        curr_close = float(m1_bars["close"].iloc[-1])
        ha_curr = ha.iloc[-1]

        sl_dist = self._sl_mult * curr_atr
        tp_dist = self._tp_mult * curr_atr

        # --- BUY ---
        if _is_strong_green(ha_curr) and curr_close > curr_upper:
            if not self._m5_trend_agrees(m5_bars, "BUY"):
                return None
            if not self._m15_rsi_not_extreme(m15_bars):
                return None

            sl = curr_close - sl_dist
            tp = curr_close + tp_dist
            self._increment_daily_count(symbol, now)

            logger.info(
                "HA Momentum [%s]: BUY @ %.5f  (HA green + above upper channel)",
                symbol, curr_close,
            )
            return StrategySignal(
                symbol=symbol,
                action="BUY",
                entry_price=curr_close,
                stop_loss=sl,
                take_profit=tp,
                confidence=0.60,
                reason="HA green no-wick + price above MA(55) high channel",
                strategy_name="m1_heikin_ashi_momentum",
            )

        # --- SELL ---
        if _is_strong_red(ha_curr) and curr_close < curr_lower:
            if not self._m5_trend_agrees(m5_bars, "SELL"):
                return None
            if not self._m15_rsi_not_extreme(m15_bars):
                return None

            sl = curr_close + sl_dist
            tp = curr_close - tp_dist
            self._increment_daily_count(symbol, now)

            logger.info(
                "HA Momentum [%s]: SELL @ %.5f  (HA red + below lower channel)",
                symbol, curr_close,
            )
            return StrategySignal(
                symbol=symbol,
                action="SELL",
                entry_price=curr_close,
                stop_loss=sl,
                take_profit=tp,
                confidence=0.60,
                reason="HA red no-wick + price below MA(55) low channel",
                strategy_name="m1_heikin_ashi_momentum",
            )

        return None
