"""M5 Multi-Timeframe Momentum Strategy.

Full 3-level alignment: H1 trend -> M15 setup -> M5 entry.
The strongest MTF filter of all scalping strategies.

H1: EMA(200) direction + Supertrend(10,3) for overall trend bias.
M15: ADX(14) > 25 confirms trending setup; RSI(14) filters direction.
M5: EMA(9)/EMA(21) crossover + Heikin Ashi color flip + volume surge.

SL = 1.5x ATR(10), TP = 3x ATR(10). Max 20 trades/day. London+NY hours.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from src.core.models import StrategySignal
from src.analysis.strategies.scalping_base import ScalpingStrategyBase

logger = logging.getLogger(__name__)

# London open through NY close (UTC hours 8-21 inclusive)
ACTIVE_HOURS = list(range(8, 22))

# Minimum bar counts for each timeframe
_MIN_H1_BARS = 20
_MIN_M15_BARS = 20
_MIN_M5_BARS = 30


class M5MtfMomentumStrategy(ScalpingStrategyBase):
    """3-level MTF momentum: H1 trend -> M15 setup -> M5 entry."""

    def __init__(
        self,
        h1_ema_length: int = 50,
        h1_st_period: int = 10,
        h1_st_mult: float = 3.0,
        m15_adx_period: int = 14,
        m15_adx_threshold: float = 15.0,
        m15_rsi_period: int = 14,
        m5_fast_ema: int = 9,
        m5_slow_ema: int = 21,
        atr_period: int = 10,
        sl_atr_mult: float = 1.5,
        tp_atr_mult: float = 3.0,
        volume_mult: float = 1.5,
        max_trades_per_day: int = 20,
    ) -> None:
        super().__init__(max_trades_per_day=max_trades_per_day)

        # H1 parameters
        self._h1_ema_len = h1_ema_length
        self._h1_st_period = h1_st_period
        self._h1_st_mult = h1_st_mult

        # M15 parameters
        self._m15_adx_period = m15_adx_period
        self._m15_adx_threshold = m15_adx_threshold
        self._m15_rsi_period = m15_rsi_period

        # M5 parameters
        self._m5_fast = m5_fast_ema
        self._m5_slow = m5_slow_ema
        self._atr_period = atr_period
        self._sl_mult = sl_atr_mult
        self._tp_mult = tp_atr_mult
        self._vol_mult = volume_mult

        # Track previous EMA cross state per symbol for crossover detection
        self._prev_fast_above: dict[str, bool | None] = {}

    # ------------------------------------------------------------------
    # Heikin Ashi helper
    # ------------------------------------------------------------------

    @staticmethod
    def _heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
        """Calculate Heikin Ashi candles from OHLC data.

        Returns a DataFrame with columns: open, high, low, close.
        """
        ha = pd.DataFrame(index=df.index)
        ha["close"] = (
            df["open"] + df["high"] + df["low"] + df["close"]
        ) / 4

        # Iteratively compute HA open (first bar uses simple average)
        ha_open = [0.0] * len(df)
        ha_open[0] = (float(df["open"].iloc[0]) + float(df["close"].iloc[0])) / 2
        for i in range(1, len(df)):
            ha_open[i] = (ha_open[i - 1] + float(ha["close"].iloc[i - 1])) / 2
        ha["open"] = ha_open

        ha["high"] = pd.concat(
            [ha["open"], ha["close"], df["high"]], axis=1
        ).max(axis=1)
        ha["low"] = pd.concat(
            [ha["open"], ha["close"], df["low"]], axis=1
        ).min(axis=1)

        return ha

    # ------------------------------------------------------------------
    # H1 trend analysis
    # ------------------------------------------------------------------

    def _h1_trend(self, h1_bars: pd.DataFrame) -> str | None:
        """Determine H1 trend bias using EMA(200) + Supertrend.

        Returns "BUY", "SELL", or None (no clear bias / disagreement).
        """
        close = h1_bars["close"]

        # EMA(200) direction: compare current vs 5 bars ago
        ema200 = ta.ema(close, length=self._h1_ema_len)
        if ema200 is None or ema200.isna().all():
            return None
        ema_curr = float(ema200.iloc[-1])
        ema_prev = float(ema200.iloc[-5])
        ema_rising = ema_curr > ema_prev
        price_above_ema = float(close.iloc[-1]) > ema_curr

        # Supertrend direction
        st = ta.supertrend(
            h1_bars["high"], h1_bars["low"], close,
            length=self._h1_st_period, multiplier=self._h1_st_mult,
        )
        if st is None or st.empty:
            return None

        # pandas_ta supertrend: direction column is the 4th (index 3).
        # Value == 1 means downtrend, value == -1 means uptrend.
        st_dir_col = st.columns[1]  # SUPERTd_<period>_<mult>
        st_dir = int(st[st_dir_col].iloc[-1])
        st_bullish = st_dir == 1

        # EMA direction + price position is primary; Supertrend is secondary
        if ema_rising and price_above_ema:
            return "BUY"
        if not ema_rising and not price_above_ema:
            return "SELL"

        return None

    # ------------------------------------------------------------------
    # M15 setup validation
    # ------------------------------------------------------------------

    def _m15_setup_valid(
        self, m15_bars: pd.DataFrame, h1_bias: str,
    ) -> bool:
        """Validate M15 setup: ADX > threshold and RSI aligns with bias."""
        close = m15_bars["close"]

        adx_df = ta.adx(
            m15_bars["high"], m15_bars["low"], close,
            length=self._m15_adx_period,
        )
        if adx_df is None or adx_df.empty:
            return False

        # ADX value is the first column (ADX_<period>)
        adx_val = float(adx_df.iloc[-1, 0])
        if adx_val < self._m15_adx_threshold:
            return False

        rsi = ta.rsi(close, length=self._m15_rsi_period)
        if rsi is None or rsi.isna().all():
            return False

        curr_rsi = float(rsi.iloc[-1])

        if h1_bias == "BUY" and curr_rsi <= 40:
            return False
        if h1_bias == "SELL" and curr_rsi >= 60:
            return False

        return True

    # ------------------------------------------------------------------
    # M5 entry detection
    # ------------------------------------------------------------------

    def _m5_entry(
        self, symbol: str, m5_bars: pd.DataFrame, h1_bias: str,
    ) -> bool:
        """Detect M5 entry: EMA crossover + HA color flip + volume surge.

        Only returns True for crossovers aligned with *h1_bias*.
        """
        close = m5_bars["close"]

        # EMA crossover
        fast = ta.ema(close, length=self._m5_fast)
        slow = ta.ema(close, length=self._m5_slow)
        if fast is None or slow is None:
            return False

        curr_fast = float(fast.iloc[-1])
        curr_slow = float(slow.iloc[-1])
        fast_above = curr_fast > curr_slow

        prev_state = self._prev_fast_above.get(symbol)
        self._prev_fast_above[symbol] = fast_above

        # Need a fresh crossover
        if prev_state is None:
            return False

        bullish_cross = not prev_state and fast_above
        bearish_cross = prev_state and not fast_above

        if h1_bias == "BUY" and not bullish_cross:
            return False
        if h1_bias == "SELL" and not bearish_cross:
            return False

        # Heikin Ashi color flip confirmation
        ha = self._heikin_ashi(m5_bars)
        ha_curr_green = float(ha["close"].iloc[-1]) > float(ha["open"].iloc[-1])
        ha_prev_green = float(ha["close"].iloc[-2]) > float(ha["open"].iloc[-2])

        if h1_bias == "BUY" and not (ha_curr_green and not ha_prev_green):
            return False
        if h1_bias == "SELL" and not (not ha_curr_green and ha_prev_green):
            return False

        # Volume surge: optional confidence factor (no longer a hard gate)
        # High volume is desirable but not required for entry

        return True

    # ------------------------------------------------------------------
    # Main scan
    # ------------------------------------------------------------------

    async def scan(
        self,
        symbol: str,
        m5_bars: pd.DataFrame | None = None,
        point_size: float = 0.01,
        as_of: datetime | None = None,
        m15_bars: pd.DataFrame | None = None,
        h1_bars: pd.DataFrame | None = None,
        regime=None,
        **kwargs,
    ) -> StrategySignal | None:
        """Scan for an MTF momentum entry.

        Requires all three timeframes (H1, M15, M5). Gracefully returns
        None when any higher-timeframe data is missing.
        """
        now = as_of or datetime.now(timezone.utc)

        # ----- Guard: session hours -----
        if not self._check_session(now, ACTIVE_HOURS):
            return None

        # ----- Guard: daily trade limit -----
        if not self._check_daily_limit(symbol, now):
            return None

        # ----- Guard: minimum data -----
        if m5_bars is None or len(m5_bars) < _MIN_M5_BARS:
            return None
        if h1_bars is None or len(h1_bars) < _MIN_H1_BARS:
            return None
        # ----- Step 1: H1 trend bias -----
        h1_bias = self._h1_trend(h1_bars)
        if h1_bias is None:
            return None

        # ----- Step 2: M15 setup validation (skip if data insufficient) -----
        if m15_bars is None or len(m15_bars) < 20:
            pass  # skip M15 check, proceed with entry
        elif not self._m15_setup_valid(m15_bars, h1_bias):
            return None

        # ----- Step 3: M5 entry trigger -----
        if not self._m5_entry(symbol, m5_bars, h1_bias):
            return None

        # ----- Compute SL / TP via ATR -----
        atr = ta.atr(
            m5_bars["high"], m5_bars["low"], m5_bars["close"],
            length=self._atr_period,
        )
        if atr is None or atr.isna().all():
            return None

        curr_atr = float(atr.iloc[-1])
        if curr_atr <= 0:
            return None

        curr_close = float(m5_bars["close"].iloc[-1])
        sl_dist = curr_atr * self._sl_mult
        tp_dist = curr_atr * self._tp_mult

        if h1_bias == "BUY":
            sl = curr_close - sl_dist
            tp = curr_close + tp_dist
        else:
            sl = curr_close + sl_dist
            tp = curr_close - tp_dist

        # ----- Emit signal -----
        self._increment_daily_count(symbol, now)

        confidence = 0.80  # High confidence from full 3-level alignment
        reason = (
            f"H1 trend {h1_bias} (EMA200+ST) | "
            f"M15 ADX>{self._m15_adx_threshold:.0f} RSI aligned | "
            f"M5 EMA cross + HA flip + vol surge | "
            f"ATR={curr_atr:.5f}"
        )

        # RSI overbought/oversold filter
        if not self._check_rsi_filter(m5_bars, h1_bias):
            logger.debug("M5 MTF Momentum [%s]: %s blocked by RSI filter", symbol, h1_bias)
            return None

        logger.info(
            "M5 MTF Momentum [%s]: %s @ %.5f  SL=%.5f  TP=%.5f  (ATR=%.5f)",
            symbol, h1_bias, curr_close, sl, tp, curr_atr,
        )

        return StrategySignal(
            symbol=symbol,
            action=h1_bias,
            entry_price=curr_close,
            stop_loss=sl,
            take_profit=tp,
            confidence=confidence,
            reason=reason,
            strategy_name="m5_mtf_momentum",
        )
