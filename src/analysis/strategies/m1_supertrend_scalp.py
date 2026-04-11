"""M1 Supertrend + VWAP Scalp.

Supertrend(7, 2.0) direction + VWAP position for high-probability entries.
MTF: M5 Supertrend must agree, H1 regime not CHOPPY.
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

_SESSION_HOURS = list(range(8, 23))  # 8-22 UTC inclusive


def _calc_vwap(df: pd.DataFrame) -> pd.Series | None:
    """Calculate VWAP from M1 OHLCV data.

    Uses typical price * volume cumulative sum / cumulative volume.
    Falls back to equal-weight typical price if volume is missing or zero.
    """
    tp = (df["high"] + df["low"] + df["close"]) / 3.0

    if "volume" not in df.columns or df["volume"].sum() == 0:
        # No volume data — use rolling mean of typical price as proxy
        return tp.rolling(window=20, min_periods=1).mean()

    cum_vol = df["volume"].cumsum()
    cum_tp_vol = (tp * df["volume"]).cumsum()

    vwap = cum_tp_vol / cum_vol.replace(0, np.nan)
    return vwap


def _vwap_sigma(df: pd.DataFrame, vwap: pd.Series, mult: float = 1.5) -> float:
    """Return distance from VWAP to mult-sigma band (upper).

    Uses rolling std of (close - vwap) over 20 bars.
    """
    diff = df["close"] - vwap
    std = diff.rolling(window=20, min_periods=5).std()
    if std is None or std.isna().iloc[-1]:
        return 0.0
    return mult * float(std.iloc[-1])


class M1SupertrendScalpStrategy(ScalpingStrategyBase):
    """Supertrend(7, 2.0) + VWAP scalp on M1.

    Entry:
        BUY  - Supertrend flips bullish + price above VWAP
               + volume > 1.2x avg(20).
        SELL - Supertrend flips bearish + price below VWAP
               + volume > 1.2x avg(20).

    SL: Supertrend value (dynamic).
    TP: Distance from VWAP to 1.5-sigma band.
    Session: 8-22 UTC, max 40 trades/day.
    MTF: M5 Supertrend(10, 3) must agree with M1 direction.
    """

    def __init__(
        self,
        st_length: int = 7,
        st_multiplier: float = 2.0,
        vol_avg_period: int = 20,
        vol_threshold: float = 1.2,
        vwap_sigma_mult: float = 1.5,
        max_trades_per_day: int = 40,
    ) -> None:
        super().__init__(max_trades_per_day=max_trades_per_day)
        self._st_length = st_length
        self._st_mult = st_multiplier
        self._vol_avg = vol_avg_period
        self._vol_thresh = vol_threshold
        self._vwap_sigma_mult = vwap_sigma_mult

    # ------------------------------------------------------------------
    # MTF filters
    # ------------------------------------------------------------------

    @staticmethod
    def _m5_supertrend_agrees(
        m5_bars: pd.DataFrame | None, direction: str,
    ) -> bool:
        """M5 Supertrend(10, 3) must agree with M1 direction."""
        if m5_bars is None or len(m5_bars) < 15:
            return True  # skip filter when data unavailable

        st = ta.supertrend(
            m5_bars["high"], m5_bars["low"], m5_bars["close"],
            length=10, multiplier=3.0,
        )
        if st is None or st.empty:
            return True

        # Direction column: SUPERTd_10_3.0  (1 = bullish, -1 = bearish)
        dir_col = [c for c in st.columns if c.startswith("SUPERTd")]
        if not dir_col:
            return True
        m5_dir = int(st[dir_col[0]].iloc[-1])

        if direction == "BUY":
            return m5_dir == 1
        return m5_dir == -1

    @staticmethod
    def _h1_regime_not_choppy(
        h1_bars: pd.DataFrame | None, regime: str | None,
    ) -> bool:
        """H1 regime must not be CHOPPY.

        If an explicit regime string is provided, use it directly.
        Otherwise, approximate via ADX(14) < 15 => choppy.
        """
        if regime is not None:
            regime_name = regime.name if hasattr(regime, "name") else str(regime).upper()
            return regime_name != "CHOPPY"

        if h1_bars is None or len(h1_bars) < 20:
            return True

        adx_df = ta.adx(h1_bars["high"], h1_bars["low"], h1_bars["close"], length=14)
        if adx_df is None or adx_df.empty:
            return True
        adx_val = float(adx_df.iloc[-1, 0])  # ADX_14
        return adx_val >= 15

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _volume_above_avg(
        m1_bars: pd.DataFrame, period: int, threshold: float,
    ) -> bool:
        """Return True if current volume > threshold * avg(period).

        Skips check if volume column is missing or all zero.
        """
        if "volume" not in m1_bars.columns:
            return True
        vol = m1_bars["volume"]
        if vol.sum() == 0:
            return True
        avg_vol = vol.rolling(window=period, min_periods=5).mean()
        if avg_vol is None or avg_vol.isna().iloc[-1]:
            return True
        return float(vol.iloc[-1]) > threshold * float(avg_vol.iloc[-1])

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
        h1_bars: pd.DataFrame | None = None,
        regime: str | None = None,
        **kwargs,
    ) -> StrategySignal | None:
        """Scan M1 bars for Supertrend + VWAP entry."""
        min_len = max(self._st_length + 5, self._vol_avg + 5, 25)
        if m1_bars is None or len(m1_bars) < min_len:
            return None

        now = as_of or datetime.now(timezone.utc)

        if not self._check_session(now, _SESSION_HOURS):
            return None
        if not self._check_daily_limit(symbol, now):
            return None

        # --- Supertrend ---
        st = ta.supertrend(
            m1_bars["high"], m1_bars["low"], m1_bars["close"],
            length=self._st_length, multiplier=self._st_mult,
        )
        if st is None or st.empty:
            return None

        # Columns: SUPERT_7_2.0, SUPERTd_7_2.0, SUPERTl_7_2.0, SUPERTs_7_2.0
        dir_col = [c for c in st.columns if c.startswith("SUPERTd")]
        val_col = [c for c in st.columns if c.startswith("SUPERT_")]
        if not dir_col or not val_col:
            return None

        curr_dir = int(st[dir_col[0]].iloc[-1])
        prev_dir = int(st[dir_col[0]].iloc[-2])
        st_value = float(st[val_col[0]].iloc[-1])

        # Require a flip (not already in the same direction)
        flipped_bull = prev_dir == -1 and curr_dir == 1
        flipped_bear = prev_dir == 1 and curr_dir == -1

        if not flipped_bull and not flipped_bear:
            return None

        # --- VWAP ---
        vwap = _calc_vwap(m1_bars)
        if vwap is None or vwap.isna().iloc[-1]:
            return None

        curr_close = float(m1_bars["close"].iloc[-1])
        curr_vwap = float(vwap.iloc[-1])

        # --- Volume filter ---
        if not self._volume_above_avg(m1_bars, self._vol_avg, self._vol_thresh):
            return None

        # --- TP from VWAP sigma ---
        sigma_dist = _vwap_sigma(m1_bars, vwap, self._vwap_sigma_mult)
        if sigma_dist <= 0:
            # Fallback: 1.5x distance to VWAP
            sigma_dist = abs(curr_close - curr_vwap) * 1.5
        if sigma_dist <= 0:
            return None

        # --- BUY ---
        if flipped_bull and curr_close > curr_vwap:
            if not self._m5_supertrend_agrees(m5_bars, "BUY"):
                return None
            if not self._h1_regime_not_choppy(h1_bars, regime):
                return None

            sl = st_value  # Supertrend line as dynamic SL
            tp = curr_close + sigma_dist
            self._increment_daily_count(symbol, now)

            logger.info(
                "Supertrend Scalp [%s]: BUY @ %.5f  (ST flip bull + above VWAP)",
                symbol, curr_close,
            )
            return StrategySignal(
                symbol=symbol,
                action="BUY",
                entry_price=curr_close,
                stop_loss=sl,
                take_profit=tp,
                confidence=0.60,
                reason="Supertrend flip bullish + price above VWAP + volume spike",
                strategy_name="m1_supertrend_scalp",
            )

        # --- SELL ---
        if flipped_bear and curr_close < curr_vwap:
            if not self._m5_supertrend_agrees(m5_bars, "SELL"):
                return None
            if not self._h1_regime_not_choppy(h1_bars, regime):
                return None

            sl = st_value  # Supertrend line as dynamic SL
            tp = curr_close - sigma_dist
            self._increment_daily_count(symbol, now)

            logger.info(
                "Supertrend Scalp [%s]: SELL @ %.5f  (ST flip bear + below VWAP)",
                symbol, curr_close,
            )
            return StrategySignal(
                symbol=symbol,
                action="SELL",
                entry_price=curr_close,
                stop_loss=sl,
                take_profit=tp,
                confidence=0.60,
                reason="Supertrend flip bearish + price below VWAP + volume spike",
                strategy_name="m1_supertrend_scalp",
            )

        return None
