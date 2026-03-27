"""M5 Bollinger Band Squeeze Breakout Strategy.

Detects periods of low volatility (Bollinger Band squeeze) on the 5-minute
chart and trades the breakout when volatility expands. Gold consolidates
in tight ranges then explodes — this strategy captures that explosion.

Squeeze = BB bandwidth < 30% of its 50-period average.
Entry = price breaks above upper BB (BUY) or below lower BB (SELL).
ADX filter ensures breakout has trending momentum behind it.
ATR-dynamic SL/TP adapts to current volatility regime.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from src.analysis.strategies.scalping_base import ScalpingStrategyBase
from src.core.models import StrategySignal

logger = logging.getLogger(__name__)


class M5BbSqueezeStrategy:
    """Bollinger Band squeeze breakout on M5."""

    def __init__(
        self,
        bb_length: int = 20,
        bb_std: float = 2.0,
        squeeze_threshold: float = 0.30,
        tp_multiplier: float = 1.5,
        max_trades_per_day: int = 20,
        atr_period: int = 10,
        adx_period: int = 14,
        adx_threshold: float = 20.0,
    ) -> None:
        self._bb_length = bb_length
        self._bb_std = bb_std
        self._squeeze_pct = squeeze_threshold
        self._tp_mult = tp_multiplier
        self._max_trades = max_trades_per_day
        self._atr_period = atr_period
        self._adx_period = adx_period
        self._adx_threshold = adx_threshold
        self._daily_trades: dict[str, tuple[str, int]] = {}
        self._in_squeeze: dict[str, bool] = {}

    async def scan(
        self,
        symbol: str,
        m5_bars: pd.DataFrame,
        point_size: float = 0.01,
        as_of: datetime | None = None,
    ) -> StrategySignal | None:
        if m5_bars is None or len(m5_bars) < 60:
            return None

        now = as_of or datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        hour = now.hour

        # Session filter: London/NY only
        if hour < 8 or hour >= 21:
            return None

        # Daily limit
        entry = self._daily_trades.get(symbol, ("", 0))
        if entry[0] == today_str and entry[1] >= self._max_trades:
            return None

        close = m5_bars["close"]
        high = m5_bars["high"]
        low = m5_bars["low"]

        # Calculate Bollinger Bands
        bb = ta.bbands(close, length=self._bb_length, std=self._bb_std)
        if bb is None or bb.empty:
            return None

        # BB columns: BBL, BBM, BBU, BBB, BBP
        bbl = bb.iloc[:, 0]  # lower band
        bbm = bb.iloc[:, 1]  # middle band
        bbu = bb.iloc[:, 2]  # upper band
        bbb = bb.iloc[:, 3]  # bandwidth

        # Calculate ADX and ATR for trend filter and dynamic SL/TP
        adx_df = ta.adx(high, low, close, length=self._adx_period)
        atr = ta.atr(high, low, close, length=self._atr_period)

        if adx_df is None or atr is None:
            return None

        curr_adx = float(adx_df.iloc[-1, 0])  # ADX column
        curr_atr = float(atr.iloc[-1])

        if curr_atr <= 0:
            return None

        # ADX filter: require minimum trend strength for breakout
        if curr_adx < self._adx_threshold:
            return None

        curr_bbb = float(bbb.iloc[-1])
        prev_bbb = float(bbb.iloc[-2])
        curr_close = float(close.iloc[-1])
        curr_upper = float(bbu.iloc[-1])
        curr_lower = float(bbl.iloc[-1])
        curr_middle = float(bbm.iloc[-1])

        # Average bandwidth over last 50 bars
        avg_bbb = float(bbb.tail(50).mean()) if len(bbb) >= 50 else float(bbb.mean())

        if avg_bbb <= 0:
            return None

        # Detect squeeze: bandwidth < threshold% of average
        is_squeeze = curr_bbb < avg_bbb * self._squeeze_pct
        was_squeeze = self._in_squeeze.get(symbol, False)

        # Track squeeze state
        self._in_squeeze[symbol] = is_squeeze

        # Only trade on squeeze RELEASE (was in squeeze, now breaking out)
        if not was_squeeze:
            return None

        squeeze_range = curr_upper - curr_lower
        if squeeze_range <= 0:
            return None

        # ATR-dynamic SL/TP based on ADX regime
        sl_mult, tp_mult = ScalpingStrategyBase._atr_dynamic_sl_tp(curr_atr, curr_adx)

        # Bullish breakout: price closes above upper BB
        if curr_close > curr_upper and not is_squeeze:
            sl = curr_close - sl_mult * curr_atr
            tp = curr_close + tp_mult * curr_atr
            self._daily_trades[symbol] = (today_str, entry[1] + 1 if entry[0] == today_str else 1)
            self._in_squeeze[symbol] = False
            logger.info(
                "M5 BB Squeeze [%s]: BUY breakout @ %.5f (squeeze range=%.5f, ADX=%.1f)",
                symbol, curr_close, squeeze_range, curr_adx,
            )
            return StrategySignal(
                symbol=symbol, action="BUY", entry_price=curr_close,
                stop_loss=sl, take_profit=tp, confidence=0.60,
                reason=f"M5 BB squeeze breakout UP (range={squeeze_range:.2f})",
                strategy_name="m5_bb_squeeze",
            )

        # Bearish breakout: price closes below lower BB
        if curr_close < curr_lower and not is_squeeze:
            sl = curr_close + sl_mult * curr_atr
            tp = curr_close - tp_mult * curr_atr
            self._daily_trades[symbol] = (today_str, entry[1] + 1 if entry[0] == today_str else 1)
            self._in_squeeze[symbol] = False
            logger.info(
                "M5 BB Squeeze [%s]: SELL breakout @ %.5f (squeeze range=%.5f, ADX=%.1f)",
                symbol, curr_close, squeeze_range, curr_adx,
            )
            return StrategySignal(
                symbol=symbol, action="SELL", entry_price=curr_close,
                stop_loss=sl, take_profit=tp, confidence=0.60,
                reason=f"M5 BB squeeze breakout DOWN (range={squeeze_range:.2f})",
                strategy_name="m5_bb_squeeze",
            )

        return None
