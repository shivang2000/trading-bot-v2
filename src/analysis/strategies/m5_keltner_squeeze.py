"""M5 Keltner Channel + Bollinger Band Squeeze Strategy.

Detects volatility compression (BB inside KC) and trades the breakout.
MACD histogram confirms direction.

Squeeze detection: BB(20, 2.0) contracts inside KC (EMA(20) +/- 1.5x ATR(10)).
Entry: BB expands back outside KC (squeeze release) + MACD histogram confirms.
MTF filter: M15 ADX(14) > 20 and EMA(21) slope confirms breakout direction.
SL: KC middle line (EMA 20). TP: 1.5x KC channel width.
Session: 8-22 UTC. Max 15 trades/day.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from src.core.models import StrategySignal
from src.analysis.strategies.scalping_base import ScalpingStrategyBase

logger = logging.getLogger(__name__)

ACTIVE_HOURS = list(range(0, 24))


class M5KeltnerSqueezeStrategy(ScalpingStrategyBase):
    """Keltner Channel + Bollinger Band squeeze breakout on M5.

    Trades the volatility expansion when BB exits KC after a compression phase.
    MACD histogram confirms breakout direction; M15 ADX + EMA slope filters
    ensure the breakout aligns with a trending higher-timeframe context.
    """

    def __init__(
        self,
        bb_length: int = 20,
        bb_std: float = 2.0,
        kc_length: int = 20,
        kc_atr_mult: float = 1.5,
        kc_atr_period: int = 10,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        adx_period: int = 14,
        m15_adx_threshold: float = 20.0,
        m15_ema_period: int = 21,
        tp_channel_mult: float = 1.5,
        max_trades_per_day: int = 15,
    ) -> None:
        super().__init__(max_trades_per_day=max_trades_per_day)

        # Bollinger Bands parameters
        self._bb_length = bb_length
        self._bb_std = bb_std

        # Keltner Channel parameters
        self._kc_length = kc_length
        self._kc_atr_mult = kc_atr_mult
        self._kc_atr_period = kc_atr_period

        # MACD parameters
        self._macd_fast = macd_fast
        self._macd_slow = macd_slow
        self._macd_signal = macd_signal

        # MTF filter parameters
        self._adx_period = adx_period
        self._m15_adx_threshold = m15_adx_threshold
        self._m15_ema_period = m15_ema_period

        # TP sizing
        self._tp_channel_mult = tp_channel_mult

        # Squeeze state tracking per symbol
        self._in_squeeze: dict[str, bool] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_squeeze(
        bb_upper: float, bb_lower: float,
        kc_upper: float, kc_lower: float,
    ) -> bool:
        """BB inside KC means volatility is compressed (squeeze)."""
        return bb_upper < kc_upper and bb_lower > kc_lower

    @staticmethod
    def _m15_confirms(
        m15_bars: pd.DataFrame,
        adx_period: int,
        adx_threshold: float,
        ema_period: int,
        direction: str,
    ) -> bool:
        """Check M15 ADX > threshold and EMA slope agrees with direction."""
        if m15_bars is None or len(m15_bars) < max(adx_period + 5, ema_period + 3):
            return False

        adx_df = ta.adx(
            m15_bars["high"], m15_bars["low"], m15_bars["close"],
            length=adx_period,
        )
        if adx_df is None or adx_df.empty:
            return False

        # ADX column is the first one (ADX_{period})
        adx_val = float(adx_df.iloc[-1, 0])
        if adx_val < adx_threshold:
            return False

        ema = ta.ema(m15_bars["close"], length=ema_period)
        if ema is None or len(ema) < 3:
            return False

        slope = float(ema.iloc[-1]) - float(ema.iloc[-3])

        if direction == "BUY" and slope <= 0:
            return False
        if direction == "SELL" and slope >= 0:
            return False

        return True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def scan(
        self,
        symbol: str,
        m5_bars: pd.DataFrame,
        point_size: float = 0.01,
        as_of: datetime | None = None,
        m15_bars: pd.DataFrame | None = None,
        **kwargs,
    ) -> StrategySignal | None:
        """Scan M5 bars for a Keltner squeeze release signal.

        Parameters
        ----------
        symbol:
            Instrument symbol (e.g. ``"XAUUSD"``).
        m5_bars:
            M5 OHLCV DataFrame with columns ``open, high, low, close``.
        point_size:
            Pip/point value for the instrument.
        as_of:
            Current timestamp (defaults to ``utcnow``).
        m15_bars:
            Optional M15 OHLCV DataFrame for multi-timeframe filtering.
        """
        min_bars = max(self._bb_length, self._kc_length, self._macd_slow) + 15
        if m5_bars is None or len(m5_bars) < min_bars:
            return None

        now = as_of or datetime.now(timezone.utc)

        # 1. Session filter
        if not self._check_session(now, ACTIVE_HOURS):
            return None

        # 2. Daily trade limit
        if not self._check_daily_limit(symbol, now):
            return None

        close = m5_bars["close"]
        high = m5_bars["high"]
        low = m5_bars["low"]

        # ----------------------------------------------------------
        # 3. Calculate Bollinger Bands
        # ----------------------------------------------------------
        bb = ta.bbands(close, length=self._bb_length, std=self._bb_std)
        if bb is None or bb.empty:
            return None

        bb_lower = float(bb.iloc[-1, 0])
        bb_middle = float(bb.iloc[-1, 1])
        bb_upper = float(bb.iloc[-1, 2])

        # ----------------------------------------------------------
        # 4. Calculate Keltner Channel: EMA +/- mult * ATR
        # ----------------------------------------------------------
        kc_ema = ta.ema(close, length=self._kc_length)
        kc_atr = ta.atr(high, low, close, length=self._kc_atr_period)

        if kc_ema is None or kc_atr is None:
            return None

        kc_mid = float(kc_ema.iloc[-1])
        atr_val = float(kc_atr.iloc[-1])

        if atr_val <= 0:
            return None

        kc_upper = kc_mid + self._kc_atr_mult * atr_val
        kc_lower = kc_mid - self._kc_atr_mult * atr_val
        kc_width = kc_upper - kc_lower

        if kc_width <= 0:
            return None

        # ----------------------------------------------------------
        # 5. Squeeze detection
        # ----------------------------------------------------------
        currently_squeezed = self._is_squeeze(bb_upper, bb_lower, kc_upper, kc_lower)
        was_in_squeeze = self._in_squeeze.get(symbol, False)

        # Update state
        self._in_squeeze[symbol] = currently_squeezed

        # Only trade on squeeze RELEASE: was in squeeze, now bands expanded
        if not was_in_squeeze or currently_squeezed:
            return None

        # ----------------------------------------------------------
        # 6. MACD histogram for direction confirmation
        # ----------------------------------------------------------
        macd_df = ta.macd(
            close,
            fast=self._macd_fast,
            slow=self._macd_slow,
            signal=self._macd_signal,
        )
        if macd_df is None or macd_df.empty:
            return None

        # Histogram is the third column (MACDh_{fast}_{slow}_{signal})
        histogram = float(macd_df.iloc[-1, 2])

        if histogram == 0:
            return None

        direction = "BUY" if histogram > 0 else "SELL"

        # ----------------------------------------------------------
        # 7. M15 MTF filter: ADX > 20 and EMA slope confirms direction
        # ----------------------------------------------------------
        if m15_bars is not None:
            if not self._m15_confirms(
                m15_bars,
                self._adx_period,
                self._m15_adx_threshold,
                self._m15_ema_period,
                direction,
            ):
                logger.debug(
                    "M5 KC Squeeze [%s]: %s rejected by M15 filter",
                    symbol, direction,
                )
                return None

        # ----------------------------------------------------------
        # 8. SL / TP calculation
        # ----------------------------------------------------------
        curr_close = float(close.iloc[-1])
        sl = kc_mid  # SL at KC middle line (EMA 20)
        tp_dist = self._tp_channel_mult * kc_width

        if direction == "BUY":
            # Ensure SL is below entry
            if sl >= curr_close:
                sl = curr_close - atr_val * 0.5
            tp = curr_close + tp_dist
        else:
            # Ensure SL is above entry
            if sl <= curr_close:
                sl = curr_close + atr_val * 0.5
            tp = curr_close - tp_dist

        # ----------------------------------------------------------
        # 9. RSI overbought/oversold filter
        # ----------------------------------------------------------
        if not self._check_rsi_filter(m5_bars, direction):
            logger.debug("M5 KC Squeeze [%s]: %s blocked by RSI filter", symbol, direction)
            return None

        # ----------------------------------------------------------
        # 10. Emit signal
        # ----------------------------------------------------------
        self._increment_daily_count(symbol, now)
        self._in_squeeze[symbol] = False

        confidence = 0.65
        reason = (
            f"M5 KC squeeze release {direction} "
            f"(KC width={kc_width:.2f}, MACD hist={histogram:.4f})"
        )

        logger.info(
            "M5 KC Squeeze [%s]: %s @ %.5f  SL=%.5f  TP=%.5f  (width=%.2f)",
            symbol, direction, curr_close, sl, tp, kc_width,
        )

        return StrategySignal(
            symbol=symbol,
            action=direction,
            entry_price=curr_close,
            stop_loss=sl,
            take_profit=tp,
            confidence=confidence,
            reason=reason,
            strategy_name="m5_keltner_squeeze",
        )
