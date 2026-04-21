"""Shared base class for all scalping strategies.

Provides common functionality:
- Daily trade limit tracking
- Session hour filtering
- ATR-dynamic SL/TP calculation based on ADX regime
- Candlestick pattern detection (pin bars, engulfing)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from src.core.models import StrategySignal

logger = logging.getLogger(__name__)


class ScalpingStrategyBase(ABC):
    """Abstract base for scalping strategies with shared helpers."""

    def __init__(self, max_trades_per_day: int = 30) -> None:
        self._max_trades_per_day = max_trades_per_day
        self._daily_trades: dict[str, tuple[str, int]] = {}

    def _check_daily_limit(self, symbol: str, as_of: datetime) -> bool:
        """Return True if daily trade limit NOT yet reached."""
        today_str = as_of.strftime("%Y-%m-%d")
        entry = self._daily_trades.get(symbol, ("", 0))
        if entry[0] == today_str and entry[1] >= self._max_trades_per_day:
            return False
        return True

    def _increment_daily_count(self, symbol: str, as_of: datetime) -> None:
        """Increment the daily trade counter."""
        today_str = as_of.strftime("%Y-%m-%d")
        entry = self._daily_trades.get(symbol, ("", 0))
        if entry[0] == today_str:
            self._daily_trades[symbol] = (today_str, entry[1] + 1)
        else:
            self._daily_trades[symbol] = (today_str, 1)

    @staticmethod
    def _check_session(as_of: datetime, allowed_hours: list[int]) -> bool:
        """Return True if current hour is in allowed hours list."""
        return as_of.hour in allowed_hours

    @staticmethod
    def _atr_dynamic_sl_tp(atr: float, adx: float) -> tuple[float, float]:
        """Return (sl_multiplier, tp_multiplier) based on ADX regime.

        Low volatility (ADX < 20):  SL = 1.0x ATR, TP = 1.5x ATR
        Normal (ADX 20-30):         SL = 1.5x ATR, TP = 3.0x ATR
        High volatility (ADX > 30): SL = 2.0x ATR, TP = 4.0x ATR
        """
        if adx < 20:
            return 1.0, 1.5
        elif adx <= 30:
            return 1.5, 3.0
        else:
            return 2.0, 4.0

    @staticmethod
    def detect_candle_pattern(
        o: float, h: float, l: float, c: float,
        po: float, ph: float, pl: float, pc: float,
    ) -> str:
        """Detect candlestick patterns from current and previous bar OHLC.

        Returns one of: "pin_bar_bull", "pin_bar_bear",
                        "engulfing_bull", "engulfing_bear", or ""
        """
        body = abs(c - o)
        total_range = h - l

        if total_range <= 0:
            return ""

        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l

        # Pin bar: one wick > 2x body, other wick < 0.5x body
        if body > 0:
            if lower_wick > 2 * body and upper_wick < 0.5 * body and c > o:
                return "pin_bar_bull"
            if upper_wick > 2 * body and lower_wick < 0.5 * body and c < o:
                return "pin_bar_bear"

        # Engulfing: current body completely engulfs previous body
        prev_body_high = max(po, pc)
        prev_body_low = min(po, pc)
        curr_body_high = max(o, c)
        curr_body_low = min(o, c)

        if c > o and pc < po:  # Current green engulfs previous red
            if curr_body_high > prev_body_high and curr_body_low < prev_body_low:
                return "engulfing_bull"
        if c < o and pc > po:  # Current red engulfs previous green
            if curr_body_high > prev_body_high and curr_body_low < prev_body_low:
                return "engulfing_bear"

        return ""

    @staticmethod
    def _check_rsi_filter(
        bars: pd.DataFrame,
        direction: str,
        period: int = 14,
        overbought: float = 75.0,
        oversold: float = 25.0,
    ) -> bool:
        """Opt-in RSI extreme filter. Returns True if entry is allowed.

        Block BUY when RSI > overbought (likely to reverse down).
        Block SELL when RSI < oversold (likely to reverse up).
        Strategies that trade mean-reversion should NOT use this filter.
        """
        if bars is None or len(bars) < period + 2:
            return True
        rsi = ta.rsi(bars["close"], length=period)
        if rsi is None or rsi.empty:
            return True
        rsi_val = float(rsi.iloc[-1])
        if direction == "BUY" and rsi_val > overbought:
            return False
        if direction == "SELL" and rsi_val < oversold:
            return False
        return True

    @staticmethod
    def _calculate_pct_sl_tp(
        current_price: float,
        direction: str,
        sl_pct: float,
        tp_pct: float,
    ) -> tuple[float, float]:
        """Calculate SL/TP as percentage of current price.

        Use for Gold, US30, BTC, and other instruments where fixed-pip
        SL/TP doesn't work due to large price changes over time.
        Returns (stop_loss, take_profit).
        """
        sl_dist = current_price * sl_pct / 100.0
        tp_dist = current_price * tp_pct / 100.0
        if direction == "BUY":
            return current_price - sl_dist, current_price + tp_dist
        else:
            return current_price + sl_dist, current_price - tp_dist

    @staticmethod
    def _detect_sr_levels(
        bars: pd.DataFrame,
        lookback: int = 100,
        touch_threshold: int = 3,
        tolerance_pct: float = 0.1,
    ) -> list[float]:
        """Detect support/resistance levels with N+ touches.

        Scans the last `lookback` bars for price levels that have been
        touched (within tolerance) at least `touch_threshold` times.
        Returns levels sorted by touch count (most-touched first).
        """
        if bars is None or len(bars) < 20:
            return []

        window = bars.tail(lookback)
        highs = window["high"].values
        lows = window["low"].values
        closes = window["close"].values

        # Collect all pivot points (local highs and lows)
        pivots: list[float] = []
        for i in range(2, len(window) - 2):
            if highs[i] > highs[i - 1] and highs[i] > highs[i - 2] and \
               highs[i] > highs[i + 1] and highs[i] > highs[i + 2]:
                pivots.append(float(highs[i]))
            if lows[i] < lows[i - 1] and lows[i] < lows[i - 2] and \
               lows[i] < lows[i + 1] and lows[i] < lows[i + 2]:
                pivots.append(float(lows[i]))

        if not pivots:
            return []

        # Cluster pivots within tolerance
        avg_price = float(closes[-1])
        tol = avg_price * tolerance_pct / 100.0

        levels: list[tuple[float, int]] = []
        used = [False] * len(pivots)

        for i, p in enumerate(pivots):
            if used[i]:
                continue
            cluster = [p]
            used[i] = True
            for j in range(i + 1, len(pivots)):
                if not used[j] and abs(pivots[j] - p) <= tol:
                    cluster.append(pivots[j])
                    used[j] = True
            avg_level = sum(cluster) / len(cluster)
            levels.append((avg_level, len(cluster)))

        # Filter by touch threshold and sort by touches
        levels = [(lvl, cnt) for lvl, cnt in levels if cnt >= touch_threshold]
        levels.sort(key=lambda x: x[1], reverse=True)

        return [lvl for lvl, _ in levels]

    @staticmethod
    def _score_trade_quality(
        has_ob: bool = False,
        has_fvg: bool = False,
        has_bos_choch: bool = False,
        has_liquidity_sweep: bool = False,
        has_fib_alignment: bool = False,
        in_session: bool = False,
    ) -> tuple[int, str]:
        """Score a trade setup from 0-6 (D to A+).

        Returns (score, grade) where grade is A+/A/B/C/D.
        Only trade A+ (6) or A (5) setups for best results.
        """
        score = sum([
            has_ob,
            has_fvg,
            has_bos_choch,
            has_liquidity_sweep,
            has_fib_alignment,
            in_session,
        ])
        grades = {6: "A+", 5: "A", 4: "B+", 3: "B", 2: "C", 1: "D", 0: "D"}
        return score, grades.get(score, "D")

    @staticmethod
    def _fibonacci_entry(
        high: float,
        low: float,
        direction: str,
        level: float = 0.618,
    ) -> float:
        """Calculate Fibonacci retracement entry price.

        For BUY: entry at high - (high - low) * level (retracement from high)
        For SELL: entry at low + (high - low) * level (retracement from low)
        """
        fib_range = high - low
        if direction == "BUY":
            return high - fib_range * level
        else:
            return low + fib_range * level

    @abstractmethod
    async def scan(self, symbol: str, **kwargs) -> StrategySignal | None:
        """Scan bars and return a signal or None."""
        ...
