"""Event bus and typed events for the trading bot.

The event system is the backbone of the architecture. Components communicate
exclusively through events, making them independently testable and replaceable.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine

from src.core.models import ModifyOrder, Order, Position, Signal

logger = logging.getLogger(__name__)

# Type alias for async event handlers
EventHandler = Callable[["Event"], Coroutine[Any, Any, None]]


@dataclass
class Event:
    """Base event class."""

    timestamp: datetime
    event_type: str = ""


@dataclass
class SignalEvent(Event):
    """Emitted when a new trading signal is parsed from Telegram."""

    event_type: str = field(default="SIGNAL", init=False)
    signal: Signal | None = None


@dataclass
class SignalAmendmentEvent(Event):
    """Emitted when a follow-up message updates an existing signal's SL/TP."""

    event_type: str = field(default="SIGNAL_AMENDMENT", init=False)
    modify_order: ModifyOrder | None = None
    channel_id: str = ""
    symbol: str = ""


@dataclass
class OrderEvent(Event):
    """Emitted when an order should be placed."""

    event_type: str = field(default="ORDER", init=False)
    order: Order | None = None


@dataclass
class ModifyOrderEvent(Event):
    """Emitted when a position's SL/TP should be modified."""

    event_type: str = field(default="MODIFY_ORDER", init=False)
    modify_order: ModifyOrder | None = None


@dataclass
class FillEvent(Event):
    """Emitted when an order is filled."""

    event_type: str = field(default="FILL", init=False)
    order: Order | None = None
    fill_price: float = 0.0
    fill_volume: float = 0.0
    commission: float = 0.0
    slippage: float = 0.0


@dataclass
class PositionClosedEvent(Event):
    """Emitted when the position monitor detects a closed position."""

    event_type: str = field(default="POSITION_CLOSED", init=False)
    position: Position | None = None
    close_price: float = 0.0
    pnl: float = 0.0
    close_reason: str = ""


@dataclass
class ForeignPositionEvent(Event):
    """Emitted when a position is detected on MT5 that the bot did not place.

    Detection: position whose `magic` field is not the bot's magic number
    (200000). Comes from manual placement, another EA, or a leaked master
    password. Bot does NOT auto-close — racing the human is dangerous.
    Alert-only is the P1 behaviour; Slack/Telegram notifiers handle it.
    """

    event_type: str = field(default="FOREIGN_POSITION", init=False)
    position: Position | None = None
    message: str = "Foreign position detected — not placed by bot"


class EventBus:
    """Async publish-subscribe event bus.

    Handlers are invoked in registration order. If a handler raises,
    the error is logged but other handlers still execute.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = {}
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        self._running = False

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        """Register a handler for an event type."""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: EventHandler) -> None:
        """Remove a handler for an event type."""
        if event_type in self._handlers:
            self._handlers[event_type] = [
                h for h in self._handlers[event_type] if h is not handler
            ]

    async def publish(self, event: Event) -> None:
        """Add an event to the processing queue."""
        await self._queue.put(event)

    async def emit(self, event: Event) -> None:
        """Process an event immediately (bypasses queue)."""
        await self._dispatch(event)

    _HEARTBEAT_PATH = Path("/tmp/bot_heartbeat")

    async def process(self) -> None:
        """Main event processing loop. Runs until stop() is called."""
        self._running = True
        while self._running:
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                await self._dispatch(event)
                self._queue.task_done()
            except asyncio.TimeoutError:
                pass
            # Touch heartbeat so Docker healthcheck knows we're alive
            try:
                self._HEARTBEAT_PATH.touch()
            except OSError:
                pass

    def stop(self) -> None:
        """Signal the processing loop to stop."""
        self._running = False

    async def drain(self) -> None:
        """Process all remaining events in the queue."""
        while not self._queue.empty():
            event = self._queue.get_nowait()
            await self._dispatch(event)
            self._queue.task_done()

    async def _dispatch(self, event: Event) -> None:
        """Dispatch an event to all registered handlers."""
        handlers = self._handlers.get(event.event_type, [])
        for handler in handlers:
            try:
                await handler(event)
            except Exception:
                logger.exception(
                    "Error in event handler %s for %s",
                    handler.__qualname__,
                    event.event_type,
                )
