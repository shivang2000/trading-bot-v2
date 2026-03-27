"""Realistic transaction cost modeling for backtesting.

Models spread, slippage, and commission costs that vary by trading session.
Reuses SessionManager from src/analysis/sessions.py for session-specific
spread multipliers instead of duplicating that logic.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime

from src.analysis.sessions import SessionManager

logger = logging.getLogger(__name__)


class CostModel:
    """Transaction cost model with session-aware spread estimation.

    Spread costs for XAUUSD vary significantly by session:
    - Asian:            3.0-5.0 pips (low liquidity)
    - London:           1.5-2.5 pips
    - NY:               1.5-2.5 pips
    - London/NY Overlap: 1.0-2.0 pips (tightest spreads)
    """

    def __init__(
        self,
        base_spread_pips: float = 1.8,
        commission_per_lot: float = 7.0,
        slippage_pips: float = 0.3,
        session_manager: SessionManager | None = None,
        spread_from_data: bool = False,
        max_spread_points: int = 35,
    ) -> None:
        self._base_spread = base_spread_pips
        self._commission = commission_per_lot
        self._slippage = slippage_pips
        self._session_mgr = session_manager or SessionManager()
        self._spread_from_data = spread_from_data
        self._max_spread_points = max_spread_points

    def get_spread_from_bar(self, bar_spread_points: float, point_size: float = 0.01) -> float:
        """Get spread in pips from the bar's spread column (in points).

        MT5 stores spread in points. For XAUUSD: 18 points = 1.8 pips.
        """
        return bar_spread_points * point_size / (10 * point_size)  # convert points to pips

    def should_skip_trade(self, bar_spread_points: float) -> bool:
        """Return True if spread is too wide (news spike / low liquidity)."""
        return bar_spread_points > self._max_spread_points

    def get_spread(self, as_of: datetime) -> float:
        """Get estimated spread in pips for the current session.
        Uses SessionManager's spread_multiplier (Asian=1.3, London=0.9,
        Overlap=0.8, etc.) and adds slight randomization for realism.
        """
        multiplier = self._session_mgr.get_spread_adjustment(as_of)
        spread = self._base_spread * multiplier
        # Add +/- 10% randomization for realism
        jitter = spread * random.uniform(-0.1, 0.1)
        return max(0.5, spread + jitter)

    def adjust_entry_for_spread(
        self,
        entry_price: float,
        side: str,
        spread_pips: float,
        point_size: float = 0.01,
    ) -> float:
        """Adjust entry price to account for spread cost.
        BUY: entry goes UP by half-spread (we buy at ask).
        SELL: entry goes DOWN by half-spread (we sell at bid).
        """
        half_spread = (spread_pips * point_size) / 2.0
        if side == "BUY":
            return entry_price + half_spread
        else:
            return entry_price - half_spread

    def get_total_cost(
        self,
        volume: float,
        spread_pips: float,
        point_size: float = 0.01,
        tick_value: float = 0.01,
    ) -> float:
        """Calculate total transaction cost in dollars.
        Includes: spread cost + slippage cost + commission.
        """
        total_pips = spread_pips + self._slippage
        pip_cost = (total_pips / point_size) * tick_value * volume if point_size > 0 else 0.0
        commission_cost = self._commission * volume
        return pip_cost + commission_cost
