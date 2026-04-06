"""Trading Session Management.

Defines and manages trading sessions for forex/gold markets:
- Asian session (Tokyo)
- London session
- New York session
- Session overlaps (highest volatility)

Gold (XAUUSD) is most volatile during:
- London/NY overlap (13:00-17:00 UTC)
- London open (08:00-10:00 UTC)
- NY open (13:00-15:00 UTC)

Usage:
    session_mgr = SessionManager()
    current = session_mgr.get_current_session()

    if current.is_high_volatility:
        print(f"High volatility session: {current.name}")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class SessionName(str, Enum):
    ASIAN = "asian"
    LONDON = "london"
    NEW_YORK = "new_york"
    LONDON_NY_OVERLAP = "london_ny_overlap"
    ASIAN_LONDON_OVERLAP = "asian_london_overlap"
    CLOSED = "closed"


class SessionPriority(str, Enum):
    LOW = "low"  # Avoid trading
    MEDIUM = "medium"  # Trade with caution
    HIGH = "high"  # Good trading conditions
    BEST = "best"  # Optimal trading conditions


@dataclass
class TradingSession:
    """A trading session with its characteristics."""

    name: SessionName
    start_time: time
    end_time: time
    is_active: bool = False
    priority: SessionPriority = SessionPriority.MEDIUM
    volatility_rating: float = 1.0  # 0.5 to 2.0
    spread_multiplier: float = 1.0  # Expected spread multiplier
    recommended_for_gold: bool = True

    @property
    def is_high_volatility(self) -> bool:
        return self.volatility_rating >= 1.5

    @property
    def is_good_for_trading(self) -> bool:
        return self.priority in (SessionPriority.HIGH, SessionPriority.BEST)

    @property
    def display_name(self) -> str:
        names = {
            SessionName.ASIAN: "Asian (Tokyo)",
            SessionName.LONDON: "London",
            SessionName.NEW_YORK: "New York",
            SessionName.LONDON_NY_OVERLAP: "London/NY Overlap",
            SessionName.ASIAN_LONDON_OVERLAP: "Asian/London Overlap",
            SessionName.CLOSED: "Market Closed",
        }
        return names.get(self.name, self.name.value)


class SessionManager:
    """Manages trading sessions and provides session-aware functionality.

    Session times are in UTC (from babypips.com):
    - Sydney/Asian: 21:00 - 09:00 UTC (spans midnight)
    - London: 07:00 - 16:00 UTC
    - New York: 12:00 - 21:00 UTC
    - London/NY Overlap: 12:00 - 16:00 UTC (BEST for gold)
    - Tokyo/London Overlap: 07:00 - 09:00 UTC
    - Market open: Sunday 21:00 UTC / close: Friday 21:00 UTC
    """

    # Define sessions (all times in UTC)
    SESSIONS: list[dict[str, Any]] = [
        {
            "name": SessionName.LONDON_NY_OVERLAP,
            "start": time(12, 0),
            "end": time(16, 0),
            "priority": SessionPriority.BEST,
            "volatility": 2.0,
            "spread_mult": 0.8,  # Lower spreads due to high liquidity
            "gold_recommended": True,
        },
        {
            "name": SessionName.LONDON,
            "start": time(7, 0),
            "end": time(12, 0),
            "priority": SessionPriority.HIGH,
            "volatility": 1.5,
            "spread_mult": 0.9,
            "gold_recommended": True,
        },
        {
            "name": SessionName.NEW_YORK,
            "start": time(12, 0),
            "end": time(20, 59),  # Ends just before 21:00 so Asian takes over
            "priority": SessionPriority.HIGH,
            "volatility": 1.5,
            "spread_mult": 0.9,
            "gold_recommended": True,
        },
        {
            "name": SessionName.ASIAN_LONDON_OVERLAP,
            "start": time(7, 0),
            "end": time(9, 0),
            "priority": SessionPriority.MEDIUM,
            "volatility": 1.2,
            "spread_mult": 1.0,
            "gold_recommended": True,
        },
        {
            "name": SessionName.ASIAN,
            "start": time(21, 0),  # Sydney opens 21:00 UTC Sunday — spans midnight
            "end": time(9, 0),     # Tokyo closes 09:00 UTC
            "priority": SessionPriority.LOW,
            "volatility": 0.7,
            "spread_mult": 1.3,  # Higher spreads
            "gold_recommended": False,  # Not ideal for gold scalping
        },
    ]

    def __init__(
        self,
        allowed_sessions: list[SessionName] | None = None,
        blocked_sessions: list[SessionName] | None = None,
        weekend_trading: bool = False,
    ) -> None:
        self.allowed_sessions = allowed_sessions
        self.blocked_sessions = blocked_sessions or []
        self.weekend_trading = weekend_trading

    def get_current_session(self, dt: datetime | None = None) -> TradingSession:
        """Get the current trading session.

        Args:
            dt: DateTime to check. None = current time.

        Returns:
            TradingSession with current status and characteristics.
        """
        if dt is None:
            dt = datetime.utcnow()

        # Check weekend — Saturday fully closed, Sunday closed until 22:00 UTC (Sydney open)
        if not self.weekend_trading:
            if dt.weekday() == 5:  # Saturday = fully closed
                return TradingSession(
                    name=SessionName.CLOSED,
                    start_time=time(0, 0),
                    end_time=time(0, 0),
                    priority=SessionPriority.LOW,
                    volatility_rating=0.0,
                    recommended_for_gold=False,
                )
            if dt.weekday() == 4 and dt.hour >= 21:  # Friday after 21:00 UTC = market closed
                return TradingSession(
                    name=SessionName.CLOSED,
                    start_time=time(0, 0),
                    end_time=time(0, 0),
                    priority=SessionPriority.LOW,
                    volatility_rating=0.0,
                    recommended_for_gold=False,
                )
            if dt.weekday() == 6 and dt.hour < 21:  # Sunday before 21:00 UTC = closed (Sydney opens 21:00)
                return TradingSession(
                    name=SessionName.CLOSED,
                    start_time=time(0, 0),
                    end_time=time(0, 0),
                    priority=SessionPriority.LOW,
                    volatility_rating=0.0,
                    recommended_for_gold=False,
                )

        current_time = dt.time()

        # Check each session (order matters - overlaps first)
        for session_def in self.SESSIONS:
            start = session_def["start"]
            end = session_def["end"]

            # Handle sessions that span midnight (like Asian)
            if start <= end:
                is_active = start <= current_time <= end
            else:
                is_active = current_time >= start or current_time <= end

            if is_active:
                return TradingSession(
                    name=session_def["name"],
                    start_time=start,
                    end_time=end,
                    is_active=True,
                    priority=session_def["priority"],
                    volatility_rating=session_def["volatility"],
                    spread_multiplier=session_def["spread_mult"],
                    recommended_for_gold=session_def["gold_recommended"],
                )

        # Default to closed if no session matches
        return TradingSession(
            name=SessionName.CLOSED,
            start_time=time(0, 0),
            end_time=time(0, 0),
            priority=SessionPriority.LOW,
            volatility_rating=0.0,
            recommended_for_gold=False,
        )

    def get_all_active_sessions(self, dt: datetime | None = None) -> list[TradingSession]:
        """Get all currently active sessions (including overlaps)."""
        if dt is None:
            dt = datetime.utcnow()

        current_time = dt.time()
        active: list[TradingSession] = []

        for session_def in self.SESSIONS:
            start = session_def["start"]
            end = session_def["end"]

            if start <= end:
                is_active = start <= current_time <= end
            else:
                is_active = current_time >= start or current_time <= end

            if is_active:
                active.append(
                    TradingSession(
                        name=session_def["name"],
                        start_time=start,
                        end_time=end,
                        is_active=True,
                        priority=session_def["priority"],
                        volatility_rating=session_def["volatility"],
                        spread_multiplier=session_def["spread_mult"],
                        recommended_for_gold=session_def["gold_recommended"],
                    )
                )

        return active

    def is_trading_allowed(self, dt: datetime | None = None) -> bool:
        """Check if trading is allowed at the given time.

        Considers:
        - Weekend restrictions
        - Allowed/blocked sessions
        - Session quality
        """
        session = self.get_current_session(dt)

        # Check if closed
        if session.name == SessionName.CLOSED:
            return False

        # Check blocked sessions
        if session.name in self.blocked_sessions:
            return False

        # Check allowed sessions (if configured)
        if self.allowed_sessions is not None:
            return session.name in self.allowed_sessions

        # Default: allow if session is not blocked
        return True

    def get_next_session(self, dt: datetime | None = None) -> TradingSession:
        """Get the next upcoming session."""
        if dt is None:
            dt = datetime.utcnow()

        current_time = dt.time()

        # Find next session
        upcoming: list[tuple[time, dict[str, Any]]] = []

        for session_def in self.SESSIONS:
            start = session_def["start"]

            if start > current_time:
                upcoming.append((start, session_def))

        if upcoming:
            # Sort by start time and return first
            upcoming.sort(key=lambda x: x[0])
            next_def = upcoming[0][1]
            return TradingSession(
                name=next_def["name"],
                start_time=next_def["start"],
                end_time=next_def["end"],
                priority=next_def["priority"],
                volatility_rating=next_def["volatility"],
                spread_multiplier=next_def["spread_mult"],
                recommended_for_gold=next_def["gold_recommended"],
            )

        # If no upcoming session today, return first session of next day
        first_session = min(self.SESSIONS, key=lambda s: s["start"])
        return TradingSession(
            name=first_session["name"],
            start_time=first_session["start"],
            end_time=first_session["end"],
            priority=first_session["priority"],
            volatility_rating=first_session["volatility"],
            spread_multiplier=first_session["spread_mult"],
            recommended_for_gold=first_session["gold_recommended"],
        )

    def get_session_schedule(self) -> dict[SessionName, dict[str, Any]]:
        """Get full session schedule for display/configuration."""
        schedule = {}
        for session_def in self.SESSIONS:
            schedule[session_def["name"]] = {
                "start_utc": session_def["start"].strftime("%H:%M"),
                "end_utc": session_def["end"].strftime("%H:%M"),
                "priority": session_def["priority"].value,
                "volatility": session_def["volatility"],
                "spread_multiplier": session_def["spread_mult"],
                "recommended_for_gold": session_def["gold_recommended"],
            }
        return schedule

    def get_gold_optimal_windows(self) -> list[dict[str, str]]:
        """Get optimal trading windows for gold (XAUUSD).

        Returns:
            List of optimal windows with start/end times and rationale.
        """
        return [
            {
                "name": "London/NY Overlap",
                "start_utc": "13:00",
                "end_utc": "17:00",
                "rationale": "Highest volume, tightest spreads, best liquidity",
                "priority": "BEST",
            },
            {
                "name": "London Open",
                "start_utc": "08:00",
                "end_utc": "10:00",
                "rationale": "Strong directional moves, good for breakout strategies",
                "priority": "HIGH",
            },
            {
                "name": "NY Open",
                "start_utc": "13:00",
                "end_utc": "15:00",
                "rationale": "News releases, high volatility, good for momentum",
                "priority": "HIGH",
            },
        ]

    def should_reduce_position_size(self, dt: datetime | None = None) -> bool:
        """Check if position size should be reduced due to low liquidity.

        Reduce size during:
        - Asian session for gold
        - Low priority sessions
        """
        session = self.get_current_session(dt)
        return session.priority in (SessionPriority.LOW, SessionPriority.MEDIUM)

    def get_spread_adjustment(self, dt: datetime | None = None) -> float:
        """Get spread multiplier for current session.

        Used to adjust for wider spreads in low-liquidity sessions.
        """
        session = self.get_current_session(dt)
        return session.spread_multiplier
