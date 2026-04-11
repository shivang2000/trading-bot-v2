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
        giveback_pct: float = 0.10,
        max_giveback: float = 10.0,
        activation_profit: float = 5.0,
    ) -> None:
        self._atr_multiplier = atr_multiplier
        self._activation_pct = activation_pct
        self._trailing_stops: dict[int, float] = {}  # ticket → current trailing SL
        self._giveback_pct: float = giveback_pct  # default 0.10
        self._max_giveback: float = max_giveback  # default 10.0
        self._activation_profit: float = activation_profit  # default 5.0
        self._peak_prices: dict[int, float] = {}  # ticket → peak price

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

    def update_profit_trail(
        self,
        ticket: int,
        side: OrderSide,
        current_price: float,
        open_price: float,
    ) -> float | None:
        """Profit-percentage trailing stop.

        SL = peak_price - min(peak_profit * giveback_pct, max_giveback_dollars)

        Examples (giveback_pct=0.10, max_giveback=$10):
          Buy at $4000, peak at $4050 → profit=$50, giveback=min($5,$10)=$5, SL=$4045
          Buy at $4000, peak at $4010 → profit=$10, giveback=min($1,$10)=$1, SL=$4009
          Buy at $4000, peak at $4200 → profit=$200, giveback=min($20,$10)=$10, SL=$4190
        """
        # Calculate peak profit
        if side == OrderSide.BUY:
            peak = max(current_price, self._peak_prices.get(ticket, current_price))
            self._peak_prices[ticket] = peak
            peak_profit = peak - open_price
        else:
            peak = min(current_price, self._peak_prices.get(ticket, current_price))
            self._peak_prices[ticket] = peak
            peak_profit = open_price - peak

        # Not enough profit to activate
        if peak_profit < self._activation_profit:
            return None

        # Max giveback = min(percentage of peak profit, absolute cap)
        giveback = min(peak_profit * self._giveback_pct, self._max_giveback)

        # Calculate new SL with breakeven floor
        if side == OrderSide.BUY:
            new_sl = peak - giveback
            new_sl = max(new_sl, open_price)  # never below entry
        else:
            new_sl = peak + giveback
            new_sl = min(new_sl, open_price)  # never above entry

        # Ratchet: only move SL in favorable direction
        current_sl = self._trailing_stops.get(ticket)
        if current_sl is not None:
            if side == OrderSide.BUY and new_sl <= current_sl:
                return None
            if side == OrderSide.SELL and new_sl >= current_sl:
                return None

        self._trailing_stops[ticket] = new_sl
        logger.info(
            "Profit trail: ticket=%d peak=%.2f profit=%.2f giveback=%.2f SL=%.2f",
            ticket, peak, peak_profit, giveback, new_sl,
        )
        return new_sl

    def get_stop(self, ticket: int) -> float | None:
        """Get the current trailing stop level for a ticket."""
        return self._trailing_stops.get(ticket)

    def remove(self, ticket: int) -> None:
        """Remove tracking when a position is closed."""
        self._trailing_stops.pop(ticket, None)
        self._peak_prices.pop(ticket, None)

    def restore(self, stops: dict[int, float]) -> None:
        """Restore trailing stops from persisted data (after restart)."""
        self._trailing_stops.update(stops)
        if stops:
            logger.info("Restored %d trailing stop(s) from database", len(stops))
