"""Tests for TickPositionManager — modify rate-limit, ratchet, partial-on-tick.

Uses lightweight fakes for EventBus, TrackingDB, and Position to avoid
hitting MT5 / SQLite. The contract under test is internal-only:

  - tick → trailing → enqueue_modify → ModifyOrderEvent on bus
  - second tick within rate-limit → drop modify
  - second tick past rate-limit → emit modify
  - partial close bypasses throttle (always reaches broker)
  - foreign position is skipped
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from src.config.schema import TickEngineConfig
from src.core.enums import OrderSide
from src.core.events import ModifyOrderEvent, OrderEvent
from src.core.models import Position, Tick
from src.monitoring.partial_profit_manager import PartialProfitManager
from src.monitoring.tick_position_manager import BOT_MAGIC, TickPositionManager
from src.risk.trailing_stop import TrailingStopManager


class FakeBus:
    """Captures published events without dispatching."""

    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, event):  # mirrors EventBus.publish
        self.events.append(event)

    def modifies(self) -> list[ModifyOrderEvent]:
        return [e for e in self.events if isinstance(e, ModifyOrderEvent)]

    def orders(self) -> list[OrderEvent]:
        return [e for e in self.events if isinstance(e, OrderEvent)]


class FakeDB:
    """Async no-op DB stand-in."""

    async def save_trailing_stop(self, ticket, sl):
        pass

    async def save_partial_profit_state(self, **kwargs):
        pass


def _bot_pos(
    ticket: int = 1,
    side: OrderSide = OrderSide.BUY,
    open_price: float = 4000.0,
    sl: float = 3960.0,
    tp: float = 4080.0,
    symbol: str = "XAUUSD",
) -> Position:
    return Position(
        ticket=ticket, symbol=symbol, side=side, volume=0.10,
        open_price=open_price, open_time=datetime.now(timezone.utc),
        stop_loss=sl, take_profit=tp,
        current_price=open_price, profit=0.0, swap=0.0, commission=0.0,
        magic=BOT_MAGIC, comment="tg:test",
    )


def _foreign_pos(ticket: int = 99, symbol: str = "XAUUSD") -> Position:
    return Position(
        ticket=ticket, symbol=symbol, side=OrderSide.BUY, volume=0.10,
        open_price=4000.0, open_time=datetime.now(timezone.utc),
        stop_loss=3950.0, take_profit=4100.0,
        current_price=4000.0, profit=0.0, swap=0.0, commission=0.0,
        magic=999000, comment="manual",
    )


def _tick(symbol: str = "XAUUSD", bid: float = 4040.0, ask: float = 4040.5) -> Tick:
    return Tick(
        symbol=symbol, timestamp=datetime.now(timezone.utc),
        bid=bid, ask=ask, last=bid, volume=1.0,
    )


def _build_pm(
    positions: list[Position],
    *,
    atr: float = 5.0,
    enable_partial: bool = False,
    cfg: TickEngineConfig | None = None,
) -> tuple[TickPositionManager, FakeBus, PartialProfitManager | None]:
    bus = FakeBus()
    db = FakeDB()
    trail = TrailingStopManager(
        atr_multiplier=1.5, activation_pct=0.0,  # always active for tests
        giveback_pct=0.10, max_giveback=10.0, activation_profit=5.0,
    )
    pp: PartialProfitManager | None = None
    if enable_partial:
        pp = PartialProfitManager(breakeven_buffer=1.0)

    async def atr_func(_symbol: str):
        return atr

    pm = TickPositionManager(
        event_bus=bus,
        tracking_db=db,
        trailing_manager=trail,
        partial_profit_manager=pp,
        positions_func=lambda: positions,
        atr_func=atr_func,
        config=cfg or TickEngineConfig(
            enabled=True, poll_interval_ms=200,
            modify_rate_limit_seconds=2.0,
            drop_unchanged_modifies=True,
            min_sl_change_points=5.0,
        ),
    )
    return pm, bus, pp


@pytest.mark.asyncio
async def test_handle_tick_no_positions_emits_nothing():
    pm, bus, _ = _build_pm(positions=[])
    await pm.handle_tick(_tick())
    assert bus.events == []


@pytest.mark.asyncio
async def test_handle_tick_skips_foreign_position():
    pm, bus, _ = _build_pm(positions=[_foreign_pos()])
    await pm.handle_tick(_tick(bid=4050.0, ask=4050.5))
    # No modify events even though price moved favorably — foreign positions are off-limits
    assert bus.modifies() == []


@pytest.mark.asyncio
async def test_handle_tick_emits_trailing_modify_for_bot_position():
    """First tick on a profitable bot BUY → trailing computes new SL → modify on bus."""
    pos = _bot_pos(ticket=1, open_price=4000.0, sl=3960.0)
    pm, bus, _ = _build_pm(positions=[pos], atr=5.0)

    # Tick at 4040 (favorable for BUY) — trail = price - 1.5*ATR = 4040 - 7.5 = 4032.5
    await pm.handle_tick(_tick(bid=4040.0, ask=4040.5))

    modifies = bus.modifies()
    assert len(modifies) == 1
    mod = modifies[0].modify_order
    assert mod.ticket == 1
    assert mod.stop_loss > pos.stop_loss  # ratcheted up


@pytest.mark.asyncio
async def test_rate_limit_drops_modify_within_window():
    """Two ticks within the rate-limit window → only one modify reaches the bus."""
    pos = _bot_pos(ticket=1, open_price=4000.0, sl=3960.0)
    cfg = TickEngineConfig(
        enabled=True, modify_rate_limit_seconds=2.0,
        drop_unchanged_modifies=False,  # isolate rate-limit logic
        min_sl_change_points=0.0,
    )
    pm, bus, _ = _build_pm(positions=[pos], cfg=cfg)

    await pm.handle_tick(_tick(bid=4040.0, ask=4040.5))
    # Update price slightly so it would otherwise issue another modify
    pos.current_price = 4045.0
    await pm.handle_tick(_tick(bid=4045.0, ask=4045.5))

    assert len(bus.modifies()) == 1
    assert pm.stats["modifies_throttled"] == 1


@pytest.mark.asyncio
async def test_rate_limit_releases_after_window():
    """Two ticks separated by > rate-limit window → both modify."""
    pos = _bot_pos(ticket=1, open_price=4000.0, sl=3960.0)
    cfg = TickEngineConfig(
        enabled=True, modify_rate_limit_seconds=0.05,  # 50ms for fast test
        drop_unchanged_modifies=False,
        min_sl_change_points=0.0,
    )
    pm, bus, _ = _build_pm(positions=[pos], cfg=cfg)

    await pm.handle_tick(_tick(bid=4040.0, ask=4040.5))
    await asyncio.sleep(0.10)  # wait past window
    pos.current_price = 4050.0
    await pm.handle_tick(_tick(bid=4050.0, ask=4050.5))

    assert len(bus.modifies()) == 2


@pytest.mark.asyncio
async def test_drop_unchanged_modifies():
    """Same SL twice → second is dropped via min_sl_change check."""
    pos = _bot_pos(ticket=1, open_price=4000.0, sl=3960.0)
    cfg = TickEngineConfig(
        enabled=True, modify_rate_limit_seconds=0.0,  # disable rate-limit
        drop_unchanged_modifies=True,
        min_sl_change_points=10.0,  # generous threshold
    )
    pm, bus, _ = _build_pm(positions=[pos], cfg=cfg)

    await pm.handle_tick(_tick(bid=4040.0, ask=4040.5))
    # Same price → trailing manager.update returns None (ratchet not moved)
    await pm.handle_tick(_tick(bid=4040.0, ask=4040.5))

    # Only the first tick produces a modify; the second yields no new SL
    assert len(bus.modifies()) == 1


@pytest.mark.asyncio
async def test_partial_profit_bypasses_throttle():
    """Partial close must reach the broker even mid-rate-limit window.

    The TP1 cross is rare (microseconds) and missing it would leave a fully
    open position past its target. Throttle is for trailing-modify spam,
    not for once-per-position partial closes.
    """
    pos = _bot_pos(ticket=5, open_price=4000.0, sl=3960.0, tp=4080.0)
    cfg = TickEngineConfig(
        enabled=True, modify_rate_limit_seconds=10.0,  # long enough to throttle anything
        drop_unchanged_modifies=False,
        min_sl_change_points=0.0,
    )
    pm, bus, pp = _build_pm(positions=[pos], cfg=cfg, enable_partial=True)
    pp.register(
        ticket=5, side=OrderSide.BUY, volume=0.30, entry_price=4000.0,
        tp_levels=[4010.0, 4020.0, 4030.0],
    )

    # First tick triggers a trailing modify (consumes the rate-limit slot)
    await pm.handle_tick(_tick(bid=4011.0, ask=4011.5))

    # Second tick within throttle window crosses TP1 → must still emit close + modify
    pos.current_price = 4015.0
    await pm.handle_tick(_tick(bid=4015.0, ask=4015.5))

    orders = bus.orders()
    assert len(orders) == 1, "expected one partial-close OrderEvent"
    assert orders[0].order.comment.startswith("partial:TP1")
    # And a modify carrying the breakeven SL must also fire (bypassed throttle)
    breakeven_modifies = [
        m for m in bus.modifies()
        if abs(m.modify_order.stop_loss - 4001.0) < 0.01  # entry + buffer
    ]
    assert len(breakeven_modifies) == 1


@pytest.mark.asyncio
async def test_cleanup_clears_throttle_state():
    pos = _bot_pos(ticket=42)
    pm, bus, _ = _build_pm(positions=[pos])
    await pm.handle_tick(_tick(bid=4040.0, ask=4040.5))
    assert 42 in pm._last_modify_at

    pm.cleanup(42)
    assert 42 not in pm._last_modify_at
    assert 42 not in pm._last_sent_sl


@pytest.mark.asyncio
async def test_atr_cache_avoids_repeat_calls():
    """ATR is cached for 60s; multiple ticks shouldn't refetch."""
    call_count = {"n": 0}

    async def counting_atr(_symbol: str):
        call_count["n"] += 1
        return 5.0

    pos = _bot_pos(ticket=1)
    bus = FakeBus()
    db = FakeDB()
    trail = TrailingStopManager(
        atr_multiplier=1.5, activation_pct=0.0,
        giveback_pct=0.10, max_giveback=10.0, activation_profit=5.0,
    )
    pm = TickPositionManager(
        event_bus=bus, tracking_db=db, trailing_manager=trail,
        partial_profit_manager=None,
        positions_func=lambda: [pos],
        atr_func=counting_atr,
        config=TickEngineConfig(
            enabled=True, modify_rate_limit_seconds=0.0,
            drop_unchanged_modifies=False, min_sl_change_points=0.0,
        ),
    )
    for _ in range(5):
        await pm.handle_tick(_tick(bid=4040.0, ask=4040.5))

    assert call_count["n"] == 1
