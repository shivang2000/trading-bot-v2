"""Health gate — exit-on-degraded.

Existing `src/main.py:start()` already checks MT5 + Telegram on boot and
exits CRITICAL if BOTH are down. This module adds a *runtime* gate: if
the bot has been live for `degraded_grace_seconds` and degraded
components haven't recovered, raise the kill switch.

This closes the gap from `docs/PROJECT_KNOWLEDGE.md` design issue
"bot reports LIVE while dead" — once the boot gate has passed, an MT5
disconnect that drags on (e.g., Wine crashed, RPyC port died) currently
keeps the bot in 'running' state with no trades fired. Health gate
escalates to a process exit so Docker's restart-policy brings it back.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from src.mt5.client import AsyncMT5Client

logger = logging.getLogger(__name__)


class HealthGate:
    """Periodic liveness check.

    Owns its own asyncio task. Calling `start()` schedules a background
    loop that polls MT5 + a caller-supplied `telegram_health_func`
    (typically `lambda: listener._client is not None and listener._running`).

    On `degraded_grace_seconds` of continuous degradation, fires
    `on_critical()` callback (default: sets a shutdown event so main.py's
    `_shutdown_event.wait()` returns).
    """

    def __init__(
        self,
        mt5_client: AsyncMT5Client,
        telegram_health_func: Callable[[], bool],
        on_critical: Callable[[str], None],
        check_interval_seconds: int = 60,
        degraded_grace_seconds: int = 300,  # 5 minutes
    ) -> None:
        self._mt5 = mt5_client
        self._telegram_health = telegram_health_func
        self._on_critical = on_critical
        self._interval = check_interval_seconds
        self._grace = degraded_grace_seconds

        self._task: asyncio.Task | None = None
        self._running = False
        self._first_degraded_at: float | None = None
        self._consecutive_failures = 0

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "HealthGate started (check every %ds, grace %ds)",
            self._interval, self._grace,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._interval)
                await self._check_once()
            except Exception:
                logger.exception("HealthGate loop error")

    async def _check_once(self) -> None:
        mt5_ok = self._mt5.is_connected
        telegram_ok = False
        try:
            telegram_ok = bool(self._telegram_health())
        except Exception:
            telegram_ok = False

        # If both are healthy, reset the degraded timer.
        if mt5_ok and telegram_ok:
            if self._first_degraded_at is not None:
                logger.info("HealthGate: components recovered after degraded period")
            self._first_degraded_at = None
            self._consecutive_failures = 0
            return

        # Both down for `grace` continuous seconds → escalate to critical.
        # Single-component degradation is logged but not fatal — the bot
        # can still trade with one channel up (e.g., MT5 alone if Telegram
        # is the only signal source disabled in config).
        now = time.monotonic()
        if self._first_degraded_at is None:
            self._first_degraded_at = now
            logger.warning(
                "HealthGate: degraded — mt5=%s telegram=%s (grace %ds)",
                mt5_ok, telegram_ok, self._grace,
            )
            return

        elapsed = now - self._first_degraded_at
        self._consecutive_failures += 1

        if not mt5_ok and not telegram_ok and elapsed >= self._grace:
            msg = (
                f"HealthGate CRITICAL: MT5 + Telegram both down for {elapsed:.0f}s "
                f"({self._consecutive_failures} consecutive failed checks). "
                "Triggering shutdown — Docker restart policy will relaunch."
            )
            logger.critical(msg)
            self._on_critical(msg)
            self._running = False
