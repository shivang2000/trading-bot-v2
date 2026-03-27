"""M5 Stochastic RSI + ADX Filter Strategy.

StochRSI crossovers at OB/OS zones with ADX trending filter.
Session-specific %K periods for adaptive sensitivity.

MTF filter: H1 regime must be trending (not CHOPPY/RANGING).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from src.core.models import StrategySignal
from src.analysis.strategies.scalping_base import ScalpingStrategyBase

logger = logging.getLogger(__name__)

# Session-specific hours (UTC)
ASIAN_HOURS = list(range(0, 8))
LONDON_HOURS = list(range(8, 13))
NY_HOURS = list(range(13, 22))
ACTIVE_HOURS = list(range(0, 22))

# H1 regimes that allow entry
_ALLOWED_REGIMES = frozenset({
    "TRENDING_UP",
    "TRENDING_DOWN",
    "VOLATILE_TREND",
})


class M5StochRsiAdxStrategy(ScalpingStrategyBase):
    """StochRSI crossover scalping with ADX trend filter on M5.

    Entry rules:
        BUY  - %K crosses above %D while both below ``os_level`` (oversold)
        SELL - %K crosses below %D while both above ``ob_level`` (overbought)

    Filters:
        1. ADX(14) > ``adx_threshold`` (trending market)
        2. H1 regime in TRENDING_UP / TRENDING_DOWN / VOLATILE_TREND
        3. RSI(14) anti-trap: not in extreme counter-zone

    Session-specific %K smoothing adapts sensitivity to volatility:
        Asian  (00-08 UTC) - slower (%K=21) to avoid noise
        London (08-13 UTC) - balanced (%K=14)
        NY     (13-22 UTC) - faster (%K=9) for momentum
    """

    def __init__(
        self,
        stochrsi_length: int = 14,
        k_period_asian: int = 21,
        k_period_london: int = 14,
        k_period_ny: int = 9,
        d_period: int = 3,
        adx_period: int = 14,
        adx_threshold: float = 25.0,
        ob_level: float = 75.0,
        os_level: float = 25.0,
        atr_period: int = 10,
        rsi_period: int = 14,
        max_trades_per_day: int = 30,
    ) -> None:
        super().__init__(max_trades_per_day=max_trades_per_day)
        self._stochrsi_len = stochrsi_length
        self._k_asian = k_period_asian
        self._k_london = k_period_london
        self._k_ny = k_period_ny
        self._d_period = d_period
        self._adx_period = adx_period
        self._adx_threshold = adx_threshold
        self._ob = ob_level
        self._os = os_level
        self._atr_period = atr_period
        self._rsi_period = rsi_period

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_k_period(self, hour: int) -> int:
        """Return session-specific %K smoothing period."""
        if hour in ASIAN_HOURS:
            return self._k_asian
        if hour in LONDON_HOURS:
            return self._k_london
        return self._k_ny

    @staticmethod
    def _regime_allowed(regime: str | None) -> bool:
        """Return True if H1 regime permits entry."""
        if regime is None:
            return True  # no regime data available — allow
        regime_name = regime.name if hasattr(regime, "name") else str(regime).upper()
        return regime_name in _ALLOWED_REGIMES

    # ------------------------------------------------------------------
    # Core scan
    # ------------------------------------------------------------------

    async def scan(  # noqa: C901
        self,
        symbol: str,
        m5_bars: pd.DataFrame,
        point_size: float = 0.01,
        as_of: datetime | None = None,
        h1_bars: pd.DataFrame | None = None,
        regime: str | None = None,
        **kwargs,
    ) -> StrategySignal | None:
        """Scan M5 bars for StochRSI + ADX entry signals.

        Args:
            symbol: Instrument symbol (e.g. "XAUUSD").
            m5_bars: M5 OHLCV DataFrame (needs >= 60 bars).
            point_size: Symbol point size for pip calculation.
            as_of: Override current time (for backtesting).
            h1_bars: Optional H1 bars for MTF regime check.
            regime: Optional pre-computed H1 regime string.
            **kwargs: Ignored — keeps caller flexibility.
        """
        if m5_bars is None or len(m5_bars) < 60:
            return None

        now = as_of or datetime.now(timezone.utc)

        # 1. Session filter
        if not self._check_session(now, ACTIVE_HOURS):
            return None

        # 2. Daily trade limit
        if not self._check_daily_limit(symbol, now):
            return None

        # 3. H1 regime filter — skip CHOPPY / RANGING
        if not self._regime_allowed(regime):
            logger.debug(
                "StochRSI+ADX [%s]: skipping — regime=%s", symbol, regime,
            )
            return None

        close = m5_bars["close"]
        high = m5_bars["high"]
        low = m5_bars["low"]

        # 4. Session-specific K period
        k_period = self._get_k_period(now.hour)

        # 5. Compute StochRSI with session-specific K smoothing.
        #    ta.stochrsi returns cols: STOCHRSIk_..., STOCHRSId_...
        #    We compute RSI first, then apply ta.stoch() on the RSI
        #    series so we can control the K smoothing period per session.
        rsi_series = ta.rsi(close, length=self._stochrsi_len)
        if rsi_series is None or rsi_series.dropna().empty:
            return None

        stoch_of_rsi = ta.stoch(
            high=rsi_series,
            low=rsi_series,
            close=rsi_series,
            k=k_period,
            d=self._d_period,
            smooth_k=k_period,
        )
        if stoch_of_rsi is None or stoch_of_rsi.dropna().empty:
            return None

        # Columns are STOCHk_{k}_{d}_{smooth_k} and STOCHd_{k}_{d}_{smooth_k}
        k_col = stoch_of_rsi.columns[0]
        d_col = stoch_of_rsi.columns[1]

        curr_k = float(stoch_of_rsi[k_col].iloc[-1])
        prev_k = float(stoch_of_rsi[k_col].iloc[-2])
        curr_d = float(stoch_of_rsi[d_col].iloc[-1])
        prev_d = float(stoch_of_rsi[d_col].iloc[-2])

        if pd.isna(curr_k) or pd.isna(curr_d) or pd.isna(prev_k) or pd.isna(prev_d):
            return None

        # 6. ADX filter
        adx_df = ta.adx(high, low, close, length=self._adx_period)
        if adx_df is None or adx_df.dropna().empty:
            return None

        adx_col = [c for c in adx_df.columns if c.startswith("ADX_")]
        if not adx_col:
            return None

        curr_adx = float(adx_df[adx_col[0]].iloc[-1])
        if pd.isna(curr_adx) or curr_adx < self._adx_threshold:
            return None

        # 7. Detect crossover at OB/OS zones
        k_crossed_up = prev_k <= prev_d and curr_k > curr_d
        k_crossed_down = prev_k >= prev_d and curr_k < curr_d

        oversold_zone = curr_k < self._os and curr_d < self._os
        overbought_zone = curr_k > self._ob and curr_d > self._ob

        # 8. Anti-trap: plain RSI(14) must not be in the extreme counter-zone
        rsi_14 = ta.rsi(close, length=self._rsi_period)
        if rsi_14 is None:
            return None

        curr_rsi = float(rsi_14.iloc[-1])
        if pd.isna(curr_rsi):
            return None

        # 9. ATR for dynamic SL/TP
        atr_series = ta.atr(high, low, close, length=self._atr_period)
        if atr_series is None or atr_series.dropna().empty:
            return None

        curr_atr = float(atr_series.iloc[-1])
        if pd.isna(curr_atr) or curr_atr <= 0:
            return None

        sl_mult, tp_mult = self._atr_dynamic_sl_tp(curr_atr, curr_adx)
        curr_close = float(close.iloc[-1])

        # --- BUY signal ---
        if k_crossed_up and oversold_zone:
            # Anti-trap: RSI(14) must not be extremely overbought (counter-zone)
            if curr_rsi > 70:
                logger.debug(
                    "StochRSI+ADX [%s]: BUY blocked — RSI anti-trap (%.1f)",
                    symbol, curr_rsi,
                )
                return None

            sl = curr_close - sl_mult * curr_atr
            tp = curr_close + tp_mult * curr_atr

            self._increment_daily_count(symbol, now)
            logger.info(
                "StochRSI+ADX [%s]: BUY @ %.5f (K=%.1f D=%.1f ADX=%.1f RSI=%.1f session_k=%d)",
                symbol, curr_close, curr_k, curr_d, curr_adx, curr_rsi, k_period,
            )
            return StrategySignal(
                symbol=symbol,
                action="BUY",
                entry_price=curr_close,
                stop_loss=sl,
                take_profit=tp,
                confidence=0.65,
                reason=(
                    f"StochRSI OS cross (K={curr_k:.0f} D={curr_d:.0f}) "
                    f"ADX={curr_adx:.0f} RSI={curr_rsi:.0f}"
                ),
                strategy_name="m5_stochrsi_adx",
            )

        # --- SELL signal ---
        if k_crossed_down and overbought_zone:
            # Anti-trap: RSI(14) must not be extremely oversold (counter-zone)
            if curr_rsi < 30:
                logger.debug(
                    "StochRSI+ADX [%s]: SELL blocked — RSI anti-trap (%.1f)",
                    symbol, curr_rsi,
                )
                return None

            sl = curr_close + sl_mult * curr_atr
            tp = curr_close - tp_mult * curr_atr

            self._increment_daily_count(symbol, now)
            logger.info(
                "StochRSI+ADX [%s]: SELL @ %.5f (K=%.1f D=%.1f ADX=%.1f RSI=%.1f session_k=%d)",
                symbol, curr_close, curr_k, curr_d, curr_adx, curr_rsi, k_period,
            )
            return StrategySignal(
                symbol=symbol,
                action="SELL",
                entry_price=curr_close,
                stop_loss=sl,
                take_profit=tp,
                confidence=0.65,
                reason=(
                    f"StochRSI OB cross (K={curr_k:.0f} D={curr_d:.0f}) "
                    f"ADX={curr_adx:.0f} RSI={curr_rsi:.0f}"
                ),
                strategy_name="m5_stochrsi_adx",
            )

        return None
