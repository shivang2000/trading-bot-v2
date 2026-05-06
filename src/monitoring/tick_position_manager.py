"""Tick-driven position manager — runs trailing stop and partial-profit
logic on every tick, replacing the 30s poll-driven path in PositionMonitor.

Wiring (in `src/main.py`):
    tick_stream = TickStream(mt5, symbols=cfg.tick_engine.symbols, ...)
    tick_pm = TickPositionManager(
        event_bus, db, trailing_manager, partial_profit_manager,
        positions_func=lambda: cached_positions, ...
    )
    tick_stream.on_tick(tick_pm.handle_tick)
    await tick_stream.start()

Per tick, for each open BOT position on that symbol:
  1. TrailingStopManager.update + update_profit_trail → candidate new SL
  2. PartialProfitManager.evaluate_on_tick → list of partial-close actions
  3. Rate-limited modify queue: at most one SL change per ticket per
     `modify_rate_limit_seconds`. Drops stale intermediate updates.
  4. Partial closes go straight onto the EventBus (close-volume orders).

Entry signals are NOT in scope here — they remain bar-driven via
SignalGenerator. This module owns exits and SL adjustments only.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable

from src.config.schema import TickEngineConfig
from src.core.enums import OrderSide, OrderType
from src.core.events import (
    Event,
    EventBus,
    ModifyOrderEvent,
    OrderEvent,
    PositionClosedEvent,
)
from src.core.models import ModifyOrder, Order, Position, Tick
from src.monitoring.partial_profit_manager import PartialProfitManager
from src.risk.trailing_stop import TrailingStopManager
from src.tracking.database import TrackingDB

logger = logging.getLogger(__name__)

BOT_MAGIC = 200000


class TickPositionManager:
    """Tick subscriber that drives trailing stops + partial profits.

    Stateless w.r.t. positions — relies on `positions_func` callback to
    fetch current open positions (same cached list that PositionMonitor
    populates every poll cycle). This avoids racing MT5 from the tick
    callback.
    """

    def __init__(
        self,
        event_bus: EventBus,
        tracking_db: TrackingDB,
        trailing_manager: TrailingStopManager | None,
        partial_profit_manager: PartialProfitManager | None,
        positions_func: Callable[[], list[Position]],
        atr_func: Callable[[str], Any],  # async, returns float | None
        config: TickEngineConfig,
    ) -> None:
        self._event_bus = event_bus
        self._db = tracking_db
        self._trailing = trailing_manager
        self._partial = partial_profit_manager
        self._positions_func = positions_func
        self._atr_func = atr_func
        self._cfg = config

        # Per-ticket modify throttle: ticket → last_modify_monotonic
        self._last_modify_at: dict[int, float] = {}
        # Per-ticket last-sent SL — drop modify if SL didn't move enough
        self._last_sent_sl: dict[int, float] = {}
        # Per-symbol ATR cache to avoid hammering MT5 on every tick
        self._atr_cache: dict[str, tuple[float, float]] = {}  # symbol → (atr, monotonic_at)
        self._atr_cache_ttl = 60.0  # seconds

        # Tick counters for diagnostic logging
        self._ticks_seen = 0
        self._modifies_sent = 0
        self._modifies_throttled = 0
        self._partial_actions_sent = 0

    async def handle_tick(self, tick: Tick) -> None:
        """Entry point registered with TickStream.on_tick."""
        try:
            self._ticks_seen += 1
            positions = self._positions_func() or []
            # Filter to bot positions on this tick's symbol
            relevant = [p for p in positions if p.symbol == tick.symbol and self._is_bot_position(p)]
            if not relevant:
                return

            # Use mid price for SL calculations; broker fills against bid/ask
            # but mid is the best symmetric reference for trailing.
            mid_price = (tick.bid + tick.ask) / 2.0 if tick.bid and tick.ask else (tick.last or tick.bid or tick.ask)
            if not mid_price:
                return

            for pos in relevant:
                await self._process_position(pos, mid_price, tick.symbol)
        except Exception:
            logger.debug("Tick handler error for %s", tick.symbol, exc_info=True)

    async def _process_position(
        self, pos: Position, current_price: float, symbol: str
    ) -> None:
        """Run trailing + partial logic for one position on one tick."""
        # 1. Partial profit check (cheap — pure dict lookup)
        if self._partial is not None and self._partial.is_tracked(pos.ticket):
            actions = self._partial.evaluate_on_tick(pos.ticket, current_price, symbol)
            for action in actions:
                close_side = OrderSide.SELL if pos.side == OrderSide.BUY else OrderSide.BUY
                close_order = Order(
                    symbol=pos.symbol,
                    side=close_side,
                    order_type=OrderType.MARKET,
                    volume=action.close_volume,
                    magic=BOT_MAGIC,
                    comment=f"partial:TP{action.level_idx + 1}",
                    position_ticket=pos.ticket,
                )
                await self._event_bus.publish(
                    OrderEvent(timestamp=datetime.now(timezone.utc), order=close_order)
                )
                # Move SL to breakeven / previous TP via the same modify queue
                await self._enqueue_modify(
                    ticket=pos.ticket,
                    symbol=pos.symbol,
                    new_sl=action.new_sl,
                    take_profit=pos.take_profit,
                    reason=f"partial:TP{action.level_idx + 1}",
                    bypass_throttle=True,  # partials must always fire
                )
                self._partial_actions_sent += 1
                # Persist updated state
                state = self._partial.get_state(pos.ticket)
                if state is not None:
                    try:
                        await self._db.save_partial_profit_state(
                            ticket=pos.ticket,
                            tp_levels=state.tp_levels,
                            levels_hit=state.levels_hit,
                            original_volume=state.original_volume,
                            entry_price=state.entry_price,
                            side=state.side.value,
                        )
                    except Exception:
                        pass
                logger.info(
                    "Tick partial: ticket=%d TP%d @ %.2f close %.2f lots → SL %.2f",
                    pos.ticket, action.level_idx + 1, action.level_price,
                    action.close_volume, action.new_sl,
                )

        # 2. Trailing stop update (needs ATR — cached)
        if self._trailing is not None:
            atr = await self._get_atr_cached(pos.symbol)
            if atr is None or atr <= 0:
                return

            new_sl = self._trailing.update(
                ticket=pos.ticket,
                side=pos.side,
                current_price=current_price,
                atr=atr,
                initial_sl=pos.stop_loss,
                take_profit=pos.take_profit,
                open_price=pos.open_price,
            )
            profit_sl = self._trailing.update_profit_trail(
                ticket=pos.ticket,
                side=pos.side,
                current_price=current_price,
                open_price=pos.open_price,
            )
            # Use tighter of ATR-trail / profit-trail (matches PositionMonitor.update_trailing_stops)
            if profit_sl is not None:
                if new_sl is None:
                    new_sl = profit_sl
                elif pos.side == OrderSide.BUY:
                    new_sl = max(new_sl, profit_sl)
                else:
                    new_sl = min(new_sl, profit_sl)

            if new_sl is not None:
                # Persist (same path as PositionMonitor — idempotent)
                try:
                    await self._db.save_trailing_stop(pos.ticket, round(new_sl, 5))
                except Exception:
                    pass
                await self._enqueue_modify(
                    ticket=pos.ticket,
                    symbol=pos.symbol,
                    new_sl=round(new_sl, 5),
                    take_profit=pos.take_profit,
                    reason="trail",
                )

    async def _enqueue_modify(
        self,
        ticket: int,
        symbol: str,
        new_sl: float,
        take_profit: float | None,
        reason: str,
        bypass_throttle: bool = False,
    ) -> None:
        """Rate-limited modify dispatch.

        Drops the modify if (a) we sent one for this ticket within the last
        `modify_rate_limit_seconds`, or (b) the new SL is within
        `min_sl_change_points` of the last-sent SL. `bypass_throttle=True`
        skips both checks (used for partial-close SL moves which are
        infrequent and must always reach the broker).
        """
        now = time.monotonic()

        if not bypass_throttle:
            last_at = self._last_modify_at.get(ticket)
            if last_at is not None and (now - last_at) < self._cfg.modify_rate_limit_seconds:
                self._modifies_throttled += 1
                return

            if self._cfg.drop_unchanged_modifies:
                last_sl = self._last_sent_sl.get(ticket)
                if last_sl is not None:
                    # min_sl_change_points is in MT5 points — translate via
                    # the SL itself: a 5-point change at point=0.01 = $0.05.
                    # We don't know the symbol's point here without an MT5
                    # call, so use a conservative absolute threshold derived
                    # from the SL magnitude. 0.0001 of price level catches
                    # the typical 5-point-on-XAUUSD case (~$0.05 at $5000).
                    min_change = max(abs(last_sl) * 1e-5, 1e-5) * self._cfg.min_sl_change_points
                    if abs(new_sl - last_sl) < min_change:
                        self._modifies_throttled += 1
                        return

        self._last_modify_at[ticket] = now
        self._last_sent_sl[ticket] = new_sl
        self._modifies_sent += 1

        modify = ModifyOrder(
            ticket=ticket,
            symbol=symbol,
            stop_loss=new_sl,
            take_profit=take_profit,
        )
        await self._event_bus.publish(
            ModifyOrderEvent(
                timestamp=datetime.now(timezone.utc),
                modify_order=modify,
            )
        )
        logger.debug(
            "Tick modify (%s): ticket=%d SL=%.2f", reason, ticket, new_sl,
        )

    async def _get_atr_cached(self, symbol: str) -> float | None:
        """ATR with a 60s monotonic cache.

        ATR is on H1 by default (see TrailingStopConfig); a tick-rate
        recompute would be wasteful. The PositionMonitor's poll-loop ATR
        cache lives separately, so we keep our own here to avoid coupling.
        """
        now = time.monotonic()
        cached = self._atr_cache.get(symbol)
        if cached is not None:
            atr_val, cached_at = cached
            if (now - cached_at) < self._atr_cache_ttl:
                return atr_val
        try:
            atr = await self._atr_func(symbol)
            if atr is not None and atr > 0:
                self._atr_cache[symbol] = (float(atr), now)
                return float(atr)
        except Exception:
            logger.debug("ATR fetch failed for %s", symbol, exc_info=True)
        return None

    @staticmethod
    def _is_bot_position(pos: Position) -> bool:
        """Mirror of PositionMonitor._is_bot_position so tick logic skips
        foreign positions exactly like the poll path does."""
        magic = getattr(pos, "magic", None)
        if magic is not None and magic != 0:
            return magic == BOT_MAGIC
        return bool(pos.comment and pos.comment.startswith("tg:"))

    def cleanup(self, ticket: int) -> None:
        """Drop per-ticket throttle state when a position closes."""
        self._last_modify_at.pop(ticket, None)
        self._last_sent_sl.pop(ticket, None)

    async def on_position_closed(self, event: Event) -> None:
        """EventBus handler — cleans throttle state when positions close.

        Wire in main.py: `event_bus.subscribe("POSITION_CLOSED", tick_pm.on_position_closed)`.
        Without this, the per-ticket dicts grow unbounded over the bot's lifetime
        (~1 entry per closed trade — non-fatal, but tidy is better).
        """
        if not isinstance(event, PositionClosedEvent) or event.position is None:
            return
        self.cleanup(event.position.ticket)

    @property
    def stats(self) -> dict[str, int]:
        """Diagnostic counters for /status endpoints + tests."""
        return {
            "ticks_seen": self._ticks_seen,
            "modifies_sent": self._modifies_sent,
            "modifies_throttled": self._modifies_throttled,
            "partial_actions_sent": self._partial_actions_sent,
        }
