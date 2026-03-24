"""M5 Bollinger Band Squeeze Breakout Strategy.

Detects periods of low volatility (Bollinger Band squeeze) on the 5-minute
chart and trades the breakout when volatility expands. Gold consolidates
in tight ranges then explodes — this strategy captures that explosion.

Squeeze = BB bandwidth < 30% of its 50-period average.
Entry = price breaks above upper BB (BUY) or below lower BB (SELL).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

logger = logging.getLogger(__name__)


@dataclass
class StrategySignal:
    symbol: str
    action: str
    entry_price: float
    stop_loss: float
    take_profit: float
    confidence: float
    reason: str


class M5BbSqueezeStrategy:
    """Bollinger Band squeeze breakout on M5."""

    def __init__(
        self,
        bb_length: int = 20,
        bb_std: float = 2.0,
        squeeze_threshold: float = 0.30,
        tp_multiplier: float = 1.5,
        max_trades_per_day: int = 2,
    ) -> None:
        self._bb_length = bb_length
        self._bb_std = bb_std
        self._squeeze_pct = squeeze_threshold
        self._tp_mult = tp_multiplier
        self._max_trades = max_trades_per_day
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

        # Bullish breakout: price closes above upper BB
        if curr_close > curr_upper and not is_squeeze:
            sl = curr_lower
            tp = curr_close + squeeze_range * self._tp_mult
            self._daily_trades[symbol] = (today_str, entry[1] + 1 if entry[0] == today_str else 1)
            self._in_squeeze[symbol] = False
            logger.info(
                "M5 BB Squeeze [%s]: BUY breakout @ %.5f (squeeze range=%.5f)",
                symbol, curr_close, squeeze_range,
            )
            return StrategySignal(
                symbol=symbol, action="BUY", entry_price=curr_close,
                stop_loss=sl, take_profit=tp, confidence=0.60,
                reason=f"M5 BB squeeze breakout UP (range={squeeze_range:.2f})",
            )

        # Bearish breakout: price closes below lower BB
        if curr_close < curr_lower and not is_squeeze:
            sl = curr_upper
            tp = curr_close - squeeze_range * self._tp_mult
            self._daily_trades[symbol] = (today_str, entry[1] + 1 if entry[0] == today_str else 1)
            self._in_squeeze[symbol] = False
            logger.info(
                "M5 BB Squeeze [%s]: SELL breakout @ %.5f (squeeze range=%.5f)",
                symbol, curr_close, squeeze_range,
            )
            return StrategySignal(
                symbol=symbol, action="SELL", entry_price=curr_close,
                stop_loss=sl, take_profit=tp, confidence=0.60,
                reason=f"M5 BB squeeze breakout DOWN (range={squeeze_range:.2f})",
            )

        return None
