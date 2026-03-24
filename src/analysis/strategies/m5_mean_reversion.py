"""M5 Mean Reversion RSI Extreme Strategy.

Catches overextension snapbacks on the 5-minute chart. Gold oscillates
around its mean on short timeframes — extreme RSI readings (< 10 or > 90)
indicate the price has stretched too far and is likely to snap back.

Key difference from standard RSI: we use RSI(7) at EXTREME levels (10/90),
not the standard 30/70 which produces too many false signals on M5.

Entry: RSI(7) < 10 (BUY) or > 90 (SELL) + EMA(20) slope filter
Exit: TP 20-30 pips, SL 15-20 pips, or time stop after 5 candles
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


class M5MeanReversionStrategy:
    """RSI extreme mean reversion on M5."""

    def __init__(
        self,
        rsi_period: int = 7,
        rsi_oversold: float = 10.0,
        rsi_overbought: float = 90.0,
        tp_pips: float = 25.0,
        sl_pips: float = 18.0,
        max_trades_per_day: int = 3,
    ) -> None:
        self._rsi_period = rsi_period
        self._rsi_os = rsi_oversold
        self._rsi_ob = rsi_overbought
        self._tp_pips = tp_pips
        self._sl_pips = sl_pips
        self._max_trades = max_trades_per_day
        self._daily_trades: dict[str, tuple[str, int]] = {}

    async def scan(
        self,
        symbol: str,
        m5_bars: pd.DataFrame,
        point_size: float = 0.01,
        as_of: datetime | None = None,
    ) -> StrategySignal | None:
        if m5_bars is None or len(m5_bars) < 30:
            return None

        now = as_of or datetime.now(timezone.utc)
        today_str = now.strftime("%Y-%m-%d")
        hour = now.hour

        # Session filter: London/NY only (08:00-21:00 UTC)
        if hour < 8 or hour >= 21:
            return None

        # Daily trade limit
        entry = self._daily_trades.get(symbol, ("", 0))
        if entry[0] == today_str and entry[1] >= self._max_trades:
            return None

        close = m5_bars["close"]
        high = m5_bars["high"]
        low = m5_bars["low"]

        # Calculate RSI(7) — fast RSI for extremes
        rsi = ta.rsi(close, length=self._rsi_period)
        ema20 = ta.ema(close, length=20)

        if rsi is None or ema20 is None:
            return None

        curr_rsi = float(rsi.iloc[-1])
        prev_rsi = float(rsi.iloc[-2])
        curr_close = float(close.iloc[-1])
        curr_ema20 = float(ema20.iloc[-1])

        # EMA slope (last 5 bars)
        ema_slope_up = float(ema20.iloc[-1]) > float(ema20.iloc[-5]) if len(ema20) > 5 else False

        tp_dist = self._tp_pips * point_size
        sl_dist = self._sl_pips * point_size

        # BUY: RSI drops below oversold extreme + EMA slope up (with trend)
        if curr_rsi < self._rsi_os and ema_slope_up:
            sl = curr_close - sl_dist
            tp = curr_close + tp_dist
            self._daily_trades[symbol] = (today_str, entry[1] + 1 if entry[0] == today_str else 1)
            logger.info(
                "M5 Mean Rev [%s]: BUY @ %.5f (RSI=%.1f, extreme oversold)",
                symbol, curr_close, curr_rsi,
            )
            return StrategySignal(
                symbol=symbol, action="BUY", entry_price=curr_close,
                stop_loss=sl, take_profit=tp, confidence=0.65,
                reason=f"M5 mean reversion BUY (RSI {curr_rsi:.0f} < {self._rsi_os})",
            )

        # SELL: RSI rises above overbought extreme + EMA slope down
        if curr_rsi > self._rsi_ob and not ema_slope_up:
            sl = curr_close + sl_dist
            tp = curr_close - tp_dist
            self._daily_trades[symbol] = (today_str, entry[1] + 1 if entry[0] == today_str else 1)
            logger.info(
                "M5 Mean Rev [%s]: SELL @ %.5f (RSI=%.1f, extreme overbought)",
                symbol, curr_close, curr_rsi,
            )
            return StrategySignal(
                symbol=symbol, action="SELL", entry_price=curr_close,
                stop_loss=sl, take_profit=tp, confidence=0.65,
                reason=f"M5 mean reversion SELL (RSI {curr_rsi:.0f} > {self._rsi_ob})",
            )

        return None
