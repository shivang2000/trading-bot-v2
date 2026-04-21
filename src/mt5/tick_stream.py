"""Tick Stream Service — Real-time price feed from MT5.

Provides tick-level price updates for position monitoring,
trailing stops, and partial profit management. Runs a fast
polling loop (100-500ms) that fetches latest ticks and
dispatches to registered callbacks.

This replaces the 1-second position monitor poll with
near-real-time tick processing, similar to MT5 Expert
Advisor OnTick() behavior.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from src.core.models import Tick
from src.mt5.client import AsyncMT5Client

logger = logging.getLogger(__name__)


class TickStream:
    """Real-time tick subscription service.

    Polls MT5 for latest ticks at configurable interval and
    dispatches to registered callbacks.

    Usage:
        stream = TickStream(mt5_client, symbols=["XAUUSD", "US30"])
        stream.on_tick(my_callback)  # async def my_callback(tick: Tick)
        await stream.start()
    """

    def __init__(
        self,
        mt5_client: AsyncMT5Client,
        symbols: list[str] | None = None,
        poll_interval_ms: int = 200,
        enabled: bool = True,
    ) -> None:
        self._mt5 = mt5_client
        self._symbols = symbols or []
        self._poll_ms = poll_interval_ms
        self._enabled = enabled
        self._callbacks: list[Callable] = []
        self._on_new_bar_callbacks: list[Callable] = []
        self._running = False
        self._task: asyncio.Task | None = None

        # Track last tick time per symbol to detect new bars
        self._last_tick_time: dict[str, datetime] = {}
        self._last_bar_minute: dict[str, int] = {}

    def on_tick(self, callback: Callable) -> None:
        """Register a callback for every tick. async def cb(tick: Tick)."""
        self._callbacks.append(callback)

    def on_new_bar(self, callback: Callable) -> None:
        """Register a callback for new M5 bar formation. async def cb(symbol: str, bar_time: datetime)."""
        self._on_new_bar_callbacks.append(callback)

    async def start(self) -> None:
        """Start the tick polling loop."""
        if not self._enabled or not self._symbols:
            logger.info("TickStream disabled or no symbols configured")
            return

        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "TickStream started: %d symbols, %dms interval",
            len(self._symbols), self._poll_ms,
        )

    async def stop(self) -> None:
        """Stop the tick polling loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("TickStream stopped")

    async def _poll_loop(self) -> None:
        """Fast polling loop — fetches ticks for all symbols."""
        interval = self._poll_ms / 1000.0

        while self._running:
            try:
                for symbol in self._symbols:
                    try:
                        tick = await self._mt5.symbol_info_tick(symbol)
                        if tick is None:
                            continue

                        # Dispatch tick to all callbacks
                        for cb in self._callbacks:
                            try:
                                await cb(tick)
                            except Exception:
                                logger.debug("Tick callback error for %s", symbol, exc_info=True)

                        # Detect new bar (M5 = every 5 minutes)
                        self._check_new_bar(symbol, tick.timestamp)

                    except Exception:
                        logger.debug("Tick fetch error for %s", symbol, exc_info=True)

            except Exception:
                logger.warning("TickStream poll error", exc_info=True)

            await asyncio.sleep(interval)

    def _check_new_bar(self, symbol: str, tick_time: datetime) -> None:
        """Detect when a new M5 bar forms based on tick timestamps."""
        current_minute = tick_time.minute // 5 * 5  # Round to M5 boundary
        last_minute = self._last_bar_minute.get(symbol, -1)

        if current_minute != last_minute and last_minute >= 0:
            # New M5 bar formed — notify callbacks
            bar_time = tick_time.replace(
                minute=current_minute, second=0, microsecond=0
            )
            for cb in self._on_new_bar_callbacks:
                try:
                    asyncio.create_task(cb(symbol, bar_time))
                except Exception:
                    logger.debug("New bar callback error for %s", symbol, exc_info=True)

        self._last_bar_minute[symbol] = current_minute
