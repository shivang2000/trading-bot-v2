"""Daily P&L summary: generates and sends end-of-day reports.

Runs on a schedule (or triggered manually) to compile daily
trading statistics and push them via Telegram + Slack.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from src.monitoring.notifier import TelegramNotifier
from src.monitoring.slack import SlackNotifier
from src.tracking.database import TrackingDB

logger = logging.getLogger(__name__)


class DailySummary:
    """Generates and sends daily trading summaries."""

    def __init__(
        self,
        tracking_db: TrackingDB,
        telegram_notifier: TelegramNotifier,
        slack_notifier: SlackNotifier,
        account_state_func: Callable[[], Any] | None = None,
    ) -> None:
        self._db = tracking_db
        self._telegram = telegram_notifier
        self._slack = slack_notifier
        self._account_state_func = account_state_func
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Start the daily summary scheduler."""
        self._running = True
        self._task = asyncio.create_task(self._schedule_loop())
        logger.info("DailySummary scheduler started")

    async def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _schedule_loop(self) -> None:
        """Run daily summary at end of each trading day (23:55 UTC)."""
        while self._running:
            now = datetime.now(timezone.utc)
            # Calculate seconds until 23:55 UTC
            target_hour, target_minute = 23, 55
            target = now.replace(
                hour=target_hour, minute=target_minute, second=0, microsecond=0
            )
            if now >= target:
                # Already past today's target, schedule for tomorrow
                from datetime import timedelta
                target += timedelta(days=1)

            wait_seconds = (target - now).total_seconds()
            logger.debug("Next daily summary in %.0f seconds", wait_seconds)

            try:
                await asyncio.sleep(wait_seconds)
                await self.generate_and_send()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error generating daily summary")
                # Wait a bit before retrying
                await asyncio.sleep(60)

    async def generate_and_send(self) -> None:
        """Generate today's summary and send via all notification channels."""
        stats = await self._get_daily_stats()

        logger.info(
            "Daily summary: %d trades, %d wins, %d losses, P&L: $%.2f",
            stats.get("trades", 0),
            stats.get("wins", 0),
            stats.get("losses", 0),
            stats.get("pnl", 0.0),
        )

        await self._telegram.send_daily_summary(stats)
        await self._slack.send_daily_summary(stats)

    async def _get_daily_stats(self) -> dict[str, Any]:
        """Compile today's trading statistics."""
        try:
            db_stats = await self._db.get_daily_stats()
        except Exception:
            logger.warning("Failed to fetch daily stats from DB")
            db_stats = {}

        stats: dict[str, Any] = {
            "trades": db_stats.get("total_trades", 0) or 0,
            "wins": db_stats.get("winning_trades", 0) or 0,
            "losses": db_stats.get("losing_trades", 0) or 0,
            "pnl": db_stats.get("total_pnl", 0.0) or 0.0,
            "balance": 0.0,
            "equity": 0.0,
        }

        # Add account info if available
        if self._account_state_func:
            try:
                account = self._account_state_func()
                stats["balance"] = account.balance
                stats["equity"] = account.equity
            except Exception:
                logger.debug("Could not fetch account state for daily summary")

        return stats
