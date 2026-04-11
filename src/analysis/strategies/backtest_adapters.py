"""Backtest adapters for EMA Pullback and London Breakout strategies.

These strategies have different scan() signatures than the scalping strategies.
These adapters wrap them to be compatible with the backtest engine's introspection.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

from src.analysis.strategies.ema_pullback import EmaPullbackStrategy
from src.analysis.strategies.london_breakout import LondonBreakoutStrategy
from src.analysis.strategies.scalping_base import ScalpingStrategyBase
from src.config.schema import EmaPullbackConfig, LondonBreakoutConfig
from src.core.models import StrategySignal

logger = logging.getLogger(__name__)


class EmaPullbackBacktestAdapter(ScalpingStrategyBase):
    """Adapter to run EMA Pullback in the scalping backtest engine."""

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(max_trades_per_day=10)
        self._strategy = EmaPullbackStrategy(EmaPullbackConfig())

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
        """Adapt to backtest engine interface."""
        # EMA Pullback needs M15 data — resample from M5 if M15 not available
        if m15_bars is None and m5_bars is not None and len(m5_bars) >= 60:
            m15_bars = self._resample_m5_to_m15(m5_bars)

        if m15_bars is None or len(m15_bars) < 60:
            return None

        # Convert regime to booleans
        regime_str = regime.value if hasattr(regime, "value") else str(regime) if regime else ""
        is_up = "up" in regime_str.lower() or "trending_up" in regime_str.lower()
        is_down = "down" in regime_str.lower() or "trending_down" in regime_str.lower()
        is_choppy = "choppy" in regime_str.lower()

        return await self._strategy.scan(
            symbol=symbol,
            m15_bars=m15_bars,
            h1_regime_is_trending_up=is_up,
            h1_regime_is_trending_down=is_down,
            h1_regime_is_choppy=is_choppy,
        )

    @staticmethod
    def _resample_m5_to_m15(m5: pd.DataFrame) -> pd.DataFrame:
        """Resample M5 bars to M15."""
        df = m5.copy()
        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], utc=True)
            df = df.set_index("time")
        resampled = df.resample("15min").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "tick_volume": "sum",
        }).dropna()
        resampled = resampled.reset_index()
        return resampled


class LondonBreakoutBacktestAdapter(ScalpingStrategyBase):
    """Adapter to run London Breakout in the scalping backtest engine."""

    def __init__(self, config: dict | None = None) -> None:
        super().__init__(max_trades_per_day=2)
        self._strategy = LondonBreakoutStrategy(LondonBreakoutConfig())

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
        """Adapt to backtest engine interface."""
        # London Breakout needs M15 data
        if m15_bars is None and m5_bars is not None and len(m5_bars) >= 60:
            m15_bars = EmaPullbackBacktestAdapter._resample_m5_to_m15(m5_bars)

        if m15_bars is None or len(m15_bars) < 30:
            return None

        regime_str = regime.value if hasattr(regime, "value") else str(regime) if regime else ""
        is_choppy = "choppy" in regime_str.lower()
        is_ranging = "ranging" in regime_str.lower()

        return await self._strategy.scan(
            symbol=symbol,
            m15_bars=m15_bars,
            point_size=point_size,
            regime_is_choppy=is_choppy,
            regime_is_ranging=is_ranging,
            as_of=as_of,
        )
