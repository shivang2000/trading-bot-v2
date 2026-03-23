"""Position monitor: polls MT5 for position changes.

Runs a background loop that:
1. Fetches all open positions from MT5 every N seconds
2. Compares with last known state
3. Detects position closes and records results
4. Publishes POSITION_CLOSED events for notifications
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

import pandas_ta as ta

from src.config.schema import TrailingStopConfig
from src.core.enums import OrderSide
from src.core.events import EventBus, ModifyOrderEvent, PositionClosedEvent
from src.core.models import ModifyOrder, Position
from src.mt5.client import AsyncMT5Client
from src.risk.trailing_stop import TrailingStopManager
from src.tracking.database import TrackingDB

logger = logging.getLogger(__name__)


class PositionMonitor:
    """Polls MT5 for position status and detects closes."""

    def __init__(
        self,
        mt5_client: AsyncMT5Client,
        event_bus: EventBus,
        tracking_db: TrackingDB,
        poll_interval: int = 30,
        account_state_func: Callable[[], Any] | None = None,
        trailing_stop_config: TrailingStopConfig | None = None,
        positions_callback: Callable[[list], None] | None = None,
    ) -> None:
        self._mt5 = mt5_client
        self._event_bus = event_bus
        self._db = tracking_db
        self._poll_interval = poll_interval
        self._account_state_func = account_state_func
        self._positions_callback = positions_callback
        self._known_tickets: dict[int, Position] = {}
        self._running = False
        self._task: asyncio.Task | None = None

        # Trailing stop management
        self._ts_config = trailing_stop_config
        self._trailing_manager: TrailingStopManager | None = None
        if trailing_stop_config and trailing_stop_config.enabled:
            self._trailing_manager = TrailingStopManager(
                atr_multiplier=trailing_stop_config.atr_multiplier,
                activation_pct=trailing_stop_config.activation_pct,
            )
        # Cache ATR values per symbol to avoid recalculating every poll
        self._atr_cache: dict[str, tuple[float, datetime]] = {}

    async def start(self) -> None:
        """Start the position monitoring loop."""
        self._running = True
        # Snapshot current positions on startup
        await self._sync_positions()
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(
            "PositionMonitor started (interval=%ds, tracking %d positions)",
            self._poll_interval,
            len(self._known_tickets),
        )

    async def stop(self) -> None:
        """Stop the monitoring loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("PositionMonitor stopped")

    async def _poll_loop(self) -> None:
        """Background loop that checks positions periodically."""
        while self._running:
            await asyncio.sleep(self._poll_interval)
            try:
                await self._check_positions()
            except Exception:
                logger.exception("Position monitor poll error (will retry next cycle)")

    async def _sync_positions(self) -> None:
        """Fetch current MT5 positions and populate known_tickets."""
        try:
            positions = await self._mt5.positions_get()
            if not positions:
                return

            for pos in positions:
                self._known_tickets[pos.ticket] = pos
        except Exception:
            logger.warning("Could not sync initial positions")

    async def _check_positions(self) -> None:
        """Compare current positions with known state, detect closes."""
        try:
            current_positions = await self._mt5.positions_get()
        except Exception:
            logger.warning("Failed to fetch positions from MT5")
            return

        current_tickets: dict[int, Position] = {}
        if current_positions:
            for pos in current_positions:
                current_tickets[pos.ticket] = pos

        # Detect closed positions (were known, no longer open)
        for ticket, old_pos in self._known_tickets.items():
            if ticket not in current_tickets:
                await self._handle_close(old_pos)

        # Detect new positions (not previously known)
        for ticket, pos in current_tickets.items():
            if ticket not in self._known_tickets:
                logger.info(
                    "New position detected: ticket=%d %s %s %.2f lots @ %.5f",
                    ticket, pos.side.value, pos.symbol, pos.volume, pos.open_price,
                )

        # Update trailing stops for open positions
        if self._trailing_manager and current_tickets:
            await self._update_trailing_stops(current_tickets)

        # Update known state
        self._known_tickets = current_tickets

        # Update cached positions for RiskManager (avoids deadlock)
        if self._positions_callback:
            self._positions_callback(list(current_tickets.values()))

    async def _handle_close(self, position: Position) -> None:
        """Handle a detected position close."""
        # Use last known profit and current_price from the Position snapshot
        pnl = position.profit + position.commission + position.swap
        close_price = position.current_price or position.open_price

        duration_seconds = 0.0
        now = datetime.now(timezone.utc)
        if position.open_time:
            try:
                open_time = position.open_time
                if open_time.tzinfo is None:
                    open_time = open_time.replace(tzinfo=timezone.utc)
                duration_seconds = (now - open_time).total_seconds()
            except (OSError, ValueError):
                pass

        logger.info(
            "Position CLOSED: ticket=%d %s %s %.2f lots | P&L: $%.2f | Duration: %.1fh",
            position.ticket,
            position.side.value,
            position.symbol,
            position.volume,
            pnl,
            duration_seconds / 3600,
        )

        # Update tracking database
        try:
            trade = await self._db.get_trade_by_ticket(position.ticket)
            if trade:
                await self._db.close_trade(
                    trade_id=trade["id"],
                    close_price=close_price,
                    pnl=pnl,
                    close_reason="market",
                )
        except Exception:
            logger.warning("Could not update tracking DB for ticket %d", position.ticket)

        # Publish event for notifications
        await self._event_bus.publish(
            PositionClosedEvent(
                timestamp=now,
                position=position,
                close_price=close_price,
                pnl=pnl,
                close_reason="market",
            )
        )

        # Clean up trailing stop tracking
        if self._trailing_manager:
            self._trailing_manager.remove(position.ticket)

    async def _update_trailing_stops(
        self, positions: dict[int, Position]
    ) -> None:
        """Update trailing stops for all open positions."""
        for ticket, pos in positions.items():
            try:
                atr = await self._get_atr(pos.symbol)
                if atr is None or atr <= 0:
                    continue

                new_sl = self._trailing_manager.update(
                    ticket=ticket,
                    side=pos.side,
                    current_price=pos.current_price or pos.open_price,
                    atr=atr,
                    initial_sl=pos.stop_loss,
                    take_profit=pos.take_profit,
                    open_price=pos.open_price,
                )

                if new_sl is not None:
                    # Publish modify order to move SL on MT5
                    modify = ModifyOrder(
                        ticket=ticket,
                        symbol=pos.symbol,
                        stop_loss=round(new_sl, 5),
                        take_profit=pos.take_profit,
                    )
                    await self._event_bus.publish(
                        ModifyOrderEvent(
                            timestamp=datetime.now(timezone.utc),
                            modify_order=modify,
                        )
                    )

            except Exception:
                logger.warning(
                    "Trailing stop update failed for ticket %d", ticket,
                    exc_info=True,
                )

    async def _get_atr(self, symbol: str) -> float | None:
        """Get ATR for a symbol, cached for 5 minutes."""
        now = datetime.now(timezone.utc)

        # Check cache
        if symbol in self._atr_cache:
            cached_val, cached_time = self._atr_cache[symbol]
            if (now - cached_time).total_seconds() < 300:
                return cached_val

        try:
            cfg = self._ts_config
            timeframe = cfg.atr_timeframe if cfg else "H1"
            period = cfg.atr_period if cfg else 14

            bars = await self._mt5.get_bars(symbol, timeframe, count=period + 5)
            if bars is None or bars.empty or len(bars) < period:
                return None

            atr_series = ta.atr(bars["high"], bars["low"], bars["close"], length=period)
            if atr_series is None or atr_series.empty:
                return None

            val = float(atr_series.iloc[-1])
            self._atr_cache[symbol] = (val, now)
            return val

        except Exception:
            logger.warning("ATR calculation failed for %s", symbol)
            return None
