"""WS3 profit-lock tests: trailing-modify clamp + one-time partial profit-book.

Covers the 2026-06-01 lost-winner bug: a +$11 gold trade reverted to its entry
SL because every trailing modify was silently skipped (the order_check pre-flight
returns a non-DONE code for TRADE_ACTION_SLTP on VT Markets). Fix =
  1. clamp-and-send the SL (never silently skip), and
  2. a one-time partial profit-book that exits via a MARKET close (no
     stops_level constraint) for splittable lots — the user's "+$10 / 100-pip →
     book some" rule.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.config.schema import TrailingStopConfig
from src.core.enums import OrderSide
from src.core.events import ModifyOrderEvent
from src.core.models import ModifyOrder
from src.execution.executor import OrderExecutor
from src.monitoring.position_monitor import PositionMonitor


class FakeBus:
    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, event):
        self.events.append(event)


class FakeMT5:
    """symbol_info returns gold-like info; records order_send calls."""

    def __init__(self, info: dict | None = None, send_retcode: int = 10009) -> None:
        self._info = info or {
            "point": 0.01, "trade_stops_level": 20,
            "bid": 4000.00, "ask": 4000.10,
        }
        self._send_rc = send_retcode
        self.sent: list[dict] = []

    async def symbol_info(self, symbol):
        return dict(self._info)

    async def order_send(self, req):
        self.sent.append(req)
        return {"retcode": self._send_rc, "comment": "done", "order": 1, "deal": 1,
                "price": req.get("price", 0.0), "volume": req.get("volume", 0.0)}


# ── trailing-modify clamp (skip → clamp+send) ──────────────────────────────

async def test_modify_clamps_sell_side_stop_and_sends():
    """SELL-side stop just above ask, inside the band → clamp UP and send."""
    mt5 = FakeMT5()
    ex = OrderExecutor(event_bus=FakeBus(), mt5_client=mt5)
    mod = ModifyOrder(ticket=1, symbol="XAUUSD", stop_loss=4000.12)
    await ex._on_modify_order(
        ModifyOrderEvent(timestamp=datetime.now(timezone.utc), modify_order=mod)
    )
    assert len(mt5.sent) == 1
    assert mt5.sent[0]["sl"] == pytest.approx(4000.31)   # ask 4000.10 + 0.21


async def test_modify_far_sl_passes_through_unchanged():
    mt5 = FakeMT5()
    ex = OrderExecutor(event_bus=FakeBus(), mt5_client=mt5)
    mod = ModifyOrder(ticket=1, symbol="XAUUSD", stop_loss=3990.0)  # far below bid
    await ex._on_modify_order(
        ModifyOrderEvent(timestamp=datetime.now(timezone.utc), modify_order=mod)
    )
    assert len(mt5.sent) == 1
    assert mt5.sent[0]["sl"] == pytest.approx(3990.0)


class FillingFakeMT5:
    """order_send rejects every filling mode except `accept` with 10030.

    Reproduces VT gold: omitting/using the wrong type_filling on a SLTP modify
    returns retcode 10030 'Unsupported filling mode'.
    """

    def __init__(self, accept_mode: int, force_retcode: int | None = None) -> None:
        self._accept = accept_mode
        self._force = force_retcode
        self._info = {"point": 0.01, "trade_stops_level": 20,
                      "bid": 4000.00, "ask": 4000.10, "filling_mode": 2}
        self.sent: list[dict] = []

    async def symbol_info(self, symbol):
        return dict(self._info)

    async def order_send(self, req):
        self.sent.append(req)
        if self._force is not None:
            return {"retcode": self._force, "comment": "Invalid request"}
        if req.get("type_filling") == self._accept:
            return {"retcode": 10009, "comment": "done"}
        return {"retcode": 10030, "comment": "Unsupported filling mode"}


async def test_modify_sends_bare_sltp_first_then_retries_filling_modes():
    # 2026-06-10 VT regression: a SLTP carrying type_filling is rejected with
    # 10014 "Invalid volume" — the spec-compliant BARE request must go first.
    # Fallback preserved: if the broker answers the bare request with 10030
    # (older-broker behavior), retry with each supported filling mode.
    mt5 = FillingFakeMT5(accept_mode=1)   # bare → 10030, then modes until IOC(1)
    ex = OrderExecutor(event_bus=FakeBus(), mt5_client=mt5)
    mod = ModifyOrder(ticket=1, symbol="XAUUSD", stop_loss=3990.0)
    await ex._on_modify_order(
        ModifyOrderEvent(timestamp=datetime.now(timezone.utc), modify_order=mod)
    )
    assert "type_filling" not in mt5.sent[0]                    # bare spec request first
    assert len(mt5.sent) >= 2                                   # retried past the 10030
    assert any(s.get("type_filling") == 1 for s in mt5.sent)    # found the accepted mode
    assert all("type_filling" in s for s in mt5.sent[1:])       # fallbacks carry modes


async def test_modify_bare_sltp_accepted_means_single_send():
    # Happy path on VT: the bare SLTP is accepted — no filling-mode churn.
    mt5 = FillingFakeMT5(accept_mode=1)

    async def order_send(req):
        mt5.sent.append(req)
        if "type_filling" in req:
            return {"retcode": 10014, "comment": "Invalid volume"}  # VT live behavior
        return {"retcode": 10009, "comment": "done"}

    mt5.order_send = order_send
    ex = OrderExecutor(event_bus=FakeBus(), mt5_client=mt5)
    mod = ModifyOrder(ticket=1, symbol="XAUUSD", stop_loss=3990.0)
    await ex._on_modify_order(
        ModifyOrderEvent(timestamp=datetime.now(timezone.utc), modify_order=mod)
    )
    assert len(mt5.sent) == 1
    assert "type_filling" not in mt5.sent[0]


async def test_modify_stops_on_non_filling_error():
    # 10013 is not a filling problem → must NOT churn through all modes
    mt5 = FillingFakeMT5(accept_mode=999, force_retcode=10013)
    ex = OrderExecutor(event_bus=FakeBus(), mt5_client=mt5)
    mod = ModifyOrder(ticket=1, symbol="XAUUSD", stop_loss=3990.0)
    await ex._on_modify_order(
        ModifyOrderEvent(timestamp=datetime.now(timezone.utc), modify_order=mod)
    )
    assert len(mt5.sent) == 1                                   # bailed after the first


# ── partial profit-book ────────────────────────────────────────────────────

def _monitor(cfg: TrailingStopConfig, mt5: FakeMT5, bus: FakeBus) -> PositionMonitor:
    """Build a PositionMonitor with only the attrs _maybe_book_partial reads."""
    pm = object.__new__(PositionMonitor)
    pm._ts_config = cfg
    pm._partial_booked = set()
    pm._mt5 = mt5
    pm._event_bus = bus
    return pm


def _pos(side, open_price, current, volume, ticket=42, symbol="XAUUSD"):
    return SimpleNamespace(
        side=side, open_price=open_price, current_price=current,
        volume=volume, ticket=ticket, symbol=symbol,
    )


_ENABLED = TrailingStopConfig(
    partial_book_enabled=True,
    partial_book_trigger_points=10.0,
    partial_book_fraction=0.5,
)


async def test_partial_book_fires_once_for_splittable_lot():
    bus, mt5 = FakeBus(), FakeMT5()
    pm = _monitor(_ENABLED, mt5, bus)
    pos = _pos(OrderSide.SELL, 4000.0, 3989.0, 0.40)   # +11 profit, 0.40 lot
    await pm._maybe_book_partial(42, pos)
    assert len(bus.events) == 1
    order = bus.events[0].order
    assert order.volume == pytest.approx(0.20)          # half, rounded to step
    assert order.side == OrderSide.BUY                   # counter-direction close
    assert order.position_ticket == 42                   # closes THIS position
    assert order.comment == "partial:profit_book"
    # a later poll must NOT double-book
    await pm._maybe_book_partial(42, pos)
    assert len(bus.events) == 1


async def test_partial_book_skips_unsplittable_min_lot():
    bus, mt5 = FakeBus(), FakeMT5()
    pm = _monitor(_ENABLED, mt5, bus)
    pos = _pos(OrderSide.BUY, 4000.0, 4011.0, 0.01)    # +11 but 0.01 can't split
    await pm._maybe_book_partial(7, pos)
    assert bus.events == []


async def test_partial_book_skips_below_trigger():
    bus, mt5 = FakeBus(), FakeMT5()
    pm = _monitor(_ENABLED, mt5, bus)
    pos = _pos(OrderSide.SELL, 4000.0, 3995.0, 0.40)   # +5 < +10 trigger
    await pm._maybe_book_partial(42, pos)
    assert bus.events == []
    assert 42 not in pm._partial_booked                 # can still fire once it grows


async def test_partial_book_disabled_is_noop():
    bus, mt5 = FakeBus(), FakeMT5()
    pm = _monitor(TrailingStopConfig(partial_book_enabled=False), mt5, bus)
    pos = _pos(OrderSide.SELL, 4000.0, 3989.0, 0.40)
    await pm._maybe_book_partial(42, pos)
    assert bus.events == []
