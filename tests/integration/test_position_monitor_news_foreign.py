"""Integration tests for PositionMonitor's news + foreign-position checks."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.analysis.news_filter import NewsEvent, NewsEventFilter
from src.core.enums import OrderSide
from src.core.events import EventBus, ForeignPositionEvent, OrderEvent
from src.core.models import Position
from src.monitoring.position_monitor import BOT_MAGIC, PositionMonitor

NFP = datetime(2026, 2, 6, 13, 30, tzinfo=timezone.utc)


def _bot_position(ticket: int = 1, magic: int = BOT_MAGIC) -> Position:
    return Position(
        ticket=ticket,
        symbol="XAUUSD",
        side=OrderSide.BUY,
        volume=0.10,
        open_price=2650.0,
        open_time=datetime.now(timezone.utc),
        magic=magic,
        comment="tg:test BUY",
    )


def _foreign_position(ticket: int = 999, magic: int = 12345) -> Position:
    return Position(
        ticket=ticket,
        symbol="XAUUSD",
        side=OrderSide.SELL,
        volume=0.50,
        open_price=2655.0,
        open_time=datetime.now(timezone.utc),
        magic=magic,
        comment="manual",
    )


def _filter_with_event(event_time: datetime) -> NewsEventFilter:
    nf = NewsEventFilter.__new__(NewsEventFilter)
    nf._block_before = timedelta(minutes=15)
    nf._block_after = timedelta(minutes=30)
    nf._events = [NewsEvent("Non-Farm Payrolls (NFP)", event_time)]
    return nf


def _build_monitor(
    positions: list,
    news_filter: NewsEventFilter | None = None,
) -> tuple[PositionMonitor, EventBus, list]:
    """Build a PositionMonitor with an in-memory bus that captures published events."""
    bus = EventBus()
    captured: list = []

    async def capture(event):
        captured.append(event)

    bus.subscribe("ORDER", capture)
    bus.subscribe("FOREIGN_POSITION", capture)

    mt5 = MagicMock()
    mt5.positions_get = AsyncMock(return_value=positions)

    pm = PositionMonitor(
        mt5_client=mt5,
        event_bus=bus,
        tracking_db=MagicMock(),
        poll_interval=30,
        news_filter=news_filter,
        pre_news_flat_minutes=5,
    )
    return pm, bus, captured


@pytest.mark.asyncio
async def test_foreign_position_emits_event_once():
    """A non-bot-magic position triggers ForeignPositionEvent; second poll silent."""
    foreign = _foreign_position()
    pm, bus, captured = _build_monitor([foreign])

    await pm._check_foreign_positions()
    await bus.drain()
    await pm._check_foreign_positions()
    await bus.drain()

    foreign_events = [e for e in captured if isinstance(e, ForeignPositionEvent)]
    assert len(foreign_events) == 1
    assert foreign_events[0].position.ticket == foreign.ticket


@pytest.mark.asyncio
async def test_bot_position_does_not_emit_foreign_event():
    pm, bus, captured = _build_monitor([_bot_position()])
    await pm._check_foreign_positions()
    await bus.drain()
    assert not any(isinstance(e, ForeignPositionEvent) for e in captured)


@pytest.mark.asyncio
async def test_foreign_re_alerts_after_close_and_reopen():
    """If user closes the foreign trade and a new one appears later, re-alert."""
    foreign = _foreign_position(ticket=999)
    pm, bus, captured = _build_monitor([foreign])

    await pm._check_foreign_positions()
    await bus.drain()
    # User closes it → no positions
    pm._mt5.positions_get.return_value = []
    await pm._check_foreign_positions()
    await bus.drain()
    # New foreign position appears
    new_foreign = _foreign_position(ticket=1000)
    pm._mt5.positions_get.return_value = [new_foreign]
    await pm._check_foreign_positions()
    await bus.drain()

    foreign_events = [e for e in captured if isinstance(e, ForeignPositionEvent)]
    assert len(foreign_events) == 2


@pytest.mark.asyncio
async def test_pre_news_flat_closes_bot_position():
    """A bot position with NFP 3 min away should generate a counter-direction close."""
    in_3_min = datetime.now(timezone.utc) + timedelta(minutes=3)
    nf = _filter_with_event(in_3_min)
    pm, bus, captured = _build_monitor([_bot_position()], news_filter=nf)

    await pm._check_pre_news_flat()
    await bus.drain()

    orders = [e for e in captured if isinstance(e, OrderEvent)]
    assert len(orders) == 1
    assert orders[0].order.side == OrderSide.SELL  # closing a BUY
    assert "pre_news" in orders[0].order.comment


@pytest.mark.asyncio
async def test_pre_news_flat_skips_foreign_positions():
    """The bot must NOT close human-placed positions (race risk)."""
    in_3_min = datetime.now(timezone.utc) + timedelta(minutes=3)
    nf = _filter_with_event(in_3_min)
    pm, bus, captured = _build_monitor([_foreign_position()], news_filter=nf)

    await pm._check_pre_news_flat()
    await bus.drain()

    orders = [e for e in captured if isinstance(e, OrderEvent)]
    assert orders == []


@pytest.mark.asyncio
async def test_pre_news_flat_outside_window_no_op():
    """Event 1 hour out is far beyond the 5-min window — no close."""
    in_1_hour = datetime.now(timezone.utc) + timedelta(hours=1)
    nf = _filter_with_event(in_1_hour)
    pm, bus, captured = _build_monitor([_bot_position()], news_filter=nf)

    await pm._check_pre_news_flat()
    await bus.drain()

    assert not any(isinstance(e, OrderEvent) for e in captured)


@pytest.mark.asyncio
async def test_pre_news_flat_handles_event_only_once():
    """Two heartbeat ticks within the window → only one batch of close orders."""
    in_3_min = datetime.now(timezone.utc) + timedelta(minutes=3)
    nf = _filter_with_event(in_3_min)
    pm, bus, captured = _build_monitor([_bot_position()], news_filter=nf)

    await pm._check_pre_news_flat()
    await pm._check_pre_news_flat()
    await bus.drain()

    orders = [e for e in captured if isinstance(e, OrderEvent)]
    assert len(orders) == 1


@pytest.mark.asyncio
async def test_is_bot_position_uses_magic():
    """Magic-based detection takes priority over comment."""
    # Old-format position (legacy comment, magic=0) → fallback to comment
    legacy = Position(
        ticket=1, symbol="XAUUSD", side=OrderSide.BUY, volume=0.1,
        open_price=2600.0, open_time=datetime.now(timezone.utc),
        magic=0, comment="tg:legacy",
    )
    assert PositionMonitor._is_bot_position(legacy) is True

    # Foreign EA with non-bot magic and innocuous comment → foreign
    foreign = Position(
        ticket=2, symbol="XAUUSD", side=OrderSide.SELL, volume=0.1,
        open_price=2600.0, open_time=datetime.now(timezone.utc),
        magic=99999, comment="tg:looks_like_bot",
    )
    assert PositionMonitor._is_bot_position(foreign) is False

    # Bot magic, any comment → bot
    botpos = _bot_position(magic=BOT_MAGIC)
    assert PositionMonitor._is_bot_position(botpos) is True
