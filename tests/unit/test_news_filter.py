"""Unit tests for NewsEventFilter — boundaries, time_until_next, fallbacks."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.analysis.news_filter import NewsEvent, NewsEventFilter

NFP = datetime(2026, 2, 6, 13, 30, tzinfo=timezone.utc)


def _filter_with_one_event(event_time: datetime = NFP) -> NewsEventFilter:
    """Build a filter that bypasses CSV/default loading and holds one event."""
    nf = NewsEventFilter.__new__(NewsEventFilter)
    nf._block_before = timedelta(minutes=15)
    nf._block_after = timedelta(minutes=30)
    nf._events = [NewsEvent("Non-Farm Payrolls (NFP)", event_time)]
    return nf


def test_block_at_left_boundary():
    """Exactly -15 min is blocked (inclusive)."""
    nf = _filter_with_one_event()
    blocked, reason = nf.is_blocked(NFP - timedelta(minutes=15))
    assert blocked is True
    assert "Non-Farm" in reason


def test_not_blocked_just_before_window():
    """At -16 min, outside the window."""
    nf = _filter_with_one_event()
    blocked, _ = nf.is_blocked(NFP - timedelta(minutes=16))
    assert blocked is False


def test_block_at_right_boundary():
    """Exactly +30 min is blocked (inclusive)."""
    nf = _filter_with_one_event()
    blocked, _ = nf.is_blocked(NFP + timedelta(minutes=30))
    assert blocked is True


def test_not_blocked_after_window():
    """At +31 min, outside the window."""
    nf = _filter_with_one_event()
    blocked, _ = nf.is_blocked(NFP + timedelta(minutes=31))
    assert blocked is False


def test_naive_timestamp_treated_as_utc():
    """A naive datetime should be treated as UTC, not crash."""
    nf = _filter_with_one_event()
    naive = (NFP - timedelta(minutes=10)).replace(tzinfo=None)
    blocked, _ = nf.is_blocked(naive)
    assert blocked is True


def test_time_until_next_event_basic():
    nf = _filter_with_one_event()
    now = NFP - timedelta(minutes=30)
    evt, delta = nf.time_until_next_event(now=now)
    assert evt is not None
    assert evt.name.startswith("Non-Farm")
    assert delta == timedelta(minutes=30)


def test_time_until_next_event_naive_now():
    nf = _filter_with_one_event()
    naive_now = (NFP - timedelta(minutes=20)).replace(tzinfo=None)
    evt, delta = nf.time_until_next_event(now=naive_now)
    assert evt is not None
    assert delta == timedelta(minutes=20)


def test_time_until_next_event_no_future():
    """When the calendar is exhausted, returns (None, None)."""
    nf = _filter_with_one_event()
    now = NFP + timedelta(days=365)
    evt, delta = nf.time_until_next_event(now=now)
    assert evt is None
    assert delta is None


def test_time_until_next_event_default_now():
    """Calling with no args uses datetime.now(); returns next future event."""
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    nf = _filter_with_one_event(event_time=future)
    evt, delta = nf.time_until_next_event()
    assert evt is not None
    # Allow a small drift between datetime.now() inside the method and the test
    assert timedelta(hours=1, minutes=59) < delta < timedelta(hours=2, minutes=1)


def test_csv_missing_does_not_crash(tmp_path):
    """A non-existent calendar_path logs a warning but does not crash."""
    nf = NewsEventFilter(calendar_path=str(tmp_path / "missing.csv"))
    # Explicit path provided → no default fallback. Filter is empty.
    assert nf.event_count == 0
    blocked, _ = nf.is_blocked(NFP)
    assert blocked is False
    evt, delta = nf.time_until_next_event()
    assert evt is None and delta is None


def test_default_calendar_loads_when_no_path():
    """When constructed with no path, defaults populate FOMC + NFP + CPI."""
    nf = NewsEventFilter(calendar_path=None)
    assert nf.event_count > 0
    # Should be sorted ascending
    times = [e.datetime_utc for e in nf._events]
    assert times == sorted(times)


def test_overlapping_events_use_first():
    """If two events both block the timestamp, the first sorted match wins."""
    nf = NewsEventFilter.__new__(NewsEventFilter)
    nf._block_before = timedelta(minutes=15)
    nf._block_after = timedelta(minutes=30)
    e1 = NewsEvent("FOMC Rate Decision", datetime(2026, 1, 28, 19, 0, tzinfo=timezone.utc))
    e2 = NewsEvent("US PPI", datetime(2026, 1, 28, 19, 15, tzinfo=timezone.utc))
    nf._events = sorted([e1, e2], key=lambda e: e.datetime_utc)

    overlap = datetime(2026, 1, 28, 19, 5, tzinfo=timezone.utc)
    blocked, reason = nf.is_blocked(overlap)
    assert blocked is True
    assert "FOMC" in reason  # earlier event wins


@pytest.mark.parametrize("offset_min", [-14, -10, 0, 15, 29])
def test_blocked_anywhere_inside_window(offset_min):
    nf = _filter_with_one_event()
    ts = NFP + timedelta(minutes=offset_min)
    blocked, _ = nf.is_blocked(ts)
    assert blocked is True
