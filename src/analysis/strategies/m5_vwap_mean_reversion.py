"""M5 VWAP Mean Reversion Strategy.

Trades snapbacks to VWAP when price overextends to 2-sigma bands.
VWAP resets at session boundaries. Best during London/NY overlap.

MTF filter: M15 EMA(50) slope must be flat (mean reversion only works
when higher TF is not strongly trending).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pandas_ta as ta

from src.analysis.strategies.scalping_base import ScalpingStrategyBase
from src.core.models import StrategySignal

logger = logging.getLogger(__name__)

# London/NY active hours
ACTIVE_HOURS = list(range(8, 22))


class M5VwapMeanReversionStrategy(ScalpingStrategyBase):
    """VWAP + 2-sigma band mean reversion on M5."""

    def __init__(
        self,
        vwap_std_mult: float = 2.0,
        rsi_period: int = 7,
        atr_period: int = 10,
        adx_period: int = 14,
        m15_ema_period: int = 50,
        m15_slope_threshold: float = 0.0005,  # max EMA slope for MR to be valid
        max_trades_per_day: int = 30,
    ) -> None:
        super().__init__(max_trades_per_day=max_trades_per_day)
        self._vwap_std = vwap_std_mult
        self._rsi_period = rsi_period
        self._atr_period = atr_period
        self._adx_period = adx_period
        self._m15_ema_period = m15_ema_period
        self._m15_slope_threshold = m15_slope_threshold

    # ------------------------------------------------------------------
    # MTF filter
    # ------------------------------------------------------------------

    def _m15_is_trending(self, m15_bars: pd.DataFrame | None) -> bool:
        """Return True if M15 EMA slope exceeds threshold (too trendy for MR)."""
        if m15_bars is None or len(m15_bars) < self._m15_ema_period + 5:
            return False  # No data -> allow trade (conservative)

        m15_ema = ta.ema(m15_bars["close"], length=self._m15_ema_period)
        if m15_ema is None or len(m15_ema) < 5:
            return False

        slope = (
            abs(float(m15_ema.iloc[-1]) - float(m15_ema.iloc[-5]))
            / float(m15_ema.iloc[-5])
        )
        return slope > self._m15_slope_threshold

    # ------------------------------------------------------------------
    # VWAP calculation
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_vwap_bands(
        high: np.ndarray,
        low: np.ndarray,
        close: np.ndarray,
        volume: np.ndarray,
        std_mult: float,
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Return (vwap, upper_band, lower_band) arrays."""
        typical_price = (close + high + low) / 3.0
        cum_tp_vol = np.cumsum(typical_price * volume)
        cum_vol = np.cumsum(volume)
        # Avoid division by zero
        cum_vol_safe = np.where(cum_vol == 0, 1.0, cum_vol)
        vwap = cum_tp_vol / cum_vol_safe

        sq_diff = (typical_price - vwap) ** 2
        cum_sq_diff = np.cumsum(sq_diff * volume)
        variance = cum_sq_diff / cum_vol_safe
        std = np.sqrt(variance)

        upper_band = vwap + std_mult * std
        lower_band = vwap - std_mult * std
        return vwap, upper_band, lower_band

    # ------------------------------------------------------------------
    # Confirmation indicators
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_confirmations(
        high: np.ndarray,
        low: np.ndarray,
        close: np.ndarray,
        rsi_period: int,
        atr_period: int,
        adx_period: int,
    ) -> tuple[float, float, float] | None:
        """Return (rsi, atr, adx) for the latest bar, or None on failure."""
        s_high = pd.Series(high)
        s_low = pd.Series(low)
        s_close = pd.Series(close)

        rsi = ta.rsi(s_close, length=rsi_period)
        atr = ta.atr(s_high, s_low, s_close, length=atr_period)
        adx_df = ta.adx(s_high, s_low, s_close, length=adx_period)

        if rsi is None or atr is None or adx_df is None:
            return None

        curr_rsi = float(rsi.iloc[-1])
        curr_atr = float(atr.iloc[-1])
        curr_adx = float(adx_df.iloc[:, 0].iloc[-1])  # ADX column

        if curr_atr <= 0:
            return None

        return curr_rsi, curr_atr, curr_adx

    # ------------------------------------------------------------------
    # Signal generation
    # ------------------------------------------------------------------

    def _build_signal(
        self,
        symbol: str,
        action: str,
        curr_close: float,
        curr_vwap: float,
        sl_mult: float,
        tp_mult: float,
        curr_atr: float,
        curr_rsi: float,
        point_size: float,
        now: datetime,
    ) -> StrategySignal:
        """Construct a StrategySignal for the given direction."""
        if action == "BUY":
            sl = curr_close - sl_mult * curr_atr
            tp = curr_vwap
            if tp - curr_close < 0.5 * point_size:
                tp = curr_close + tp_mult * curr_atr
        else:
            sl = curr_close + sl_mult * curr_atr
            tp = curr_vwap
            if curr_close - tp < 0.5 * point_size:
                tp = curr_close - tp_mult * curr_atr

        self._increment_daily_count(symbol, now)
        return StrategySignal(
            symbol=symbol,
            action=action,
            entry_price=curr_close,
            stop_loss=sl,
            take_profit=tp,
            confidence=0.65,
            reason=f"M5 VWAP MR {action} (RSI={curr_rsi:.0f}, {'below' if action == 'BUY' else 'above'} 2\u03c3)",
            strategy_name="m5_vwap_mean_reversion",
        )

    # ------------------------------------------------------------------
    # Main scan
    # ------------------------------------------------------------------

    async def scan(  # type: ignore[override]
        self,
        symbol: str,
        m5_bars: pd.DataFrame,
        point_size: float = 0.01,
        as_of: datetime | None = None,
        m15_bars: pd.DataFrame | None = None,
        **kwargs,
    ) -> StrategySignal | None:
        """Scan M5 bars for a VWAP mean-reversion setup.

        Parameters
        ----------
        symbol:     Instrument ticker (e.g. "XAUUSD").
        m5_bars:    DataFrame with columns: open, high, low, close, tick_volume.
        point_size: Minimum price increment for TP sanity check.
        as_of:      Evaluation timestamp (defaults to utcnow).
        m15_bars:   Optional M15 DataFrame for MTF slope filter.
        """
        if m5_bars is None or len(m5_bars) < 50:
            return None

        now = as_of or datetime.now(timezone.utc)

        if not self._check_session(now, ACTIVE_HOURS):
            return None
        if not self._check_daily_limit(symbol, now):
            return None

        # MTF filter: reject if M15 is trending
        if self._m15_is_trending(m15_bars):
            return None

        close = m5_bars["close"].values
        high = m5_bars["high"].values
        low = m5_bars["low"].values
        volume = m5_bars["tick_volume"].values.astype(float)

        # VWAP + bands
        vwap, upper_band, lower_band = self._compute_vwap_bands(
            high, low, close, volume, self._vwap_std,
        )

        curr_close = float(close[-1])
        curr_vwap = float(vwap[-1])
        curr_upper = float(upper_band[-1])
        curr_lower = float(lower_band[-1])

        # Confirmation indicators
        conf = self._compute_confirmations(
            high, low, close,
            self._rsi_period, self._atr_period, self._adx_period,
        )
        if conf is None:
            return None
        curr_rsi, curr_atr, curr_adx = conf

        # Dynamic SL/TP from base class
        sl_mult, tp_mult = self._atr_dynamic_sl_tp(curr_atr, curr_adx)

        # Candlestick pattern (optional confirmation)
        pattern = ""
        if len(m5_bars) >= 2:
            o = float(m5_bars["open"].iloc[-1])
            h_val = float(high[-1])
            l_val = float(low[-1])
            c = curr_close
            po = float(m5_bars["open"].iloc[-2])
            ph = float(high[-2])
            pl = float(low[-2])
            pc = float(close[-2])
            pattern = self.detect_candle_pattern(o, h_val, l_val, c, po, ph, pl, pc)

        # BUY: price at/below lower band + RSI oversold
        if curr_close <= curr_lower and curr_rsi < 35:
            if pattern in ("pin_bar_bull", "engulfing_bull", ""):
                return self._build_signal(
                    symbol, "BUY", curr_close, curr_vwap,
                    sl_mult, tp_mult, curr_atr, curr_rsi, point_size, now,
                )

        # SELL: price at/above upper band + RSI overbought
        if curr_close >= curr_upper and curr_rsi > 65:
            if pattern in ("pin_bar_bear", "engulfing_bear", ""):
                return self._build_signal(
                    symbol, "SELL", curr_close, curr_vwap,
                    sl_mult, tp_mult, curr_atr, curr_rsi, point_size, now,
                )

        return None
