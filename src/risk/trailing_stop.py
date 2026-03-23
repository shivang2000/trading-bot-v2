"""Trailing stop manager for dynamic stop-loss adjustment.

Tracks trailing stop levels per position ticket. The stop ratchets in the
favorable direction only — it never moves backward. Uses ATR-based distance
to set the trailing offset from current price.

Ported from v1 trading-bot with minimal changes.
"""

from __future__ import annotations

import logging

from src.core.enums import OrderSide

logger = logging.getLogger(__name__)


class TrailingStopManager:
    """Manages trailing stops for open positions.

    Usage:
        manager = TrailingStopManager(atr_multiplier=1.5, activation_pct=0.5)
        # Call on each poll cycle for each open position:
        new_sl = manager.update(ticket, side, current_price, atr, initial_sl, take_profit)
        if new_sl is not None:
            # Update position's stop_loss via MT5
    """

    def __init__(
        self,
        atr_multiplier: float = 1.5,
        activation_pct: float = 0.5,
    ) -> None:
        self._atr_multiplier = atr_multiplier
        self._activation_pct = activation_pct
        self._trailing_stops: dict[int, float] = {}  # ticket → current trailing SL

    def update(
        self,
        ticket: int,
        side: OrderSide,
        current_price: float,
        atr: float,
        initial_sl: float | None = None,
        take_profit: float | None = None,
        open_price: float | None = None,
    ) -> float | None:
        """Update trailing stop for a position.

        Returns the new SL level if it moved, None if unchanged or not yet activated.

        Activation: trailing stop only kicks in after the position has moved
        activation_pct of the way toward take_profit. This prevents premature
        stop tightening on small moves.
        """
        # Check activation threshold
        if take_profit is not None and open_price is not None and self._activation_pct > 0:
            tp_distance = abs(take_profit - open_price)
            current_distance = (
                current_price - open_price if side == OrderSide.BUY
                else open_price - current_price
            )
            if tp_distance > 0 and current_distance < tp_distance * self._activation_pct:
                return None  # Not yet activated

        trail_distance = atr * self._atr_multiplier

        if side == OrderSide.BUY:
            new_sl = current_price - trail_distance
        else:
            new_sl = current_price + trail_distance

        current_sl = self._trailing_stops.get(ticket)

        if current_sl is None:
            # First time — initialize from initial_sl or calculated value
            if initial_sl is not None:
                if side == OrderSide.BUY:
                    new_sl = max(initial_sl, new_sl)
                else:
                    new_sl = min(initial_sl, new_sl)
            self._trailing_stops[ticket] = new_sl
            logger.info(
                "Trailing stop activated: ticket=%d sl=%.2f", ticket, new_sl,
            )
            return new_sl

        # Ratchet: only move in favorable direction
        if side == OrderSide.BUY:
            if new_sl > current_sl:
                self._trailing_stops[ticket] = new_sl
                logger.info(
                    "Trailing stop moved: ticket=%d %.2f → %.2f",
                    ticket, current_sl, new_sl,
                )
                return new_sl
        else:
            if new_sl < current_sl:
                self._trailing_stops[ticket] = new_sl
                logger.info(
                    "Trailing stop moved: ticket=%d %.2f → %.2f",
                    ticket, current_sl, new_sl,
                )
                return new_sl

        return None  # No change

    def get_stop(self, ticket: int) -> float | None:
        """Get the current trailing stop level for a ticket."""
        return self._trailing_stops.get(ticket)

    def remove(self, ticket: int) -> None:
        """Remove tracking when a position is closed."""
        self._trailing_stops.pop(ticket, None)

    def restore(self, stops: dict[int, float]) -> None:
        """Restore trailing stops from persisted data (after restart)."""
        self._trailing_stops.update(stops)
        if stops:
            logger.info("Restored %d trailing stop(s) from database", len(stops))
