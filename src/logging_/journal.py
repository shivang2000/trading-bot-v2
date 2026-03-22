"""Trade journal: records completed trades for analysis.

Simplified from v1 — no confluence/strategy scoring. Tracks trades
by channel source so we can learn which Telegram channels perform best.
Uses the shared TrackingDB for storage.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.core.events import Event, EventBus, FillEvent, PositionClosedEvent
from src.core.models import Order, Position
from src.tracking.database import TrackingDB

logger = logging.getLogger(__name__)


class TradeJournal:
    """Records trade opens and closes in the tracking database."""

    def __init__(self, event_bus: EventBus, tracking_db: TrackingDB) -> None:
        self._event_bus = event_bus
        self._db = tracking_db

    async def initialize(self) -> None:
        """Subscribe to fill and position close events."""
        self._event_bus.subscribe("FILL", self._on_fill)
        self._event_bus.subscribe("POSITION_CLOSED", self._on_position_closed)
        logger.info("TradeJournal initialized")

    async def _on_fill(self, event: Event) -> None:
        """Record a new trade when an order is filled."""
        if not isinstance(event, FillEvent) or event.order is None:
            return

        order = event.order
        try:
            await self._db.store_trade(
                signal_id=order.signal_id,
                channel_id=None,
                mt5_ticket=order.ticket or 0,
                action=order.side.value,
                symbol=order.symbol,
                volume=event.fill_volume,
                entry_price=event.fill_price,
                stop_loss=order.stop_loss,
                take_profit=order.take_profit,
            )
            logger.info(
                "Journal: trade opened ticket=%d %s %s %.2f @ %.5f",
                order.ticket or 0,
                order.side.value,
                order.symbol,
                event.fill_volume,
                event.fill_price,
            )
        except Exception:
            logger.exception("Failed to journal trade open for ticket %d", order.ticket or 0)

    async def _on_position_closed(self, event: Event) -> None:
        """Record trade close details."""
        if not isinstance(event, PositionClosedEvent):
            return

        position = event.position
        try:
            trade = await self._db.get_trade_by_ticket(position.ticket)
            if trade:
                await self._db.close_trade(
                    trade_id=trade["id"],
                    close_price=event.close_price,
                    pnl=event.pnl,
                    close_reason=event.close_reason or "unknown",
                )
                result = "WIN" if event.pnl >= 0 else "LOSS"
                logger.info(
                    "Journal: trade closed ticket=%d %s %s P&L=$%.2f",
                    position.ticket,
                    position.symbol,
                    result,
                    event.pnl,
                )
            else:
                logger.debug(
                    "Journal: no trade record for closed ticket %d", position.ticket
                )
        except Exception:
            logger.exception(
                "Failed to journal trade close for ticket %d", position.ticket
            )
