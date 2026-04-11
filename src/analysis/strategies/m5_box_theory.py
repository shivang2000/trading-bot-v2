"""Box Theory Strategy — Daily Range Mean Reversion.

Trades the previous day's high/low range with wick rejection entries.
Generates signals during ranging/consolidation days when trend strategies are quiet.

Rules (from Instagram reel research):
  1. Box the previous day's high and low (from H1 data)
  2. Draw middle line = (high + low) / 2
  3. Buy zone = bottom 25% of range → look for pin bar (wick rejection)
  4. Sell zone = top 25% of range → look for pin bar (wick rejection)
  5. No-trade zone = middle 50% (false signals zone)
  6. SL = opposite side of daily range
  7. TP = middle line (conservative) or opposite zone (aggressive)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

from src.analysis.strategies.scalping_base import ScalpingStrategyBase
from src.core.models import StrategySignal

logger = logging.getLogger(__name__)


@dataclass
class DailyBox:
    """Previous day's price range."""

    date: str
    high: float
    low: float
    middle: float
    range_size: float


class M5BoxTheoryStrategy(ScalpingStrategyBase):
    """Daily range mean reversion with wick rejection entries."""

    CONFIDENCE = 0.70
    MAX_DAILY_TRADES = 4
    SESSION_START_HOUR = 8   # Only trade 08:00-20:00 UTC
    SESSION_END_HOUR = 20

    ALLOWED_HOURS = list(range(SESSION_START_HOUR, SESSION_END_HOUR + 1))

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(max_trades_per_day=self.MAX_DAILY_TRADES)
        cfg = config or {}
        self._zone_pct = cfg.get("zone_pct", 0.25)
        self._min_range_atr = cfg.get("min_range_atr", 0.5)
        self._max_range_atr = cfg.get("max_range_atr", 3.0)
        self._wick_ratio = cfg.get("wick_ratio", 2.0)
        self._tp_mode = cfg.get("tp_mode", "middle")
        self._current_box: dict[str, DailyBox | None] = {}

    def _calculate_daily_box(self, symbol: str, bars: pd.DataFrame) -> DailyBox | None:
        """Calculate previous day's high/low from any timeframe data (H1 or M5)."""
        if bars is None or len(bars) < 20:
            return None

        df = bars.copy()
        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], utc=True)
            df["date"] = df["time"].dt.date
        else:
            return None

        # Get previous complete day
        dates = sorted(df["date"].unique())
        if len(dates) < 2:
            return None

        prev_date = dates[-2]
        prev_day = df[df["date"] == prev_date]
        if len(prev_day) < 2:
            return None

        high = float(prev_day["high"].max())
        low = float(prev_day["low"].min())
        middle = (high + low) / 2
        range_size = high - low

        if range_size < 0.01:
            return None

        return DailyBox(
            date=str(prev_date),
            high=high,
            low=low,
            middle=middle,
            range_size=range_size,
        )

    def _is_pin_bar(self, candle: pd.Series, direction: str) -> bool:
        """Check if candle is a wick rejection (pin bar)."""
        o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
        body = abs(c - o)
        if body < 0.01:
            body = 0.01

        if direction == "BUY":
            # Bullish pin bar: long lower wick
            lower_wick = min(o, c) - l
            return lower_wick >= body * self._wick_ratio
        else:
            # Bearish pin bar: long upper wick
            upper_wick = h - max(o, c)
            return upper_wick >= body * self._wick_ratio

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
        """Scan for Box Theory signal."""
        if m5_bars is None or len(m5_bars) < 20:
            return None
        now = as_of or datetime.now(timezone.utc)
        if not self._check_daily_limit(symbol, now):
            return None
        if not self._check_session(now, self.ALLOWED_HOURS):
            return None

        current_price = float(m5_bars.iloc[-1]["close"])

        # Calculate ATR for range validation
        import pandas_ta as ta
        atr_series = ta.atr(m5_bars["high"], m5_bars["low"], m5_bars["close"], length=14)
        atr = float(atr_series.iloc[-1]) if atr_series is not None and len(atr_series) > 0 else 0

        # Calculate daily box — prefer M5 (bigger window) over H1
        box = self._calculate_daily_box(symbol, m5_bars)
        if box is None:
            return None

        # Validate range size
        if atr > 0:
            range_in_atr = box.range_size / atr
            if range_in_atr < self._min_range_atr or range_in_atr > self._max_range_atr:
                return None

        # Determine zones
        zone_size = box.range_size * self._zone_pct
        buy_zone_top = box.low + zone_size
        sell_zone_bottom = box.high - zone_size

        last_candle = m5_bars.iloc[-1]
        signal = None

        if current_price <= buy_zone_top and current_price >= box.low:
            if self._is_pin_bar(last_candle, "BUY"):
                sl = box.low - (box.range_size * 0.1)
                tp = box.middle if self._tp_mode == "middle" else sell_zone_bottom
                signal = StrategySignal(
                    symbol=symbol,
                    action="BUY",
                    entry_price=current_price,
                    stop_loss=sl,
                    take_profit=tp,
                    confidence=self.CONFIDENCE,
                    reason=f"Box Theory BUY: price in buy zone ({current_price:.2f} near low {box.low:.2f}), pin bar confirmed",
                )

        elif current_price >= sell_zone_bottom and current_price <= box.high:
            if self._is_pin_bar(last_candle, "SELL"):
                sl = box.high + (box.range_size * 0.1)
                tp = box.middle if self._tp_mode == "middle" else buy_zone_top
                signal = StrategySignal(
                    symbol=symbol,
                    action="SELL",
                    entry_price=current_price,
                    stop_loss=sl,
                    take_profit=tp,
                    confidence=self.CONFIDENCE,
                    reason=f"Box Theory SELL: price in sell zone ({current_price:.2f} near high {box.high:.2f}), pin bar confirmed",
                )

        if signal:
            self._increment_daily_count(symbol, now)
            logger.info(
                "Box Theory [%s]: %s @ %.2f (box: %.2f-%.2f, mid: %.2f)",
                symbol, signal.action, current_price, box.low, box.high, box.middle,
            )

        return signal
