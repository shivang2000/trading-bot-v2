"""AMD Cycle Strategy — Accumulation, Manipulation, FEG, Distribution.

Detects the ICT/SMC market cycle for high-probability entries:
  1. Accumulation: price consolidates in tight range (low ATR)
  2. Manipulation: liquidity sweep beyond the range (stop hunt)
  3. FEG (Fair Value Gap): imbalance created during the sweep
  4. Distribution: enter OPPOSITE to the sweep direction

This strategy produces fewer but higher-quality signals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from src.analysis.strategies.scalping_base import ScalpingStrategyBase
from src.core.models import StrategySignal

logger = logging.getLogger(__name__)


@dataclass
class AccumulationZone:
    """Detected accumulation range."""

    high: float
    low: float
    bars: int
    start_idx: int


class M5AmdCycleStrategy(ScalpingStrategyBase):
    """AMD Cycle: Accumulation → Manipulation → FEG → Distribution entry."""

    CONFIDENCE = 0.75
    MAX_DAILY_TRADES = 3
    SESSION_START_HOUR = 7
    SESSION_END_HOUR = 21

    ALLOWED_HOURS = list(range(SESSION_START_HOUR, SESSION_END_HOUR + 1))

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(max_trades_per_day=self.MAX_DAILY_TRADES)
        cfg = config or {}
        self._accum_bars = cfg.get("accum_bars", 12)
        self._accum_atr_pct = cfg.get("accum_atr_pct", 0.8)
        self._sweep_wick_atr = cfg.get("sweep_wick_atr", 1.5)
        self._feg_min_size = cfg.get("feg_min_size", 0.3)
        self._min_rr = cfg.get("min_rr", 2.0)

    def _find_accumulation(self, bars: pd.DataFrame, atr: float) -> AccumulationZone | None:
        """Find a recent accumulation zone (tight range, low volatility)."""
        if len(bars) < self._accum_bars + 10:
            return None

        # Look at the last 50-100 bars for accumulation
        lookback = min(100, len(bars) - 10)
        window = bars.iloc[-lookback:-5]  # Exclude last 5 bars (potential manipulation)

        # Sliding window to find tight ranges
        best_zone = None
        min_range = float("inf")

        for i in range(len(window) - self._accum_bars):
            chunk = window.iloc[i : i + self._accum_bars]
            high = float(chunk["high"].max())
            low = float(chunk["low"].min())
            range_size = high - low

            # Accumulation = range < accum_atr_pct × ATR
            if atr > 0 and range_size < atr * self._accum_atr_pct and range_size < min_range:
                min_range = range_size
                best_zone = AccumulationZone(
                    high=high,
                    low=low,
                    bars=self._accum_bars,
                    start_idx=len(bars) - lookback + i,
                )

        return best_zone

    def _detect_manipulation(
        self, bars: pd.DataFrame, zone: AccumulationZone, atr: float
    ) -> tuple[str, float] | None:
        """Detect a liquidity sweep beyond the accumulation range.

        Returns (sweep_direction, sweep_price) or None.
        """
        # Check last 5 bars for a sweep beyond the zone
        recent = bars.iloc[-5:]

        for _, candle in recent.iterrows():
            # Sweep above (bearish manipulation → expect sell distribution)
            if candle["high"] > zone.high + atr * 0.2:
                # Check if it reversed (close back inside or below zone)
                if candle["close"] <= zone.high:
                    return "above", float(candle["high"])

            # Sweep below (bullish manipulation → expect buy distribution)
            if candle["low"] < zone.low - atr * 0.2:
                # Check if it reversed (close back inside or above zone)
                if candle["close"] >= zone.low:
                    return "below", float(candle["low"])

        return None

    def _detect_feg(self, bars: pd.DataFrame, atr: float) -> tuple[float, float] | None:
        """Detect a fair value gap in recent bars.

        Returns (feg_high, feg_low) or None.
        """
        if len(bars) < 3:
            return None

        # Check last 3-5 candles for FVG
        for i in range(-4, -1):
            if abs(i) > len(bars):
                continue
            try:
                c1 = bars.iloc[i - 1]
                c3 = bars.iloc[i + 1]

                # Bullish FVG: gap between candle1 high and candle3 low
                if c3["low"] > c1["high"]:
                    gap_size = c3["low"] - c1["high"]
                    if atr > 0 and gap_size / atr >= self._feg_min_size:
                        return (float(c3["low"]), float(c1["high"]))

                # Bearish FVG: gap between candle3 high and candle1 low
                if c1["low"] > c3["high"]:
                    gap_size = c1["low"] - c3["high"]
                    if atr > 0 and gap_size / atr >= self._feg_min_size:
                        return (float(c1["low"]), float(c3["high"]))
            except (IndexError, KeyError):
                continue

        return None

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
        """Scan for AMD Cycle signal."""
        if m5_bars is None or len(m5_bars) < 50:
            return None
        now = as_of or datetime.now(timezone.utc)
        if not self._check_daily_limit(symbol, now):
            return None
        if not self._check_session(now, self.ALLOWED_HOURS):
            return None

        current_price = float(m5_bars.iloc[-1]["close"])

        # Skip strong trending regimes — AMD works best in ranging → breakout
        regime_str = regime.value if hasattr(regime, "value") else str(regime) if regime else ""
        if regime_str in ("STRONG_TREND_UP", "STRONG_TREND_DOWN"):
            return None

        # Calculate ATR
        import pandas_ta as ta
        atr_series = ta.atr(m5_bars["high"], m5_bars["low"], m5_bars["close"], length=14)
        atr = float(atr_series.iloc[-1]) if atr_series is not None and len(atr_series) > 0 else 0
        if atr <= 0:
            return None

        # Step 1: Find accumulation zone
        zone = self._find_accumulation(m5_bars, atr)
        if zone is None:
            return None

        # Step 2: Detect manipulation (liquidity sweep)
        sweep = self._detect_manipulation(m5_bars, zone, atr)
        if sweep is None:
            return None

        sweep_dir, sweep_price = sweep

        # Step 3: Check for FEG (optional but boosts confidence)
        feg = self._detect_feg(m5_bars, atr)
        confidence = self.CONFIDENCE
        if feg:
            confidence = min(0.90, confidence + 0.10)  # FEG confirmation boost

        # Step 4: Generate distribution entry (opposite to sweep)
        signal = None

        if sweep_dir == "below":
            # Sweep below = bullish manipulation → BUY distribution
            sl = sweep_price - atr * 0.5  # SL below the sweep wick
            tp = zone.high + (zone.high - zone.low)  # TP = range width above zone
            rr = abs(tp - current_price) / abs(current_price - sl) if abs(current_price - sl) > 0 else 0

            if rr >= self._min_rr:
                signal = StrategySignal(
                    symbol=symbol,
                    action="BUY",
                    entry_price=current_price,
                    stop_loss=sl,
                    take_profit=tp,
                    confidence=confidence,
                    reason=f"AMD BUY: sweep below {zone.low:.2f} at {sweep_price:.2f}, "
                    f"distribution up (R:R {rr:.1f})"
                    + (f", FEG confirmed" if feg else ""),
                )

        elif sweep_dir == "above":
            # Sweep above = bearish manipulation → SELL distribution
            sl = sweep_price + atr * 0.5  # SL above the sweep wick
            tp = zone.low - (zone.high - zone.low)  # TP = range width below zone
            rr = abs(current_price - tp) / abs(sl - current_price) if abs(sl - current_price) > 0 else 0

            if rr >= self._min_rr:
                signal = StrategySignal(
                    symbol=symbol,
                    action="SELL",
                    entry_price=current_price,
                    stop_loss=sl,
                    take_profit=tp,
                    confidence=confidence,
                    reason=f"AMD SELL: sweep above {zone.high:.2f} at {sweep_price:.2f}, "
                    f"distribution down (R:R {rr:.1f})"
                    + (f", FEG confirmed" if feg else ""),
                )

        if signal:
            self._increment_daily_count(symbol, now)
            logger.info(
                "AMD Cycle [%s]: %s @ %.2f (zone: %.2f-%.2f, sweep: %s @ %.2f, conf: %.2f)",
                symbol, signal.action, current_price,
                zone.low, zone.high, sweep_dir, sweep_price, confidence,
            )

        return signal
