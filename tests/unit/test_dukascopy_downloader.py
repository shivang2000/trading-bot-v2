"""Tests for scripts/download_dukascopy_xauusd_ticks.py.

Tests focus on the pure parsing/URL/IO logic; HTTP fetching is covered
indirectly via the structure of fetch_hour (network is mocked or skipped).
"""

from __future__ import annotations

import gzip
import lzma
import struct
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

# Make sibling `scripts/` import-friendly
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from download_dukascopy_xauusd_ticks import (  # type: ignore[import-not-found]
    TICK_STRUCT,
    XAUUSD_PRICE_SCALE,
    hour_url,
    out_path_for,
    parse_bi5,
    write_day_csv_gz,
)


def _make_bi5(ticks: list[tuple[int, float, float, float, float]]) -> bytes:
    """Build a synthetic LZMA-compressed bi5 payload for tests.

    Each input tick is (ms_in_hour, bid, ask, ask_vol, bid_vol). Returns
    LZMA bytes that round-trip through parse_bi5.
    """
    buf = bytearray()
    for ms, bid, ask, av, bv in ticks:
        buf += TICK_STRUCT.pack(
            ms,
            int(round(ask * XAUUSD_PRICE_SCALE)),
            int(round(bid * XAUUSD_PRICE_SCALE)),
            av,
            bv,
        )
    return lzma.compress(bytes(buf), format=lzma.FORMAT_ALONE)


class TestUrlBuild:
    def test_month_is_zero_indexed(self) -> None:
        # Dukascopy uses 0-indexed months: Jan=00, Feb=01, ..., Dec=11
        u = hour_url("XAUUSD", datetime(2026, 1, 15, 13, 0, 0, tzinfo=timezone.utc))
        assert "/2026/00/15/13h_ticks.bi5" in u

    def test_december_month(self) -> None:
        u = hour_url("XAUUSD", datetime(2025, 12, 31, 23, 0, 0, tzinfo=timezone.utc))
        assert "/2025/11/31/23h_ticks.bi5" in u

    def test_symbol_uppercased(self) -> None:
        u = hour_url("xauusd", datetime(2026, 5, 1, 0))
        assert "XAUUSD" in u
        assert "xauusd" not in u.split("XAUUSD")[1]


class TestBi5Parse:
    def test_empty_payload_returns_empty_list(self) -> None:
        # An idle hour serves an empty body — must not raise.
        assert parse_bi5(b"", hour_start_ms=0) == []

    def test_truncated_payload_returns_empty_list(self) -> None:
        # Garbage that fails LZMA decode logs a warning and returns [].
        assert parse_bi5(b"\x00\x01\x02notvalid", hour_start_ms=0) == []

    def test_round_trip_single_tick(self) -> None:
        # 13:00 UTC start, tick at +500ms with bid 3998.45 / ask 3998.55
        hour_start_ms = int(
            datetime(2026, 5, 1, 13, 0, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        payload = _make_bi5([(500, 3998.45, 3998.55, 1.5, 0.75)])
        ticks = parse_bi5(payload, hour_start_ms=hour_start_ms)
        assert len(ticks) == 1
        ts_ms, bid, ask, ask_vol, bid_vol = ticks[0]
        assert ts_ms == hour_start_ms + 500
        # parse_bi5 yields (ts_ms, bid, ask, ask_vol, bid_vol)
        assert bid == pytest.approx(3998.45, abs=1e-3)
        assert ask == pytest.approx(3998.55, abs=1e-3)
        assert ask_vol == pytest.approx(1.5, abs=1e-3)
        assert bid_vol == pytest.approx(0.75, abs=1e-3)

    def test_round_trip_multiple_ticks_preserves_order(self) -> None:
        hour_start_ms = 0
        ticks_in = [
            (100, 3990.10, 3990.20, 1.0, 1.0),
            (200, 3990.15, 3990.25, 2.0, 0.5),
            (300, 3990.05, 3990.15, 0.5, 2.0),
        ]
        payload = _make_bi5(ticks_in)
        out = parse_bi5(payload, hour_start_ms=hour_start_ms)
        assert len(out) == 3
        # parse_bi5 returns in stream order (which matches insertion order)
        assert [t[0] for t in out] == [100, 200, 300]


class TestCsvWriter:
    def test_writes_header_and_rows(self, tmp_path: Path) -> None:
        target = tmp_path / "2026" / "05" / "2026-05-01.csv.gz"
        ticks = [
            # ts_ms, bid, ask, ask_vol, bid_vol
            (
                int(datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc).timestamp() * 1000),
                3998.45, 3998.55, 1.5, 0.75,
            ),
            (
                int(datetime(2026, 5, 1, 12, 0, 1, tzinfo=timezone.utc).timestamp() * 1000),
                3998.50, 3998.60, 2.0, 1.0,
            ),
        ]
        rows = write_day_csv_gz(ticks, target)
        assert rows == 2
        assert target.exists()
        # Roundtrip read
        with gzip.open(target, "rt") as fh:
            lines = fh.read().strip().splitlines()
        assert lines[0] == "timestamp,bid,ask,last,volume"
        assert lines[1].startswith("2026-05-01T12:00:00Z,3998.45000,3998.55000,")
        # 'last' is mid = (bid + ask) / 2
        assert ",3998.50000," in lines[1]
        # 'volume' is ask_vol + bid_vol
        assert lines[1].endswith(",2.2500")

    def test_empty_input_creates_header_only_file(self, tmp_path: Path) -> None:
        target = tmp_path / "2026-05-02.csv.gz"
        rows = write_day_csv_gz([], target)
        assert rows == 0
        assert target.exists()
        with gzip.open(target, "rt") as fh:
            content = fh.read()
        assert content.strip() == "timestamp,bid,ask,last,volume"

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "nested" / "year" / "month" / "file.csv.gz"
        write_day_csv_gz([], target)
        assert target.parent.is_dir()


class TestOutPath:
    def test_path_layout(self, tmp_path: Path) -> None:
        p = out_path_for(tmp_path, "XAUUSD", date(2026, 1, 15))
        # symbol is appended by run() not by out_path_for; out_path_for assumes
        # caller passed the symbol-scoped root.
        assert p == tmp_path / "2026" / "01" / "2026-01-15.csv.gz"

    def test_padding(self, tmp_path: Path) -> None:
        p = out_path_for(tmp_path, "XAUUSD", date(2026, 3, 5))
        assert p.parts[-2] == "03"
        assert p.parts[-3] == "2026"
        assert p.name == "2026-03-05.csv.gz"
