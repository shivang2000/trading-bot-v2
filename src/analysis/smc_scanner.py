"""Unified SMC Scanner — runs all Smart Money Concept detections once per bar.

Uses the smartmoneyconcepts pip library to detect:
- Fair Value Gaps (FVG)
- Order Blocks (OB)
- Break of Structure (BOS) / Change of Character (CHOCH)
- Liquidity levels
- Swing Highs/Lows

Strategies query the cached results instead of each computing SMC independently.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

try:
    from smartmoneyconcepts import smc
    SMC_AVAILABLE = True
except ImportError:
    SMC_AVAILABLE = False
    logger.warning("smartmoneyconcepts not installed — SMC scanner disabled")


@dataclass
class SMCState:
    """Cached SMC detection results for one symbol."""
    symbol: str
    timestamp: pd.Timestamp | None = None
    swing_hl: pd.DataFrame | None = None
    bos_choch: pd.DataFrame | None = None
    fvg: pd.DataFrame | None = None
    ob: pd.DataFrame | None = None
    liquidity: pd.DataFrame | None = None

    # Derived signals
    latest_bos_direction: str | None = None  # "BUY" or "SELL"
    latest_choch_direction: str | None = None
    active_fvg_levels: list[tuple[float, float, str]] = field(default_factory=list)  # (top, bottom, direction)
    active_ob_levels: list[tuple[float, float, str]] = field(default_factory=list)
    active_liquidity_levels: list[float] = field(default_factory=list)


class SMCScanner:
    """Runs all SMC detections and caches results per symbol.

    Usage:
        scanner = SMCScanner()
        state = scanner.scan(symbol, m5_bars)
        if state.latest_bos_direction == "BUY":
            # BOS confirmed bullish
        for top, bot, direction in state.active_fvg_levels:
            # Check if price is in FVG zone
    """

    def __init__(self, swing_length: int = 10) -> None:
        self._swing_length = swing_length
        self._cache: dict[str, SMCState] = {}

    def scan(self, symbol: str, ohlc: pd.DataFrame) -> SMCState:
        """Run full SMC scan on OHLC data. Returns cached SMCState."""
        if not SMC_AVAILABLE or ohlc is None or len(ohlc) < self._swing_length + 10:
            return self._cache.get(symbol, SMCState(symbol=symbol))

        # Prepare DataFrame in expected format
        df = ohlc[["open", "high", "low", "close"]].copy()
        if "volume" in ohlc.columns:
            df["volume"] = ohlc["volume"]

        state = SMCState(symbol=symbol)

        try:
            # 1. Swing Highs/Lows (required by other detections)
            state.swing_hl = smc.swing_highs_lows(df, swing_length=self._swing_length)

            # 2. BOS / CHOCH
            state.bos_choch = smc.bos_choch(df, state.swing_hl)
            self._extract_bos_choch(state)

            # 3. Fair Value Gaps
            state.fvg = smc.fvg(df)
            self._extract_fvg(state, df)

            # 4. Order Blocks
            state.ob = smc.ob(df, state.swing_hl)
            self._extract_ob(state, df)

            # 5. Liquidity
            state.liquidity = smc.liquidity(df, state.swing_hl)
            self._extract_liquidity(state, df)

        except Exception:
            logger.debug("SMC scan error for %s", symbol, exc_info=True)

        self._cache[symbol] = state
        return state

    def get_cached(self, symbol: str) -> SMCState:
        """Get last cached SMC state for a symbol."""
        return self._cache.get(symbol, SMCState(symbol=symbol))

    def _extract_bos_choch(self, state: SMCState) -> None:
        """Extract latest BOS/CHOCH direction from scan results."""
        if state.bos_choch is None or state.bos_choch.empty:
            return

        df = state.bos_choch
        # Look for most recent BOS or CHOCH signal
        for col in df.columns:
            if "BOS" in col.upper() or "CHOCH" in col.upper():
                last_valid = df[col].last_valid_index()
                if last_valid is not None:
                    val = df[col].iloc[last_valid] if last_valid is not None else None
                    if val is not None and val != 0:
                        direction = "BUY" if val > 0 else "SELL"
                        if "BOS" in col.upper():
                            state.latest_bos_direction = direction
                        elif "CHOCH" in col.upper():
                            state.latest_choch_direction = direction

    def _extract_fvg(self, state: SMCState, ohlc: pd.DataFrame) -> None:
        """Extract active (unmitigated) FVG levels."""
        if state.fvg is None or state.fvg.empty:
            return

        current_price = float(ohlc["close"].iloc[-1])
        active = []

        for col in state.fvg.columns:
            if "Top" in col or "top" in col:
                top_col = col
                bot_col = col.replace("Top", "Bottom").replace("top", "bottom")
                dir_col = col.replace("Top", "Direction").replace("top", "direction")

                if bot_col not in state.fvg.columns:
                    continue

                for idx in range(len(state.fvg)):
                    top = state.fvg[top_col].iloc[idx]
                    bot = state.fvg[bot_col].iloc[idx]
                    if pd.isna(top) or pd.isna(bot):
                        continue

                    top, bot = float(top), float(bot)
                    direction = "BUY" if top > bot else "SELL"

                    # Check if FVG is still active (price hasn't fully mitigated it)
                    if direction == "BUY" and current_price < top:
                        active.append((top, bot, direction))
                    elif direction == "SELL" and current_price > bot:
                        active.append((top, bot, direction))

        # Keep only most recent 5
        state.active_fvg_levels = active[-5:]

    def _extract_ob(self, state: SMCState, ohlc: pd.DataFrame) -> None:
        """Extract active order block levels."""
        if state.ob is None or state.ob.empty:
            return

        active = []
        for col in state.ob.columns:
            if "Top" in col or "top" in col:
                top_col = col
                bot_col = col.replace("Top", "Bottom").replace("top", "bottom")

                if bot_col not in state.ob.columns:
                    continue

                for idx in range(len(state.ob)):
                    top = state.ob[top_col].iloc[idx]
                    bot = state.ob[bot_col].iloc[idx]
                    if pd.isna(top) or pd.isna(bot):
                        continue

                    top, bot = float(top), float(bot)
                    mid = (top + bot) / 2.0
                    current = float(ohlc["close"].iloc[-1])
                    direction = "BUY" if current > mid else "SELL"
                    active.append((top, bot, direction))

        state.active_ob_levels = active[-5:]

    def _extract_liquidity(self, state: SMCState, ohlc: pd.DataFrame) -> None:
        """Extract liquidity levels."""
        if state.liquidity is None or state.liquidity.empty:
            return

        levels = []
        for col in state.liquidity.columns:
            if "Level" in col or "level" in col or "Liquidity" in col:
                for idx in range(len(state.liquidity)):
                    val = state.liquidity[col].iloc[idx]
                    if not pd.isna(val):
                        levels.append(float(val))

        state.active_liquidity_levels = sorted(set(levels))[-10:]
