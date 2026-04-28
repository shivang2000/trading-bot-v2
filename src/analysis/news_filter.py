"""News Event Filter — blocks trading around high-impact economic events.

Gold (XAUUSD) moves $30-50 in seconds during FOMC, NFP, CPI releases.
Spreads spike to 50+ points. This filter blocks all trading N minutes
before and after these events to protect the account.
"""

from __future__ import annotations

import calendar
import csv
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class NewsEvent:
    """A high-impact economic event."""
    name: str
    datetime_utc: datetime
    impact: str = "HIGH"  # HIGH, MEDIUM, LOW
    currency: str = "USD"


class NewsEventFilter:
    """Blocks trading around high-impact economic events."""

    def __init__(
        self,
        block_before_minutes: int = 15,
        block_after_minutes: int = 30,
        calendar_path: str | None = None,
    ) -> None:
        self._block_before = timedelta(minutes=block_before_minutes)
        self._block_after = timedelta(minutes=block_after_minutes)
        self._events: list[NewsEvent] = []

        if calendar_path:
            self.load_calendar(calendar_path)
        else:
            self._load_default_calendar()

    def is_blocked(self, timestamp: datetime) -> tuple[bool, str]:
        """Check if trading is blocked at this timestamp.

        Returns (is_blocked, reason_string).
        """
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)

        for event in self._events:
            event_time = event.datetime_utc
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)

            block_start = event_time - self._block_before
            block_end = event_time + self._block_after

            if block_start <= timestamp <= block_end:
                return True, f"News: {event.name} at {event_time.strftime('%Y-%m-%d %H:%M')} UTC"

        return False, ""

    def time_until_next_event(
        self, now: datetime | None = None
    ) -> tuple[NewsEvent | None, timedelta | None]:
        """Return (next_event, delta_until_it) or (None, None) if calendar is exhausted.

        Used by PositionMonitor to drive pre-news FLAT (close open positions
        before a high-impact event lands).
        """
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        # Events are kept sorted by datetime_utc in load_calendar / _load_default_calendar
        for event in self._events:
            event_time = event.datetime_utc
            if event_time.tzinfo is None:
                event_time = event_time.replace(tzinfo=timezone.utc)
            if event_time > now:
                return event, event_time - now
        return None, None

    def load_calendar(self, csv_path: str) -> None:
        """Load event calendar from CSV file.

        CSV format: date,time_utc,name,impact,currency
        Example: 2026-01-29,19:00,FOMC Rate Decision,HIGH,USD
        """
        path = Path(csv_path)
        if not path.exists():
            logger.warning("News calendar not found: %s", csv_path)
            return

        self._events = []
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    dt = datetime.strptime(
                        f"{row['date']} {row['time_utc']}", "%Y-%m-%d %H:%M"
                    ).replace(tzinfo=timezone.utc)
                    self._events.append(NewsEvent(
                        name=row["name"],
                        datetime_utc=dt,
                        impact=row.get("impact", "HIGH"),
                        currency=row.get("currency", "USD"),
                    ))
                except (KeyError, ValueError) as e:
                    logger.warning("Skipping malformed row: %s — %s", row, e)

        # Sort by datetime for efficient lookup
        self._events.sort(key=lambda e: e.datetime_utc)
        logger.info("Loaded %d news events from %s", len(self._events), csv_path)

    def _load_default_calendar(self) -> None:
        """Load default 2025-2026 high-impact events for USD/Gold."""
        # FOMC 2025-2026 meeting dates (decision at 19:00 UTC)
        fomc_dates = [
            "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
            "2025-07-30", "2025-09-17", "2025-11-05", "2025-12-17",
            "2026-01-28", "2026-03-18", "2026-05-06", "2026-06-17",
            "2026-07-29", "2026-09-16", "2026-11-04", "2026-12-16",
        ]
        for date_str in fomc_dates:
            dt = datetime.strptime(f"{date_str} 19:00", "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            self._events.append(NewsEvent("FOMC Rate Decision", dt))

        # NFP — First Friday of each month at 13:30 UTC
        for year in (2025, 2026):
            for month in range(1, 13):
                cal = calendar.monthcalendar(year, month)
                # Find first Friday (weekday 4)
                first_friday = None
                for week in cal:
                    if week[4] != 0:  # Friday column
                        first_friday = week[4]
                        break
                if first_friday:
                    dt = datetime(year, month, first_friday, 13, 30, tzinfo=timezone.utc)
                    self._events.append(NewsEvent("Non-Farm Payrolls (NFP)", dt))

        # CPI — approximately 12th-14th of each month at 13:30 UTC
        for year in (2025, 2026):
            for month in range(1, 13):
                dt = datetime(year, month, 13, 13, 30, tzinfo=timezone.utc)
                self._events.append(NewsEvent("CPI Release", dt))

        self._events.sort(key=lambda e: e.datetime_utc)
        logger.info("Loaded %d default news events (FOMC + NFP + CPI)", len(self._events))

    @property
    def event_count(self) -> int:
        return len(self._events)
