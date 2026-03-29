"""ICT Smart Money Concepts — confidence adjuster for all signals.

Uses the smartmoneyconcepts library to detect Order Blocks, Fair Value
Gaps, and Break of Structure. Adjusts signal confidence based on
whether SMC factors support or oppose the trade direction.

Not a standalone signal generator — it's a filter/booster applied to
signals from EMA Pullback, London Breakout, and Telegram.
"""

from __future__ import annotations

import logging

import pandas as pd

from src.config.schema import SmcConfluenceConfig

logger = logging.getLogger(__name__)

try:
    from smartmoneyconcepts import smc
    SMC_AVAILABLE = True
except ImportError:
    SMC_AVAILABLE = False
    logger.warning("smartmoneyconcepts not installed — SMC confluence disabled")

try:
    from src.analysis.liquidity import LiquiditySweepDetector, SweepResult
    LIQUIDITY_AVAILABLE = True
except ImportError:
    LIQUIDITY_AVAILABLE = False

try:
    from src.analysis.anchored_vwap import AnchoredVWAP
    AVWAP_AVAILABLE = True
except ImportError:
    AVWAP_AVAILABLE = False

try:
    from src.analysis.volume_profile import VolumeProfile
    VP_AVAILABLE = True
except ImportError:
    VP_AVAILABLE = False


def _check_fvg_entry_zone(df: pd.DataFrame, price: float, action: str) -> bool:
    """Check if price is inside an unfilled Fair Value Gap.

    Bullish FVG: gap between bar[i-2].high and bar[i].low (price went up fast, left a gap)
    Bearish FVG: gap between bar[i].high and bar[i-2].low (price went down fast)

    If current price is INSIDE an unfilled FVG, it's a high-probability entry.
    """
    if len(df) < 10:
        return False

    high = df["high"].values
    low = df["low"].values

    # Scan recent bars for unfilled FVGs
    for i in range(len(df) - 3, max(len(df) - 50, 2), -1):
        # Bullish FVG: bar[i-2].high < bar[i].low (gap up)
        if high[i - 2] < low[i]:
            fvg_top = float(low[i])
            fvg_bottom = float(high[i - 2])
            # Check if FVG is still unfilled (no bar between i and now closed in the gap)
            filled = False
            for j in range(i + 1, len(df)):
                if low[j] <= fvg_top and high[j] >= fvg_bottom:
                    filled = True
                    break
            if not filled and action == "BUY" and fvg_bottom <= price <= fvg_top:
                return True

        # Bearish FVG: bar[i].high < bar[i-2].low (gap down)
        if high[i] < low[i - 2]:
            fvg_top = float(low[i - 2])
            fvg_bottom = float(high[i])
            filled = False
            for j in range(i + 1, len(df)):
                if low[j] <= fvg_top and high[j] >= fvg_bottom:
                    filled = True
                    break
            if not filled and action == "SELL" and fvg_bottom <= price <= fvg_top:
                return True

    return False


def adjust_confidence(
    action: str,
    symbol: str,
    m15_bars: pd.DataFrame,
    base_confidence: float,
    config: SmcConfluenceConfig,
) -> float:
    """Adjust signal confidence based on ICT Smart Money Concepts.

    Args:
        action: "BUY" or "SELL"
        symbol: Instrument symbol
        m15_bars: M15 OHLCV DataFrame (needs open, high, low, close columns)
        base_confidence: Original confidence from strategy (0.0-1.0)
        config: SMC config with boost/penalty values

    Returns:
        Adjusted confidence (clamped to 0.0-1.0)
    """
    if not SMC_AVAILABLE or not config.enabled:
        return base_confidence

    if m15_bars is None or len(m15_bars) < config.lookback_bars:
        return base_confidence

    try:
        ohlc = m15_bars.tail(config.lookback_bars).copy()

        # Ensure columns exist
        for col in ["open", "high", "low", "close"]:
            if col not in ohlc.columns:
                return base_confidence

        adjustment = 0.0

        # 1. Order Blocks
        try:
            ob = smc.ob(ohlc, swing_length=10)
            if ob is not None and not ob.empty:
                last_ob = ob.iloc[-1]
                ob_type = last_ob.get("OB", 0)
                # ob_type: 1 = bullish OB, -1 = bearish OB
                if action == "BUY" and ob_type == 1:
                    adjustment += config.ob_confidence_boost
                elif action == "SELL" and ob_type == -1:
                    adjustment += config.ob_confidence_boost
                elif action == "BUY" and ob_type == -1:
                    adjustment -= config.opposing_ob_penalty
                elif action == "SELL" and ob_type == 1:
                    adjustment -= config.opposing_ob_penalty
        except Exception:
            pass

        # 2. Fair Value Gaps
        try:
            fvg = smc.fvg(ohlc)
            if fvg is not None and not fvg.empty:
                last_fvg = fvg.iloc[-1]
                fvg_type = last_fvg.get("FVG", 0)
                if action == "BUY" and fvg_type == 1:
                    adjustment += config.fvg_confidence_boost
                elif action == "SELL" and fvg_type == -1:
                    adjustment += config.fvg_confidence_boost
        except Exception:
            pass

        # 3. Break of Structure
        try:
            bos = smc.bos_choch(ohlc, close_column="close")
            if bos is not None and not bos.empty:
                last_bos = bos.iloc[-1]
                bos_type = last_bos.get("BOS", 0)
                if action == "BUY" and bos_type == 1:
                    adjustment += config.bos_confidence_boost
                elif action == "SELL" and bos_type == -1:
                    adjustment += config.bos_confidence_boost
        except Exception:
            pass

        # 4. Liquidity Sweep check
        try:
            if LIQUIDITY_AVAILABLE:
                sweep_detector = LiquiditySweepDetector()
                # Derive point_size from instrument config if available
                point_size = 0.01
                for inst in (config.__dict__ if hasattr(config, '__dict__') else {}):
                    pass  # point_size comes from caller context
                sweep = sweep_detector.detect(ohlc, point_size=point_size)
                if sweep.bullish_sweep and action == "BUY":
                    adjustment += config.liquidity_sweep_boost * sweep.strength
                elif sweep.bearish_sweep and action == "SELL":
                    adjustment += config.liquidity_sweep_boost * sweep.strength
                elif sweep.bullish_sweep and action == "SELL":
                    adjustment -= 0.10  # trading against sweep = penalty
                elif sweep.bearish_sweep and action == "BUY":
                    adjustment -= 0.10
        except Exception:
            pass

        # 5. FVG Entry Zone check (price inside unfilled FVG)
        try:
            entry_price = float(ohlc["close"].iloc[-1])
            fvg_zone_boost = getattr(config, "fvg_entry_zone_boost", 0.15)
            if _check_fvg_entry_zone(ohlc, entry_price, action):
                adjustment += fvg_zone_boost
        except Exception:
            pass

        # 6. Anchored VWAP proximity
        try:
            if AVWAP_AVAILABLE:
                avwap = AnchoredVWAP()
                entry_price = float(ohlc["close"].iloc[-1])
                avwap_boost = getattr(config, "anchored_vwap_bounce_boost", 0.10)
                avwap_levels = avwap.get_nearest_levels(ohlc, entry_price)
                atr_val = (
                    float(ohlc["high"].iloc[-20:].max() - ohlc["low"].iloc[-20:].min()) / 20
                    if len(ohlc) >= 20 else 1.0
                )
                if action == "BUY" and avwap_levels.get("avwap_below") is not None:
                    if avwap_levels["distance_below"] < atr_val * 0.5:
                        adjustment += avwap_boost
                elif action == "SELL" and avwap_levels.get("avwap_above") is not None:
                    if avwap_levels["distance_above"] < atr_val * 0.5:
                        adjustment += avwap_boost
        except Exception:
            pass

        # 7. Volume Profile POC proximity
        try:
            if VP_AVAILABLE:
                volume_profile = VolumeProfile()
                entry_price = float(ohlc["close"].iloc[-1])
                vpoc_boost = getattr(config, "volume_profile_poc_boost", 0.10)
                vp = volume_profile.get_session_profile(ohlc)
                if vp.poc > 0:
                    poc_distance = abs(entry_price - vp.poc)
                    atr_val = (
                        float(ohlc["high"].iloc[-20:].max() - ohlc["low"].iloc[-20:].min()) / 20
                        if len(ohlc) >= 20 else 1.0
                    )
                    if poc_distance < atr_val:
                        adjustment += vpoc_boost
        except Exception:
            pass

        new_confidence = max(0.0, min(1.0, base_confidence + adjustment))

        if abs(adjustment) > 0.01:
            logger.info(
                "SMC confluence for %s %s: %.2f → %.2f (adj=%+.2f)",
                action, symbol, base_confidence, new_confidence, adjustment,
            )

        return new_confidence

    except Exception:
        logger.debug("SMC analysis failed for %s", symbol, exc_info=True)
        return base_confidence
