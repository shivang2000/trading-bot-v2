"""Tests for MirrorJournal — SQLite audit + state persistence."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.copy_trader.mirror_journal import MirrorJournal


def _make(tmp_path: Path) -> MirrorJournal:
    return MirrorJournal(db_path=str(tmp_path / "mirror.db"))


class TestEvents:
    def test_log_and_recall(self, tmp_path: Path) -> None:
        j = _make(tmp_path)
        j.log_event("OPEN", success=True, src_ticket=1, dest_ticket=2, symbol="XAUUSD",
                   side="BUY", volume=0.10, price=4000.0, latency_ms=42)
        events = j.recent_events(10)
        assert len(events) == 1
        e = events[0]
        assert e["event_type"] == "OPEN"
        assert e["success"] == 1
        assert e["src_ticket"] == 1
        assert e["dest_ticket"] == 2
        assert e["latency_ms"] == 42

    def test_failure_event(self, tmp_path: Path) -> None:
        j = _make(tmp_path)
        j.log_event("OPEN", success=False, src_ticket=99,
                   error_message="margin too low")
        e = j.recent_events(1)[0]
        assert e["success"] == 0
        assert e["error_message"] == "margin too low"

    def test_recent_orders_newest_first(self, tmp_path: Path) -> None:
        j = _make(tmp_path)
        j.log_event("OPEN", success=True, src_ticket=1)
        j.log_event("CLOSE", success=True, src_ticket=1)
        events = j.recent_events(5)
        assert events[0]["event_type"] == "CLOSE"
        assert events[1]["event_type"] == "OPEN"


class TestMirrorMap:
    def test_upsert_and_load(self, tmp_path: Path) -> None:
        j = _make(tmp_path)
        j.upsert_mapping(src_ticket=11, dest_ticket=22, symbol="XAUUSD",
                        side="BUY", volume=0.10, src_open_price=4000.0,
                        dest_open_price=4000.05, src_sl=3990.0, src_tp=4020.0)
        m = j.load_all_mappings()
        assert 11 in m
        assert m[11]["dest_ticket"] == 22
        assert m[11]["src_open_price"] == pytest.approx(4000.0)

    def test_update_sl_tp(self, tmp_path: Path) -> None:
        j = _make(tmp_path)
        j.upsert_mapping(src_ticket=11, dest_ticket=22, symbol="XAUUSD",
                        side="BUY", volume=0.10, src_open_price=4000.0,
                        dest_open_price=4000.0, src_sl=3990.0, src_tp=4020.0)
        j.update_mapping_sl_tp(11, sl=3995.0, tp=4025.0)
        m = j.load_all_mappings()
        assert m[11]["src_sl"] == pytest.approx(3995.0)
        assert m[11]["src_tp"] == pytest.approx(4025.0)

    def test_remove_mapping(self, tmp_path: Path) -> None:
        j = _make(tmp_path)
        j.upsert_mapping(src_ticket=11, dest_ticket=22, symbol="XAUUSD",
                        side="BUY", volume=0.10, src_open_price=4000.0,
                        dest_open_price=4000.0, src_sl=None, src_tp=None)
        assert 11 in j.load_all_mappings()
        j.remove_mapping(11)
        assert j.load_all_mappings() == {}


class TestIgnoredSet:
    def test_add_check_load(self, tmp_path: Path) -> None:
        j = _make(tmp_path)
        j.add_ignored(101)
        j.add_ignored(102, reason="below_lot_min")
        assert j.is_ignored(101)
        assert j.is_ignored(102)
        assert not j.is_ignored(999)
        assert j.load_ignored() == {101, 102}

    def test_idempotent_add(self, tmp_path: Path) -> None:
        j = _make(tmp_path)
        j.add_ignored(101)
        j.add_ignored(101)  # second add should be silent (INSERT OR IGNORE)
        assert j.is_ignored(101)
        assert len(j.load_ignored()) == 1


class TestRestartReplay:
    def test_journal_persists_across_open(self, tmp_path: Path) -> None:
        db = tmp_path / "j.db"
        j1 = MirrorJournal(db_path=str(db))
        j1.upsert_mapping(src_ticket=1, dest_ticket=2, symbol="XAUUSD",
                         side="BUY", volume=0.10, src_open_price=4000.0,
                         dest_open_price=4000.0, src_sl=None, src_tp=None)
        j1.add_ignored(99, reason="boot_snapshot")
        j1.close()

        j2 = MirrorJournal(db_path=str(db))
        assert 1 in j2.load_all_mappings()
        assert j2.is_ignored(99)
