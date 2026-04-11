"""Partial Profit Manager: closes portions of a position at each TP level.

When a signal has multiple take-profit levels (TP1, TP2, TP3, TP4), this
manager divides the position into equal portions and closes one portion
at each level. After TP1 is hit, SL moves to breakeven. After TP2, SL
moves to TP1. This locks in profit progressively so reversals can't
erase gains.

For single-TP signals, this manager is not engaged — the existing
trailing stop logic handles those positions.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

from src.core.enums import OrderSide

logger = logging.getLogger(__name__)

LOT_STEP = 0.01  # MT5 minimum lot increment


def _round_lots(volume: float) -> float:
    """Round volume down to nearest lot step."""
    return math.floor(volume / LOT_STEP) * LOT_STEP


@dataclass
class PartialProfitState:
    """Tracking state for one position's partial profit levels."""

    ticket: int
    side: OrderSide
    entry_price: float
    original_volume: float
    tp_levels: list[float]  # sorted by distance from entry
    levels_hit: list[int] = field(default_factory=list)  # indices of hit levels

    @property
    def n_levels(self) -> int:
        return len(self.tp_levels)

    def portion_volume(self, level_idx: int) -> float:
        """Calculate volume to close for a given level index.

        Divides evenly, last portion gets the remainder to avoid rounding dust.
        """
        if self.n_levels == 0:
            return 0.0

        base = _round_lots(self.original_volume / self.n_levels)
        if base < LOT_STEP:
            base = LOT_STEP

        if level_idx == self.n_levels - 1:
            # Last level: close whatever remains
            closed_so_far = base * (self.n_levels - 1)
            remainder = _round_lots(self.original_volume - closed_so_far)
            return max(remainder, LOT_STEP)

        return base

    def new_sl_after_hit(self, level_idx: int, breakeven_buffer: float = 1.0) -> float:
        """Calculate new SL after a TP level is hit.

        - After TP1: SL moves to entry +/- buffer (breakeven)
        - After TP2+: SL moves to previous TP level
        """
        if level_idx == 0:
            # Breakeven: entry price + small buffer in favorable direction
            if self.side == OrderSide.BUY:
                return self.entry_price + breakeven_buffer
            else:
                return self.entry_price - breakeven_buffer
        else:
            # Move SL to previous TP level
            return self.tp_levels[level_idx - 1]


@dataclass
class PartialCloseAction:
    """Instruction to partially close a position and move SL."""

    ticket: int
    symbol: str
    close_volume: float
    new_sl: float
    level_idx: int
    level_price: float


class PartialProfitManager:
    """Tracks TP levels per position and triggers partial closes."""

    def __init__(self, breakeven_buffer: float = 1.0) -> None:
        self._tracked: dict[int, PartialProfitState] = {}
        self._breakeven_buffer = breakeven_buffer

    def register(
        self,
        ticket: int,
        side: OrderSide,
        volume: float,
        entry_price: float,
        tp_levels: list[float],
    ) -> None:
        """Register a new position with its TP levels for partial profit tracking."""
        if len(tp_levels) < 2:
            return  # Single TP — handled by normal trailing stop

        # Sort levels by distance from entry (nearest first)
        if side == OrderSide.BUY:
            sorted_levels = sorted(tp_levels)
        else:
            sorted_levels = sorted(tp_levels, reverse=True)

        state = PartialProfitState(
            ticket=ticket,
            side=side,
            entry_price=entry_price,
            original_volume=volume,
            tp_levels=sorted_levels,
        )

        self._tracked[ticket] = state
        logger.info(
            "Partial profit registered: ticket=%d %s %.2f lots, %d TP levels %s",
            ticket, side.value, volume, len(sorted_levels), sorted_levels,
        )

    def check(
        self, ticket: int, current_price: float, symbol: str
    ) -> list[PartialCloseAction]:
        """Check if current price has hit any unhit TP level.

        Returns a list of actions (can be multiple if price gapped past
        several levels at once).
        """
        state = self._tracked.get(ticket)
        if state is None:
            return []

        actions: list[PartialCloseAction] = []

        for idx, tp_price in enumerate(state.tp_levels):
            if idx in state.levels_hit:
                continue

            # Check if price has reached this TP level
            hit = False
            if state.side == OrderSide.BUY and current_price >= tp_price:
                hit = True
            elif state.side == OrderSide.SELL and current_price <= tp_price:
                hit = True

            if not hit:
                break  # Levels are sorted, no point checking further

            # Skip the last level — let MT5's own TP handle the final close
            if idx == state.n_levels - 1:
                state.levels_hit.append(idx)
                logger.info(
                    "Partial profit: ticket=%d TP%d (final) at %.2f — letting MT5 TP close",
                    ticket, idx + 1, tp_price,
                )
                break

            state.levels_hit.append(idx)
            close_vol = state.portion_volume(idx)
            new_sl = state.new_sl_after_hit(idx, self._breakeven_buffer)

            actions.append(PartialCloseAction(
                ticket=ticket,
                symbol=symbol,
                close_volume=close_vol,
                new_sl=round(new_sl, 2),
                level_idx=idx,
                level_price=tp_price,
            ))

            logger.info(
                "Partial profit HIT: ticket=%d TP%d=%.2f → close %.2f lots, SL→%.2f",
                ticket, idx + 1, tp_price, close_vol, new_sl,
            )

        return actions

    def is_tracked(self, ticket: int) -> bool:
        """Check if a position is being tracked for partial profit."""
        return ticket in self._tracked

    def remove(self, ticket: int) -> None:
        """Clean up when a position is fully closed."""
        if ticket in self._tracked:
            del self._tracked[ticket]

    def get_state(self, ticket: int) -> Optional[PartialProfitState]:
        """Get tracking state for a position (for persistence)."""
        return self._tracked.get(ticket)

    def restore(self, states: dict[int, PartialProfitState]) -> None:
        """Restore tracked states from database (after restart)."""
        self._tracked.update(states)
        if states:
            logger.info(
                "Restored %d partial profit state(s) from database", len(states)
            )

    @property
    def tracked_tickets(self) -> set[int]:
        return set(self._tracked.keys())
