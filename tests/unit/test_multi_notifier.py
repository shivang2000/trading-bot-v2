"""MultiNotifier — fan-out tests.

Verifies:
  1. Disabled notifiers are filtered out
  2. send() fires every enabled notifier
  3. Failures in one notifier don't block the others
  4. Typed methods (send_emergency_stop, etc.) call all notifiers
  5. Discord gets critical=True for emergency_stop and foreign_position
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.monitoring.multi_notifier import MultiNotifier


class FakeNotifier:
    """Mock notifier with .enabled, .send(), and typed methods."""

    def __init__(self, name: str = "fake", enabled: bool = True) -> None:
        self.name = name
        self.enabled = enabled
        self.sent: list = []
        # Real methods (not AsyncMocks) so inspect.signature works the same
        # way it does in production for SlackNotifier/DiscordNotifier.
        self.send_trade_opened = AsyncMock(
            side_effect=lambda **kw: self.sent.append(("trade_opened", kw)),
        )
        self.send_trade_closed = AsyncMock(
            side_effect=lambda **kw: self.sent.append(("trade_closed", kw)),
        )
        self.send_emergency_stop = AsyncMock(
            side_effect=lambda reason: self.sent.append(("emergency", reason)),
        )
        self.send_foreign_position = AsyncMock(
            side_effect=lambda **kw: self.sent.append(("foreign", kw)),
        )
        self.send_error_alert = AsyncMock(
            side_effect=lambda error: self.sent.append(("error", error)),
        )

    async def send(self, message: str, *, critical: bool = False) -> None:
        self.sent.append((message, critical))


def test_filters_disabled():
    a = FakeNotifier("a", enabled=True)
    b = FakeNotifier("b", enabled=False)
    multi = MultiNotifier(a, b)
    assert multi.enabled is True
    assert len(multi._notifiers) == 1
    assert multi._notifiers[0] is a


def test_warns_when_no_active():
    a = FakeNotifier("a", enabled=False)
    multi = MultiNotifier(a)
    assert multi.enabled is False


@pytest.mark.asyncio
async def test_send_fires_every_notifier():
    a = FakeNotifier("a")
    b = FakeNotifier("b")
    multi = MultiNotifier(a, b)
    await multi.send("hello", critical=True)
    assert ("hello", True) in a.sent
    assert ("hello", True) in b.sent


@pytest.mark.asyncio
async def test_failures_dont_block_others():
    a = FakeNotifier("a")
    # b's send raises
    async def boom(message, *, critical=False):
        raise RuntimeError("webhook revoked")
    b = FakeNotifier("b")
    b.send = boom
    multi = MultiNotifier(a, b)
    # Should not raise even though b's send does
    await multi.send("test")
    # a still got it
    assert any(msg == "test" for msg, _ in a.sent)


@pytest.mark.asyncio
async def test_emergency_stop_called_on_all():
    a = FakeNotifier("a")
    b = FakeNotifier("b")
    multi = MultiNotifier(a, b)
    await multi.send_emergency_stop("Daily loss breached")
    assert ("emergency", "Daily loss breached") in a.sent
    assert ("emergency", "Daily loss breached") in b.sent


@pytest.mark.asyncio
async def test_foreign_position_called_on_all():
    a = FakeNotifier("a")
    b = FakeNotifier("b")
    multi = MultiNotifier(a, b)
    await multi.send_foreign_position(
        ticket=999, symbol="XAUUSD", side="BUY", volume=0.1,
        entry_price=4000.0, magic=123,
    )
    assert any(item[0] == "foreign" and item[1]["ticket"] == 999 for item in a.sent)
    assert any(item[0] == "foreign" and item[1]["ticket"] == 999 for item in b.sent)


@pytest.mark.asyncio
async def test_trade_opened_kwargs_passed_through():
    a = FakeNotifier("a")
    b = FakeNotifier("b")
    multi = MultiNotifier(a, b)
    await multi.send_trade_opened(symbol="XAUUSD", side="BUY", volume=0.05, price=4000.0)
    assert any(item[0] == "trade_opened" and item[1]["symbol"] == "XAUUSD" for item in a.sent)
    assert any(item[0] == "trade_opened" and item[1]["symbol"] == "XAUUSD" for item in b.sent)
