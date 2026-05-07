"""Tests for CopyTrader mirror logic with mocked MT5 clients."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from src.config.copy_trader_schema import CopyTraderConfig
from src.copy_trader.copy_trader import CopyTrader
from src.copy_trader.mirror_journal import MirrorJournal
from src.copy_trader.notifier import SlackNotifier
from src.core.enums import OrderSide
from src.core.models import Position


# ---------- Fakes ----------


class FakeMT5:
    """In-memory MT5 client. Records all order_send + position_modify calls."""

    def __init__(self) -> None:
        self.positions: list[Position] = []
        self.sent_orders: list[dict[str, Any]] = []
        self.modify_calls: list[dict[str, Any]] = []
        # Toggle for forcing failures
        self.next_send_retcode: int = 10009  # default success
        self.next_dest_ticket: int = 9001
        self.next_modify_retcode: int = 10009

    async def positions_get(self, symbol: str | None = None) -> list[Position]:
        return list(self.positions)

    async def order_send(self, request: dict[str, Any]) -> dict[str, Any]:
        self.sent_orders.append(request)
        retcode = self.next_send_retcode
        order_ticket = self.next_dest_ticket
        self.next_dest_ticket += 1
        return {
            "retcode": retcode,
            "order": order_ticket,
            "deal": order_ticket + 1,
            "price": 4000.0,
            "volume": request.get("volume"),
            "comment": "ok" if retcode == 10009 else "rejected",
        }

    async def position_modify(self, ticket: int, stop_loss=None, take_profit=None, symbol=None):
        self.modify_calls.append({"ticket": ticket, "sl": stop_loss, "tp": take_profit, "symbol": symbol})
        return {"retcode": self.next_modify_retcode, "comment": "modified"}


class SilentNotifier(SlackNotifier):
    """Notifier that records calls without hitting the network."""

    def __init__(self) -> None:
        super().__init__(webhook_url="", channel_label="test", enabled=False)
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def post(self, text, fields=None):
        self.calls.append((text, fields))


def _pos(ticket: int, symbol: str = "XAUUSD", side: OrderSide = OrderSide.BUY,
         volume: float = 0.10, open_price: float = 4000.0,
         stop_loss=None, take_profit=None) -> Position:
    """Position factory matching src.core.models.Position required fields.

    Position is a frozen dataclass; we have to supply every field.
    """
    return Position(
        ticket=ticket,
        symbol=symbol,
        side=side,
        volume=volume,
        open_price=open_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        open_time=datetime.now(timezone.utc),
        magic=0,
        comment="",
        profit=0.0,
    )


def _make(tmp_path: Path, cfg_overrides: dict | None = None):
    src = FakeMT5()
    dst = FakeMT5()
    journal = MirrorJournal(db_path=str(tmp_path / "j.db"))
    notifier = SilentNotifier()
    cfg = CopyTraderConfig(**(cfg_overrides or {}))
    ct = CopyTrader(src, dst, journal, notifier, cfg)
    return ct, src, dst, journal, notifier


# ---------- Boot ----------


class TestBoot:
    @pytest.mark.asyncio
    async def test_boot_ignores_existing_positions(self, tmp_path: Path) -> None:
        ct, src, dst, journal, _ = _make(tmp_path)
        src.positions = [_pos(101), _pos(102)]
        await ct.boot()
        assert journal.is_ignored(101)
        assert journal.is_ignored(102)
        assert ct.stats["mirror_open_count"] == 0

    @pytest.mark.asyncio
    async def test_boot_skip_when_disabled(self, tmp_path: Path) -> None:
        ct, src, _, journal, _ = _make(tmp_path, {"ignore_existing_at_boot": False})
        src.positions = [_pos(101)]
        await ct.boot()
        assert not journal.is_ignored(101)


# ---------- Open mirroring ----------


class TestOpen:
    @pytest.mark.asyncio
    async def test_new_position_mirrors(self, tmp_path: Path) -> None:
        ct, src, dst, journal, notifier = _make(tmp_path)
        await ct.boot()
        src.positions = [_pos(201, side=OrderSide.BUY, volume=0.10, stop_loss=3990.0, take_profit=4020.0)]
        await ct._cycle()
        # One order_send happened on dest
        assert len(dst.sent_orders) == 1
        order = dst.sent_orders[0]
        assert order["symbol"] == "XAUUSD"
        assert order["volume"] == pytest.approx(0.10)
        assert order["sl"] == pytest.approx(3990.0)
        assert order["tp"] == pytest.approx(4020.0)
        # Mirror map updated
        assert 201 in {row["src_ticket"] for row in journal.load_all_mappings().values()}
        # Slack got the open notification
        assert any("MIRROR OPEN" in text for text, _ in notifier.calls)

    @pytest.mark.asyncio
    async def test_existing_position_not_re_mirrored(self, tmp_path: Path) -> None:
        ct, src, dst, _, _ = _make(tmp_path)
        await ct.boot()
        src.positions = [_pos(201)]
        await ct._cycle()
        await ct._cycle()  # second cycle, same position
        assert len(dst.sent_orders) == 1  # only one mirror

    @pytest.mark.asyncio
    async def test_open_failure_logs_error(self, tmp_path: Path) -> None:
        ct, src, dst, journal, notifier = _make(tmp_path, {"order_send_max_retries": 0})
        await ct.boot()
        dst.next_send_retcode = 10006  # simulated rejection
        src.positions = [_pos(301)]
        await ct._cycle()
        events = journal.recent_events(5)
        assert any(e["event_type"] == "OPEN" and e["success"] == 0 for e in events)
        assert any("failed" in t.lower() for t, _ in notifier.calls)

    @pytest.mark.asyncio
    async def test_volume_below_floor_skipped(self, tmp_path: Path) -> None:
        ct, src, dst, journal, _ = _make(tmp_path, {"lot_min": 0.05})
        await ct.boot()
        src.positions = [_pos(401, volume=0.01)]
        await ct._cycle()
        assert dst.sent_orders == []
        assert journal.is_ignored(401)


# ---------- Close mirroring ----------


class TestClose:
    @pytest.mark.asyncio
    async def test_source_close_triggers_dest_close(self, tmp_path: Path) -> None:
        ct, src, dst, journal, notifier = _make(tmp_path)
        await ct.boot()
        src.positions = [_pos(501)]
        await ct._cycle()  # opens mirror
        assert len(dst.sent_orders) == 1
        # Source closes
        src.positions = []
        await ct._cycle()
        # Dest got a close order (second order_send)
        assert len(dst.sent_orders) == 2
        close_req = dst.sent_orders[1]
        assert "position" in close_req  # close uses position field
        # Mapping removed
        assert 501 not in journal.load_all_mappings()
        # Slack got close
        assert any("MIRROR CLOSE" in t for t, _ in notifier.calls)


# ---------- Modify (SL/TP sync) ----------


class TestModify:
    @pytest.mark.asyncio
    async def test_sl_change_triggers_dest_modify(self, tmp_path: Path) -> None:
        ct, src, dst, journal, notifier = _make(tmp_path)
        await ct.boot()
        # Initial mirror
        src.positions = [_pos(601, stop_loss=3990.0, take_profit=4020.0)]
        await ct._cycle()
        assert dst.modify_calls == []  # only open so far
        # SL changed
        src.positions = [_pos(601, stop_loss=3995.0, take_profit=4020.0)]
        await ct._cycle()
        assert len(dst.modify_calls) == 1
        assert dst.modify_calls[0]["sl"] == pytest.approx(3995.0)
        # Mapping updated
        m = journal.load_all_mappings()[601]
        assert m["src_sl"] == pytest.approx(3995.0)

    @pytest.mark.asyncio
    async def test_no_modify_when_below_threshold(self, tmp_path: Path) -> None:
        ct, src, dst, _, _ = _make(tmp_path, {"sl_change_min_points": 10.0, "tp_change_min_points": 10.0})
        await ct.boot()
        src.positions = [_pos(701, stop_loss=3990.00000)]
        await ct._cycle()
        # Tiny noise (under threshold)
        src.positions = [_pos(701, stop_loss=3990.00001)]
        await ct._cycle()
        assert dst.modify_calls == []


# ---------- Restart reconciliation ----------


class TestRestart:
    @pytest.mark.asyncio
    async def test_restored_mappings_not_re_mirrored(self, tmp_path: Path) -> None:
        # First instance: open mirror
        ct1, src1, dst1, journal, _ = _make(tmp_path)
        await ct1.boot()
        src1.positions = [_pos(801)]
        await ct1._cycle()
        assert 801 in {row["src_ticket"] for row in journal.load_all_mappings().values()}
        journal.close()

        # Second instance reuses same db
        ct2, src2, dst2, journal2, _ = _make(tmp_path)
        await ct2.boot()
        # Source still shows position 801
        src2.positions = [_pos(801)]
        await ct2._cycle()
        # Dest should NOT receive a duplicate open
        assert dst2.sent_orders == []
