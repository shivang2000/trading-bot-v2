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
