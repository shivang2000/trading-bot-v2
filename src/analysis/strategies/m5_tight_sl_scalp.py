"""M5 Tight Stop-Loss Scalping Strategy.

Inspired by the No-Nonsense Forex scalping robot. Bets that if price breaks
the last bar's high/low, it will continue in that direction by at least a
small amount — then trails the stop loss extremely aggressively.

Entry: Close > previous bar high → BUY, Close < previous bar low → SELL.
SL/TP: Fixed pips (Forex) or percentage of price (Gold, Indices).
Trailing: Activates after 1.5 pips profit, trails by 1 pip — extremely tight.
Session: 7:00-21:00 UTC. Max 30 trades/day.

Filters:
- Gap filter: skip if entry is too far from breakout level (> 1.5x ATR)
- Range filter: skip if previous bar range < 0.3x ATR (noise)
- RSI filter: opt-in, blocks buys at overbought / sells at oversold
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from src.core.models import StrategySignal
from src.analysis.strategies.scalping_base import ScalpingStrategyBase

logger = logging.getLogger(__name__)

ACTIVE_HOURS = list(range(7, 22))

# Instrument type detection
_FOREX_SYMBOLS = {"USDJPY", "GBPUSD", "EURUSD", "GBPJPY", "NZDUSD", "AUDUSD", "USDCHF", "EURGBP"}
_INDEX_SYMBOLS = {"US30", "NAS100", "USTEC", "SPX500", "DE40", "UK100"}


class M5TightSlScalpStrategy(ScalpingStrategyBase):
    """Tight stop-loss scalper on M5 with aggressive trailing.

    Uses the previous bar's high/low as breakout levels. On breakout,
    enters with a tight SL and trails aggressively after minimal profit.
    """

    def __init__(
        self,
        # Forex parameters (fixed pips)
        sl_pips: float = 20.0,
        tp_pips: float = 20.0,
        trail_trigger_pips: float = 1.5,
        trail_distance_pips: float = 1.0,
        # Percentage parameters (Gold, Indices)
        sl_pct: float = 0.5,
        tp_pct: float = 0.5,
        trail_trigger_pct: float = 0.03,
        trail_distance_pct: float = 0.02,
        # Filters
        gap_filter_mult: float = 1.5,
        range_filter_mult: float = 0.3,
        atr_period: int = 14,
        use_rsi_filter: bool = True,
        max_trades_per_day: int = 30,
    ) -> None:
        super().__init__(max_trades_per_day=max_trades_per_day)
        self._sl_pips = sl_pips
        self._tp_pips = tp_pips
        self._trail_trigger_pips = trail_trigger_pips
        self._trail_distance_pips = trail_distance_pips
        self._sl_pct = sl_pct
        self._tp_pct = tp_pct
        self._trail_trigger_pct = trail_trigger_pct
        self._trail_distance_pct = trail_distance_pct
        self._gap_filter_mult = gap_filter_mult
        self._range_filter_mult = range_filter_mult
        self._atr_period = atr_period
        self._use_rsi_filter = use_rsi_filter

    @staticmethod
    def _instrument_type(symbol: str) -> str:
        """Detect instrument type for SL/TP calculation."""
        if symbol in _FOREX_SYMBOLS:
            return "forex"
        if symbol in _INDEX_SYMBOLS:
            return "index"
        return "commodity"  # Gold, Silver, BTC, ETH

    async def scan(
        self,
        symbol: str,
        m5_bars: pd.DataFrame,
        point_size: float = 0.01,
        as_of: datetime | None = None,
        **kwargs,
    ) -> StrategySignal | None:
        """Scan M5 bars for tight SL scalp entry."""
        min_bars = self._atr_period + 5
        if m5_bars is None or len(m5_bars) < min_bars:
            return None

        now = as_of or datetime.now(timezone.utc)

        if not self._check_session(now, ACTIVE_HOURS):
            return None
        if not self._check_daily_limit(symbol, now):
            return None

        close = m5_bars["close"]
        high = m5_bars["high"]
        low = m5_bars["low"]

        curr_close = float(close.iloc[-1])
        prev_high = float(high.iloc[-2])
        prev_low = float(low.iloc[-2])
        prev_range = prev_high - prev_low

        # ATR for filters
        atr = ta.atr(high, low, close, length=self._atr_period)
        if atr is None or atr.empty:
            return None
        curr_atr = float(atr.iloc[-1])
        if curr_atr <= 0:
            return None

        # Range filter: skip if previous bar is too small (noise)
        if prev_range < self._range_filter_mult * curr_atr:
            return None

        # Detect breakout direction
        direction = None
        if curr_close > prev_high:
            direction = "BUY"
            gap_distance = curr_close - prev_high
        elif curr_close < prev_low:
            direction = "SELL"
            gap_distance = prev_low - curr_close
        else:
            return None

        # Gap filter: skip if price gapped too far past the breakout level
        if gap_distance > self._gap_filter_mult * curr_atr:
            logger.debug(
                "Tight SL [%s]: %s gap too large (%.2f > %.2f * ATR)",
                symbol, direction, gap_distance, self._gap_filter_mult,
            )
            return None

        # RSI filter (opt-in)
        if self._use_rsi_filter and not self._check_rsi_filter(m5_bars, direction):
            logger.debug("Tight SL [%s]: %s blocked by RSI filter", symbol, direction)
            return None

        # Calculate SL/TP based on instrument type
        inst_type = self._instrument_type(symbol)

        if inst_type == "forex":
            sl_dist = self._sl_pips * point_size
            tp_dist = self._tp_pips * point_size
            trail_trigger = self._trail_trigger_pips * point_size
            trail_dist = self._trail_distance_pips * point_size
        else:
            sl_dist = curr_close * self._sl_pct / 100.0
            tp_dist = curr_close * self._tp_pct / 100.0
            trail_trigger = curr_close * self._trail_trigger_pct / 100.0
            trail_dist = curr_close * self._trail_distance_pct / 100.0

        if direction == "BUY":
            sl = curr_close - sl_dist
            tp = curr_close + tp_dist
        else:
            sl = curr_close + sl_dist
            tp = curr_close - tp_dist

        self._increment_daily_count(symbol, now)

        confidence = 0.60

        logger.info(
            "Tight SL [%s]: %s @ %.5f (prev_h=%.5f prev_l=%.5f SL=%.5f TP=%.5f trail=%.4f/%.4f)",
            symbol, direction, curr_close, prev_high, prev_low, sl, tp,
            trail_trigger, trail_dist,
        )

        return StrategySignal(
            symbol=symbol,
            action=direction,
            entry_price=curr_close,
            stop_loss=sl,
            take_profit=tp,
            confidence=confidence,
            reason=f"Tight SL {direction} (break prev {'high' if direction == 'BUY' else 'low'}, ATR={curr_atr:.2f})",
            strategy_name="m5_tight_sl_scalp",
            trail_trigger=trail_trigger,
            trail_distance=trail_dist,
        )
