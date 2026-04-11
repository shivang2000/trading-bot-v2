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

from src.config.schema import PartialProfitConfig, TrailingStopConfig
from src.core.enums import OrderSide, OrderType
from src.core.events import EventBus, ModifyOrderEvent, OrderEvent, PositionClosedEvent
from src.core.models import ModifyOrder, Order, Position
from src.monitoring.partial_profit_manager import PartialProfitManager, PartialProfitState
from src.mt5.client import AsyncMT5Client
from src.risk.trailing_stop import TrailingStopManager
from src.safety.emergency import EmergencyStop
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
        initial_balance: float = 30.0,
        prop_firm_guard=None,
        partial_profit_config: PartialProfitConfig | None = None,
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
        self._emergency = EmergencyStop(
            max_daily_loss_usd=initial_balance * 0.08,   # 8% of initial
            max_drawdown_usd=initial_balance * 0.20,     # 20% of initial
        )
        self._emergency_triggered = False
        self._prop_firm_guard = prop_firm_guard
        self._friday_close_triggered = False

        # Trailing stop management
        self._ts_config = trailing_stop_config
        self._trailing_manager: TrailingStopManager | None = None
        if trailing_stop_config and trailing_stop_config.enabled:
            self._trailing_manager = TrailingStopManager(
                atr_multiplier=trailing_stop_config.atr_multiplier,
                activation_pct=trailing_stop_config.activation_pct,
            )
        # Partial profit manager (multi-TP partial closes)
        pp_cfg = partial_profit_config
        self._partial_profit: PartialProfitManager | None = None
        if pp_cfg and pp_cfg.enabled:
            self._partial_profit = PartialProfitManager(
                breakeven_buffer=pp_cfg.breakeven_buffer_points,
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

        # Check partial profit levels (close portions at each TP)
        if self._partial_profit and current_tickets:
            await self._check_partial_profits(current_tickets)

        # Update trailing stops for open positions
        if self._trailing_manager and current_tickets:
            await self._update_trailing_stops(current_tickets)

        # Update known state
        self._known_tickets = current_tickets

        # Update cached positions for RiskManager (avoids deadlock)
        if self._positions_callback:
            self._positions_callback(list(current_tickets.values()))

        # Emergency stop check — close all positions if daily loss exceeds limit
        if self._account_state_func and current_tickets and not self._emergency_triggered:
            try:
                account = self._account_state_func()
                if self._emergency.check(account):
                    self._emergency_triggered = True
                    logger.critical("EMERGENCY STOP TRIGGERED — closing bot positions")
                    for pos in list(current_tickets.values()):
                        if not self._is_bot_position(pos):
                            logger.info("Skipping manual position ticket=%d on emergency close", pos.ticket)
                            continue
                        close_side = OrderSide.SELL if pos.side == OrderSide.BUY else OrderSide.BUY
                        order = Order(
                            symbol=pos.symbol, side=close_side,
                            order_type=OrderType.MARKET, volume=pos.volume,
                            magic=200000, comment="EMERGENCY_STOP",
                        )
                        await self._event_bus.publish(
                            OrderEvent(timestamp=datetime.now(timezone.utc), order=order)
                        )
            except Exception:
                logger.debug("Emergency check skipped", exc_info=True)

        now = datetime.now(timezone.utc)

        # Friday auto-close — actually close open positions before weekend gap
        if self._prop_firm_guard and current_tickets:
            if now.weekday() != 4:
                self._friday_close_triggered = False  # reset on non-Friday
            elif not self._friday_close_triggered and self._prop_firm_guard.should_friday_close(now):
                self._friday_close_triggered = True
                logger.warning(
                    "FRIDAY AUTO-CLOSE: closing %d position(s) before weekend gap",
                    len(current_tickets),
                )
                for pos in list(current_tickets.values()):
                    if not self._is_bot_position(pos):
                        logger.info("Skipping manual position ticket=%d on Friday close", pos.ticket)
                        continue
                    close_side = OrderSide.SELL if pos.side == OrderSide.BUY else OrderSide.BUY
                    order = Order(
                        symbol=pos.symbol, side=close_side,
                        order_type=OrderType.MARKET, volume=pos.volume,
                        magic=200000, comment="FRIDAY_CLOSE",
                    )
                    await self._event_bus.publish(
                        OrderEvent(timestamp=datetime.now(timezone.utc), order=order)
                    )

        # Periodic PropFirmGuard equity check — catches rapid drawdown between signals
        if self._prop_firm_guard and self._account_state_func and current_tickets:
            try:
                account = self._account_state_func()
                can_trade, reason = self._prop_firm_guard.can_trade(account.equity, now)
                if not can_trade and ("DANGER:" in reason or "Daily loss" in reason):
                    logger.critical(
                        "PROPFIRM GUARD (periodic): %s — closing all %d position(s)",
                        reason, len(current_tickets),
                    )
                    for pos in list(current_tickets.values()):
                        if not self._is_bot_position(pos):
                            logger.info("Skipping manual position ticket=%d on propfirm guard close", pos.ticket)
                            continue
                        close_side = OrderSide.SELL if pos.side == OrderSide.BUY else OrderSide.BUY
                        order = Order(
                            symbol=pos.symbol, side=close_side,
                            order_type=OrderType.MARKET, volume=pos.volume,
                            magic=200000, comment="PROPFIRM_GUARD",
                        )
                        await self._event_bus.publish(
                            OrderEvent(timestamp=datetime.now(timezone.utc), order=order)
                        )
            except Exception:
                logger.debug("PropFirm periodic check skipped", exc_info=True)

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
            try:
                await self._db.delete_trailing_stop(position.ticket)
            except Exception:
                pass

        # Clean up partial profit tracking
        if self._partial_profit:
            self._partial_profit.remove(position.ticket)
            try:
                await self._db.delete_partial_profit_state(position.ticket)
            except Exception:
                pass

        # Close in bot_positions table
        try:
            await self._db.close_bot_position(
                position.ticket, close_price, pnl, "market"
            )
        except Exception:
            pass

    @staticmethod
    def _is_bot_position(pos) -> bool:
        """Check if a position was opened by the bot (comment starts with 'tg:')."""
        return bool(pos.comment and pos.comment.startswith("tg:"))

    async def _check_partial_profits(
        self, positions: dict[int, Position]
    ) -> None:
        """Check and execute partial profit closes for tracked positions."""
        for ticket, pos in positions.items():
            try:
                if not self._is_bot_position(pos):
                    continue
                if not self._partial_profit.is_tracked(ticket):
                    continue

                current_price = pos.current_price or pos.open_price
                actions = self._partial_profit.check(ticket, current_price, pos.symbol)

                for action in actions:
                    # 1. Partial close: send counter-direction market order
                    close_side = OrderSide.SELL if pos.side == OrderSide.BUY else OrderSide.BUY
                    close_order = Order(
                        symbol=pos.symbol,
                        side=close_side,
                        order_type=OrderType.MARKET,
                        volume=action.close_volume,
                        magic=200000,
                        comment=f"partial:TP{action.level_idx + 1}",
                        position_ticket=ticket,
                    )
                    await self._event_bus.publish(
                        OrderEvent(
                            timestamp=datetime.now(timezone.utc),
                            order=close_order,
                        )
                    )

                    # 2. Move SL to breakeven / previous TP level
                    modify = ModifyOrder(
                        ticket=ticket,
                        symbol=pos.symbol,
                        stop_loss=action.new_sl,
                        take_profit=pos.take_profit,  # keep original final TP
                    )
                    await self._event_bus.publish(
                        ModifyOrderEvent(
                            timestamp=datetime.now(timezone.utc),
                            modify_order=modify,
                        )
                    )

                    logger.info(
                        "Partial close: ticket=%d TP%d close %.2f lots, SL→%.2f",
                        ticket, action.level_idx + 1,
                        action.close_volume, action.new_sl,
                    )

                    # 3. Persist updated state
                    state = self._partial_profit.get_state(ticket)
                    if state:
                        try:
                            await self._db.save_partial_profit_state(
                                ticket=ticket,
                                tp_levels=state.tp_levels,
                                levels_hit=state.levels_hit,
                                original_volume=state.original_volume,
                                entry_price=state.entry_price,
                                side=state.side.value,
                            )
                        except Exception:
                            pass

            except Exception:
                logger.warning(
                    "Partial profit check failed for ticket %d",
                    ticket, exc_info=True,
                )

    async def _update_trailing_stops(
        self, positions: dict[int, Position]
    ) -> None:
        """Update trailing stops for bot-opened positions only."""
        for ticket, pos in positions.items():
            try:
                if not self._is_bot_position(pos):
                    continue  # Skip manual positions
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

                # Also check profit-based trailing (tighter)
                profit_sl = self._trailing_manager.update_profit_trail(
                    ticket=ticket,
                    side=pos.side,
                    current_price=pos.current_price or pos.open_price,
                    open_price=pos.open_price,
                )
                if profit_sl is not None:
                    # Use the tighter SL (profit trail or ATR trail)
                    if new_sl is None:
                        new_sl = profit_sl
                    elif pos.side == OrderSide.BUY:
                        new_sl = max(new_sl, profit_sl)  # higher SL = tighter for BUY
                    else:
                        new_sl = min(new_sl, profit_sl)  # lower SL = tighter for SELL

                if new_sl is not None:
                    # Persist trailing stop to DB (survives restart)
                    try:
                        await self._db.save_trailing_stop(ticket, round(new_sl, 5))
                    except Exception:
                        pass

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
