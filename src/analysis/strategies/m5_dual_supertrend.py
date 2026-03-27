"""M5 Dual Supertrend Scalping Strategy.

Uses two Supertrend indicators (fast and slow) for high-confidence entries.
Both must agree on direction before entry is allowed. The fast Supertrend
serves as a dynamic trailing stop-loss.

Fast Supertrend: ATR(7), multiplier=2.0
Slow Supertrend: ATR(14), multiplier=3.0

Entry conditions:
  - Both Supertrends agree on direction (both bullish or both bearish)
  - Candle close confirms direction (close > fast ST for BUY, close < for SELL)
  - ADX(14) > 20 (sufficient trend strength)

MTF filter: H1 EMA(50) direction must agree with entry direction.
            H1 regime must NOT be CHOPPY.

SL: Fast Supertrend value (dynamic trailing SL)
TP: ATR-dynamic from base class (_atr_dynamic_sl_tp)

Session: London + NY (hours 8-22 UTC). Max 25 trades/day.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from src.analysis.strategies.scalping_base import ScalpingStrategyBase
from src.core.models import StrategySignal

logger = logging.getLogger(__name__)

ACTIVE_HOURS = list(range(8, 22))


class M5DualSupertrendStrategy(ScalpingStrategyBase):
    """Dual Supertrend trend-following scalper on M5."""

    def __init__(
        self,
        fast_period: int = 7,
        fast_mult: float = 2.0,
        slow_period: int = 14,
        slow_mult: float = 3.0,
        adx_period: int = 14,
        adx_threshold: float = 20.0,
        atr_period: int = 10,
        h1_ema_period: int = 50,
        max_trades_per_day: int = 25,
    ) -> None:
        super().__init__(max_trades_per_day=max_trades_per_day)
        self._fast_period = fast_period
        self._fast_mult = fast_mult
        self._slow_period = slow_period
        self._slow_mult = slow_mult
        self._adx_period = adx_period
        self._adx_threshold = adx_threshold
        self._atr_period = atr_period
        self._h1_ema_period = h1_ema_period

    # ------------------------------------------------------------------
    # Core scan
    # ------------------------------------------------------------------

    async def scan(
        self,
        symbol: str,
        m5_bars: pd.DataFrame,
        point_size: float = 0.01,
        as_of: datetime | None = None,
        h1_bars: pd.DataFrame | None = None,
        regime=None,
        **kwargs,
    ) -> StrategySignal | None:
        """Scan M5 bars for dual Supertrend entry signals.

        Args:
            symbol: Trading instrument (e.g. "XAUUSD").
            m5_bars: DataFrame with OHLCV columns for the M5 timeframe.
            point_size: Minimum price increment for the instrument.
            as_of: Evaluation timestamp (defaults to now UTC).
            h1_bars: Optional H1 DataFrame for MTF EMA filter.
            regime: Optional MarketRegime enum for choppy-market filter.

        Returns:
            StrategySignal when entry conditions are met, otherwise None.
        """
        # --- minimum data guard ---
        min_bars = max(self._slow_period, self._adx_period, self._atr_period) + 20
        if m5_bars is None or len(m5_bars) < min_bars:
            return None

        now = as_of or datetime.now(timezone.utc)

        # 1. Session filter
        if not self._check_session(now, ACTIVE_HOURS):
            return None

        # 2. Daily trade limit
        if not self._check_daily_limit(symbol, now):
            return None

        high = m5_bars["high"]
        low = m5_bars["low"]
        close = m5_bars["close"]

        # 3. Calculate fast and slow Supertrend
        fast_st = ta.supertrend(high, low, close, length=self._fast_period, multiplier=self._fast_mult)
        slow_st = ta.supertrend(high, low, close, length=self._slow_period, multiplier=self._slow_mult)

        if fast_st is None or fast_st.empty or slow_st is None or slow_st.empty:
            logger.debug("Dual ST [%s]: Supertrend computation returned None", symbol)
            return None

        # pandas_ta supertrend columns: SUPERT_{len}_{mult}, SUPERTd_{len}_{mult}, ...
        fast_dir_col = f"SUPERTd_{self._fast_period}_{self._fast_mult}"
        fast_val_col = f"SUPERT_{self._fast_period}_{self._fast_mult}"
        slow_dir_col = f"SUPERTd_{self._slow_period}_{self._slow_mult}"

        # Validate expected columns exist
        if fast_dir_col not in fast_st.columns or fast_val_col not in fast_st.columns:
            logger.debug("Dual ST [%s]: Missing fast ST columns in %s", symbol, list(fast_st.columns))
            return None
        if slow_dir_col not in slow_st.columns:
            logger.debug("Dual ST [%s]: Missing slow ST columns in %s", symbol, list(slow_st.columns))
            return None

        curr_fast_dir = int(fast_st[fast_dir_col].iloc[-1])
        curr_slow_dir = int(slow_st[slow_dir_col].iloc[-1])
        curr_fast_val = float(fast_st[fast_val_col].iloc[-1])
        curr_close = float(close.iloc[-1])

        # 4. Both Supertrends must agree on direction
        if curr_fast_dir != curr_slow_dir:
            return None

        is_bullish = curr_fast_dir == 1
        is_bearish = curr_fast_dir == -1

        # Candle close must confirm direction
        if is_bullish and curr_close <= curr_fast_val:
            return None
        if is_bearish and curr_close >= curr_fast_val:
            return None

        # 5. ADX trend-strength filter
        adx_df = ta.adx(high, low, close, length=self._adx_period)
        if adx_df is None or adx_df.empty:
            return None

        adx_col = f"ADX_{self._adx_period}"
        if adx_col not in adx_df.columns:
            return None

        curr_adx = float(adx_df[adx_col].iloc[-1])
        if curr_adx < self._adx_threshold:
            return None

        # 6. H1 multi-timeframe filter
        if not self._h1_filter_passes(h1_bars, regime, is_bullish):
            return None

        # 7. ATR-dynamic SL / TP
        atr = ta.atr(high, low, close, length=self._atr_period)
        if atr is None or atr.empty:
            return None

        curr_atr = float(atr.iloc[-1])
        if curr_atr <= 0:
            return None

        sl_mult, tp_mult = self._atr_dynamic_sl_tp(curr_atr, curr_adx)

        # SL is at the fast Supertrend value (dynamic trailing stop)
        # TP uses ATR-dynamic multiplier
        if is_bullish:
            sl = curr_fast_val
            tp = curr_close + curr_atr * tp_mult
            action = "BUY"
        else:
            sl = curr_fast_val
            tp = curr_close - curr_atr * tp_mult
            action = "SELL"

        # Sanity: SL must be on the correct side of entry
        if action == "BUY" and sl >= curr_close:
            return None
        if action == "SELL" and sl <= curr_close:
            return None

        # Confidence scales with ADX strength (0.60 base, up to 0.80)
        confidence = min(0.60 + (curr_adx - self._adx_threshold) * 0.005, 0.80)

        self._increment_daily_count(symbol, now)

        logger.info(
            "Dual ST [%s]: %s @ %.5f (fast_dir=%d, slow_dir=%d, ADX=%.1f, "
            "SL=%.5f, TP=%.5f)",
            symbol, action, curr_close, curr_fast_dir, curr_slow_dir,
            curr_adx, sl, tp,
        )

        return StrategySignal(
            symbol=symbol,
            action=action,
            entry_price=curr_close,
            stop_loss=sl,
            take_profit=tp,
            confidence=round(confidence, 2),
            reason=(
                f"Dual ST {action} (fast={curr_fast_dir}, slow={curr_slow_dir}, "
                f"ADX={curr_adx:.0f})"
            ),
            strategy_name="m5_dual_supertrend",
        )

    # ------------------------------------------------------------------
    # H1 MTF filter
    # ------------------------------------------------------------------

    def _h1_filter_passes(
        self,
        h1_bars: pd.DataFrame | None,
        regime,
        is_bullish: bool,
    ) -> bool:
        """Check H1 EMA direction agreement and reject CHOPPY regime.

        Returns True when the filter passes (trade allowed), False otherwise.
        If h1_bars is not provided the filter is skipped (pass-through).
        """
        # Regime filter: reject CHOPPY regardless of H1 bar availability
        if regime is not None:
            regime_name = regime.value if hasattr(regime, "value") else str(regime)
            if regime_name == "choppy":
                logger.debug("Dual ST: H1 regime is CHOPPY -- skipping")
                return False

        # If no H1 data supplied, skip the EMA directional filter
        if h1_bars is None or len(h1_bars) < self._h1_ema_period + 5:
            return True

        h1_ema = ta.ema(h1_bars["close"], length=self._h1_ema_period)
        if h1_ema is None or len(h1_ema) < 6:
            return True

        # EMA direction over the last 5 H1 bars
        h1_ema_now = float(h1_ema.iloc[-1])
        h1_ema_prev = float(h1_ema.iloc[-5])
        h1_trending_up = h1_ema_now > h1_ema_prev

        # Direction must agree with entry
        if is_bullish and not h1_trending_up:
            logger.debug("Dual ST: H1 EMA(50) trending DOWN -- bullish entry rejected")
            return False
        if not is_bullish and h1_trending_up:
            logger.debug("Dual ST: H1 EMA(50) trending UP -- bearish entry rejected")
            return False

        return True
