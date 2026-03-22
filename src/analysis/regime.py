"""
Market Regime Detection Module

Detects market conditions to filter strategy signals based on whether
the market is trending, ranging, choppy, or in a volatile trend.

This is critical for win rate optimization - trading trend strategies
in ranging/choppy markets is a major source of losses.
"""

from enum import Enum
from dataclasses import dataclass
from typing import Optional
import logging

import pandas as pd

logger = logging.getLogger(__name__)


class MarketRegime(Enum):
    """Market regime classification for strategy filtering."""

    TRENDING_UP = "trending_up"
    """Strong bullish trend - ideal for trend-following longs."""

    TRENDING_DOWN = "trending_down"
    """Strong bearish trend - ideal for trend-following shorts."""

    RANGING = "ranging"
    """Sideways market - ideal for mean reversion, avoid trend strategies."""

    CHOPPY = "choppy"
    """High volatility, no direction - avoid all trading."""

    VOLATILE_TREND = "volatile_trend"
    """Strong trend with high volatility - good for breakouts, wider stops needed."""


@dataclass
class RegimeAnalysis:
    """Result of regime detection analysis."""

    regime: MarketRegime
    """Detected market regime."""

    adx: float
    """ADX value (0-100)."""

    adx_strength: str
    """Human-readable ADX strength: weak, moderate, strong, very_strong."""

    ema_slope: float
    """EMA slope in price change per bar."""

    atr_percentile: float
    """ATR as percentile of recent ATR values (0-100)."""

    bb_position: float
    """Price position in Bollinger Bands (-1 to 1, 0 = middle)."""

    trend_strength: float
    """Combined trend strength score (0-1)."""

    volatility_level: str
    """Volatility classification: low, normal, high, extreme."""

    is_tradeable: bool
    """Whether current regime allows trading (not CHOPPY)."""

    recommended_strategies: list[str]
    """Strategy types recommended for this regime."""


class RegimeDetector:
    """
    Market Regime Detection using ADX, EMA Slope, ATR, and Bollinger Bands.

    Detection Algorithm:
    - TRENDING: ADX > 25 + EMA slope aligned with direction
    - RANGING: ADX < 20 + price oscillating around EMA
    - CHOPPY: High ATR percentile + ADX < 20 (volatile but no direction)
    - VOLATILE_TREND: ADX > 30 + ATR > 75th percentile

    Usage:
        detector = RegimeDetector()
        analysis = detector.detect(
            close_prices=close_prices,
            high_prices=high_prices,
            low_prices=low_prices,
            ema_values=ema_values,
            atr_values=atr_values
        )

        if analysis.regime == MarketRegime.CHOPPY:
            # Skip trading
            return None
    """

    # Thresholds
    ADX_TREND_THRESHOLD = 25.0
    """ADX above this = trending market."""

    ADX_STRONG_THRESHOLD = 30.0
    """ADX above this = strong trend."""

    ADX_RANGING_THRESHOLD = 20.0
    """ADX below this = ranging market."""

    ATR_HIGH_PERCENTILE = 75.0
    """ATR percentile above this = high volatility."""

    ATR_EXTREME_PERCENTILE = 90.0
    """ATR percentile above this = extreme volatility."""

    EMA_SLOPE_THRESHOLD = 0.0001
    """EMA slope threshold for trend direction (relative to price)."""

    BB_RANGE_THRESHOLD = 0.3
    """Price within this % of BB middle = ranging."""

    # Lookback periods
    ATR_PERCENTILE_PERIOD = 50
    """Bars to calculate ATR percentile."""

    EMA_SLOPE_PERIOD = 5
    """Bars to calculate EMA slope."""

    def __init__(
        self,
        adx_trend_threshold: float = 25.0,
        adx_ranging_threshold: float = 20.0,
        atr_high_percentile: float = 75.0,
    ):
        """
        Initialize regime detector with custom thresholds.

        Args:
            adx_trend_threshold: ADX level for trend confirmation
            adx_ranging_threshold: ADX level below which market is ranging
            atr_high_percentile: ATR percentile for high volatility
        """
        self.adx_trend_threshold = adx_trend_threshold
        self.adx_ranging_threshold = adx_ranging_threshold
        self.atr_high_percentile = atr_high_percentile

    def detect(
        self,
        adx: float,
        ema_values: list[float],
        atr: float,
        atr_history: list[float],
        close_prices: list[float],
        bb_upper: float,
        bb_middle: float,
        bb_lower: float,
        current_price: float,
    ) -> RegimeAnalysis:
        """
        Detect market regime from indicator values.

        Args:
            adx: Current ADX value
            ema_values: Recent EMA values (at least 5 bars)
            atr: Current ATR value
            atr_history: Recent ATR values for percentile calculation
            close_prices: Recent close prices
            bb_upper: Bollinger Band upper
            bb_middle: Bollinger Band middle (SMA)
            bb_lower: Bollinger Band lower
            current_price: Current price

        Returns:
            RegimeAnalysis with detected regime and metadata
        """
        # Calculate EMA slope
        ema_slope = self._calculate_ema_slope(ema_values)
        ema_slope_relative = ema_slope / ema_values[-1] if ema_values[-1] > 0 else 0

        # Calculate ATR percentile
        atr_percentile = self._calculate_atr_percentile(atr, atr_history)

        # Calculate BB position
        bb_position = self._calculate_bb_position(
            current_price, bb_upper, bb_middle, bb_lower
        )

        # Determine ADX strength
        adx_strength = self._classify_adx(adx)

        # Determine volatility level
        volatility_level = self._classify_volatility(atr_percentile)

        # Calculate trend strength (0-1)
        trend_strength = self._calculate_trend_strength(
            adx, ema_slope_relative, atr_percentile
        )

        # Detect regime
        regime = self._classify_regime(
            adx=adx,
            ema_slope_relative=ema_slope_relative,
            atr_percentile=atr_percentile,
            bb_position=bb_position,
        )

        # Determine if tradeable
        is_tradeable = regime != MarketRegime.CHOPPY

        # Recommend strategies
        recommended_strategies = self._get_recommended_strategies(regime)

        return RegimeAnalysis(
            regime=regime,
            adx=adx,
            adx_strength=adx_strength,
            ema_slope=ema_slope,
            atr_percentile=atr_percentile,
            bb_position=bb_position,
            trend_strength=trend_strength,
            volatility_level=volatility_level,
            is_tradeable=is_tradeable,
            recommended_strategies=recommended_strategies,
        )

    def _calculate_ema_slope(self, ema_values: list[float]) -> float:
        """Calculate EMA slope (change per bar)."""
        if len(ema_values) < 2:
            return 0.0

        # Linear regression slope over recent bars
        n = min(self.EMA_SLOPE_PERIOD, len(ema_values))
        recent = ema_values[-n:]

        if n < 2:
            return 0.0

        # Simple slope calculation
        return (recent[-1] - recent[0]) / (n - 1)

    def _calculate_atr_percentile(
        self, atr: float, atr_history: list[float]
    ) -> float:
        """Calculate ATR percentile (0-100)."""
        if not atr_history or atr <= 0:
            return 50.0  # Default to middle

        # Sort history and find position
        sorted_history = sorted(atr_history[-self.ATR_PERCENTILE_PERIOD :])

        if not sorted_history:
            return 50.0

        # Count values below current ATR
        below = sum(1 for v in sorted_history if v < atr)

        return (below / len(sorted_history)) * 100

    def _calculate_bb_position(
        self,
        price: float,
        bb_upper: float,
        bb_middle: float,
        bb_lower: float,
    ) -> float:
        """
        Calculate price position in Bollinger Bands.

        Returns:
            -1.0 = at lower band
             0.0 = at middle
             1.0 = at upper band
        """
        bb_range = bb_upper - bb_lower

        if bb_range <= 0:
            return 0.0

        # Normalize to -1 to 1 range
        position = (price - bb_middle) / (bb_range / 2)

        return max(-1.0, min(1.0, position))

    def _classify_adx(self, adx: float) -> str:
        """Classify ADX strength."""
        if adx < 20:
            return "weak"
        elif adx < 25:
            return "moderate"
        elif adx < 40:
            return "strong"
        else:
            return "very_strong"

    def _classify_volatility(self, atr_percentile: float) -> str:
        """Classify volatility level."""
        if atr_percentile < 25:
            return "low"
        elif atr_percentile < 50:
            return "normal"
        elif atr_percentile < self.atr_high_percentile:
            return "high"
        else:
            return "extreme"

    def _calculate_trend_strength(
        self, adx: float, ema_slope_relative: float, atr_percentile: float
    ) -> float:
        """
        Calculate combined trend strength score (0-1).

        Higher score = stronger trend.
        """
        # ADX contribution (normalized to 0-1)
        adx_score = min(adx / 50.0, 1.0)

        # EMA slope contribution (absolute value, normalized)
        slope_score = min(abs(ema_slope_relative) * 1000, 1.0)

        # Volatility penalty (high volatility reduces trend quality)
        volatility_penalty = max(0, (atr_percentile - 50) / 100)

        # Combined score
        combined = (adx_score * 0.6 + slope_score * 0.4) * (1 - volatility_penalty * 0.3)

        return max(0.0, min(1.0, combined))

    def _classify_regime(
        self,
        adx: float,
        ema_slope_relative: float,
        atr_percentile: float,
        bb_position: float,
    ) -> MarketRegime:
        """Classify market regime from calculated metrics."""

        # Check for choppy market first (highest priority to avoid)
        if adx < self.adx_ranging_threshold and atr_percentile > self.atr_high_percentile:
            logger.debug(f"CHOPPY detected: ADX={adx}, ATR%={atr_percentile}")
            return MarketRegime.CHOPPY

        # Check for trending market
        if adx >= self.adx_trend_threshold:
            # Determine direction from EMA slope
            if ema_slope_relative > self.EMA_SLOPE_THRESHOLD:
                # Bullish trend
                if atr_percentile > self.atr_high_percentile:
                    return MarketRegime.VOLATILE_TREND
                return MarketRegime.TRENDING_UP

            elif ema_slope_relative < -self.EMA_SLOPE_THRESHOLD:
                # Bearish trend
                if atr_percentile > self.atr_high_percentile:
                    return MarketRegime.VOLATILE_TREND
                return MarketRegime.TRENDING_DOWN

        # Check for ranging market
        if adx < self.adx_ranging_threshold:
            # Price near middle of BB = ranging
            if abs(bb_position) < self.BB_RANGE_THRESHOLD:
                return MarketRegime.RANGING

        # Default: weak trend based on slope direction
        if ema_slope_relative > self.EMA_SLOPE_THRESHOLD:
            return MarketRegime.TRENDING_UP
        elif ema_slope_relative < -self.EMA_SLOPE_THRESHOLD:
            return MarketRegime.TRENDING_DOWN

        # Fallback to ranging
        return MarketRegime.RANGING

    def _get_recommended_strategies(self, regime: MarketRegime) -> list[str]:
        """Get list of strategy types recommended for this regime."""
        recommendations = {
            MarketRegime.TRENDING_UP: ["trend_following", "breakout", "pullback"],
            MarketRegime.TRENDING_DOWN: ["trend_following", "breakout", "pullback"],
            MarketRegime.RANGING: ["mean_reversion", "range_trading"],
            MarketRegime.CHOPPY: [],  # No strategies recommended
            MarketRegime.VOLATILE_TREND: ["breakout", "momentum"],
        }
        return recommendations.get(regime, [])

    def is_regime_allowed(
        self, regime: MarketRegime, allowed_regimes: list[MarketRegime]
    ) -> bool:
        """
        Check if trading is allowed for current regime.

        Args:
            regime: Current detected regime
            allowed_regimes: List of regimes where trading is allowed

        Returns:
            True if trading allowed, False otherwise
        """
        if regime == MarketRegime.CHOPPY:
            return False  # Never trade in choppy conditions

        return regime in allowed_regimes

    def get_position_size_multiplier(self, regime: MarketRegime) -> float:
        """
        Get position size multiplier based on regime quality.

        Better regimes allow larger positions.

        Returns:
            Multiplier: 0.0 = no trade, 0.5 = reduce, 1.0 = normal, 1.5 = increase
        """
        multipliers = {
            MarketRegime.TRENDING_UP: 1.0,
            MarketRegime.TRENDING_DOWN: 1.0,
            MarketRegime.RANGING: 0.75,  # Reduce size for ranging
            MarketRegime.CHOPPY: 0.0,  # No trading
            MarketRegime.VOLATILE_TREND: 1.25,  # Increase for momentum
        }
        return multipliers.get(regime, 0.5)


def detect_regime_from_ohlcv(
    df: pd.DataFrame,
    detector: RegimeDetector,
    current_price: float,
    ema_period: int = 50,
    atr_period: int = 14,
    bb_period: int = 20,
    bb_std_dev: float = 2.0,
) -> MarketRegime:
    """Compute market regime directly from a raw OHLCV DataFrame.

    Calculates EMA, ATR, and Bollinger Bands inline so callers don't need
    to issue additional indicator API requests.

    Args:
        df: OHLCV DataFrame with columns 'open', 'high', 'low', 'close'.
        detector: Configured RegimeDetector instance.
        current_price: Latest market price.
        ema_period: EMA lookback for slope calculation.
        atr_period: ATR lookback for volatility.
        bb_period: Bollinger Band SMA period.
        bb_std_dev: Number of standard deviations for bands.

    Returns:
        Detected MarketRegime, defaulting to RANGING on any error.
    """
    try:
        if len(df) < max(ema_period, atr_period, bb_period) + 5:
            return MarketRegime.RANGING

        closes = df["close"].tolist()
        highs = df["high"].tolist()
        lows = df["low"].tolist()

        # EMA slope history (last EMA_SLOPE_PERIOD values)
        ema_series = df["close"].ewm(span=ema_period, adjust=False).mean()
        ema_values = ema_series.tolist()[-RegimeDetector.EMA_SLOPE_PERIOD :]

        # ATR history (last ATR_PERCENTILE_PERIOD values)
        tr = [
            max(h - l, abs(h - pc), abs(l - pc))
            for h, l, pc in zip(highs[1:], lows[1:], closes[:-1])
        ]
        atr_series = pd.Series(tr).ewm(alpha=1 / atr_period, adjust=False).mean()
        current_atr = float(atr_series.iloc[-1])
        atr_history = atr_series.tolist()[-RegimeDetector.ATR_PERCENTILE_PERIOD :]

        # Bollinger Bands
        bb_mid = float(df["close"].rolling(bb_period).mean().iloc[-1])
        bb_std = float(df["close"].rolling(bb_period).std().iloc[-1])
        bb_upper = bb_mid + bb_std_dev * bb_std
        bb_lower = bb_mid - bb_std_dev * bb_std

        # ADX approximation via Wilder smoothing
        up_moves = [highs[i] - highs[i - 1] for i in range(1, len(highs))]
        down_moves = [lows[i - 1] - lows[i] for i in range(1, len(lows))]
        plus_dm = [max(u, 0) if u > d else 0.0 for u, d in zip(up_moves, down_moves)]
        minus_dm = [max(d, 0) if d > u else 0.0 for u, d in zip(up_moves, down_moves)]

        def _wilder(vals: list[float], period: int = atr_period) -> float:
            return float(pd.Series(vals).ewm(alpha=1 / period, adjust=False).mean().iloc[-1])

        atr_sm = _wilder(tr)
        if atr_sm <= 0:
            adx_val = 20.0
        else:
            di_plus = 100.0 * _wilder(plus_dm) / atr_sm
            di_minus = 100.0 * _wilder(minus_dm) / atr_sm
            denom = di_plus + di_minus
            dx = 100.0 * abs(di_plus - di_minus) / denom if denom > 0 else 0.0
            adx_val = _wilder([dx])

        analysis = detector.detect(
            adx=adx_val,
            ema_values=ema_values,
            atr=current_atr,
            atr_history=atr_history,
            close_prices=closes[-20:],
            bb_upper=bb_upper,
            bb_middle=bb_mid,
            bb_lower=bb_lower,
            current_price=current_price,
        )
        return analysis.regime

    except Exception:
        logger.warning("detect_regime_from_ohlcv failed; defaulting to RANGING", exc_info=True)
        return MarketRegime.RANGING


def get_regime_filter_for_strategy(strategy_type: str) -> list[MarketRegime]:
    """
    Get allowed regimes for a strategy type.

    Args:
        strategy_type: One of 'trend_following', 'mean_reversion', 'breakout'

    Returns:
        List of allowed MarketRegime values
    """
    filters = {
        "trend_following": [
            MarketRegime.TRENDING_UP,
            MarketRegime.TRENDING_DOWN,
            MarketRegime.VOLATILE_TREND,
        ],
        "mean_reversion": [MarketRegime.RANGING],
        "breakout": [MarketRegime.VOLATILE_TREND, MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN],
        "all": [MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN, MarketRegime.RANGING, MarketRegime.VOLATILE_TREND],
    }
    return filters.get(strategy_type, filters["all"])