"""Notification fan-out — fires every enabled notifier in parallel.

Solves the multi-channel problem: Slack + Telegram + Discord all need to
receive the same alerts, but each has its own surface methods. A
MultiNotifier wraps them and fans out the underlying `send(message, critical)`
plus all of the typed methods (send_trade_opened, send_emergency_stop, etc.).

Failures in one channel never block the others (each send is wrapped in
gather with return_exceptions=True, and Discord specifically swallows 5xx
already inside DiscordNotifier).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class MultiNotifier:
    """Fans out notifications to Slack, Telegram, and Discord.

    Each notifier is optional — pass whatever is enabled. The fan-out
    uses asyncio.gather with return_exceptions=True so a crash in one
    channel (e.g. webhook URL revoked) doesn't kill the others.
    """

    def __init__(self, *notifiers: Any) -> None:
        self._notifiers = [n for n in notifiers if n is not None and getattr(n, "enabled", False)]
        if self._notifiers:
            logger.info(
                "MultiNotifier: %d active channels (%s)",
                len(self._notifiers),
                ", ".join(type(n).__name__ for n in self._notifiers),
            )
        else:
            logger.warning("MultiNotifier: NO active channels — alerts will be silently dropped!")

    @property
    def enabled(self) -> bool:
        return bool(self._notifiers)

    async def _fan(self, method_name: str, *args: Any, **kwargs: Any) -> None:
        if not self._notifiers:
            return
        tasks = []
        for n in self._notifiers:
            method = getattr(n, method_name, None)
            if method is None:
                continue
            try:
                tasks.append(method(*args, **kwargs))
            except Exception:
                logger.exception(
                    "MultiNotifier: failed to schedule %s on %s",
                    method_name, type(n).__name__,
                )
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.warning("MultiNotifier: %s raised %s", method_name, r)

    # ── Generic send (forwarded to every notifier) ──

    async def send(self, message: str, *, critical: bool = False) -> None:
        # Each notifier's signature differs slightly. Try the richer signature
        # first; fall back to positional for older notifiers without `critical`.
        if not self._notifiers:
            return
        tasks = []
        for n in self._notifiers:
            try:
                if hasattr(n, "send"):
                    import inspect
                    sig = inspect.signature(n.send)
                    if "critical" in sig.parameters:
                        tasks.append(n.send(message, critical=critical))
                    else:
                        tasks.append(n.send(message))
            except Exception:
                logger.exception("MultiNotifier.send dispatch failed")
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ── Typed methods — each fans out to all notifiers ──

    async def send_trade_opened(self, **kw: Any) -> None:
        await self._fan("send_trade_opened", **kw)

    async def send_trade_closed(self, **kw: Any) -> None:
        await self._fan("send_trade_closed", **kw)

    async def send_position_modified(self, **kw: Any) -> None:
        await self._fan("send_position_modified", **kw)

    async def send_daily_summary(self, stats: dict) -> None:
        await self._fan("send_daily_summary", stats)

    async def send_error_alert(self, error: str) -> None:
        # critical=True so Discord prefixes the ping on errors
        await self._fan_with_critical("send_error_alert", error)

    async def send_emergency_stop(self, reason: str) -> None:
        # critical=True — the post-mortem's lesson #1: emergency stops MUST
        # alert loudly on every channel.
        await self._fan_with_critical("send_emergency_stop", reason)

    async def send_position_update(self, positions: list[dict]) -> None:
        await self._fan("send_position_update", positions)

    async def send_profit_milestone(self, **kw: Any) -> None:
        await self._fan("send_profit_milestone", **kw)

    async def send_loss_warning(self, **kw: Any) -> None:
        await self._fan_with_critical("send_loss_warning", **kw)

    async def send_strategy_summary(self, strategies: list[dict]) -> None:
        await self._fan("send_strategy_summary", strategies)

    async def send_foreign_position(self, **kw: Any) -> None:
        # Foreign positions are always critical (manual trade during news
        # was the proximate cause of the $5k bust).
        await self._fan_with_critical("send_foreign_position", **kw)

    async def _fan_with_critical(self, method_name: str, *args: Any, **kwargs: Any) -> None:
        """Fan out a method that should be flagged critical on Discord."""
        if not self._notifiers:
            return
        tasks = []
        for n in self._notifiers:
            method = getattr(n, method_name, None)
            if method is None:
                continue
            try:
                if "Discord" in type(n).__name__ and hasattr(n, "send"):
                    # For Discord, build a critical message via send() instead
                    # so the configured ping gets prepended.
                    import inspect
                    sig = inspect.signature(method)
                    if "critical" in sig.parameters:
                        tasks.append(method(*args, critical=True, **kwargs))
                    else:
                        # Method has no critical flag; call it normally.
                        tasks.append(method(*args, **kwargs))
                else:
                    tasks.append(method(*args, **kwargs))
            except Exception:
                logger.exception(
                    "MultiNotifier: failed to schedule %s on %s",
                    method_name, type(n).__name__,
                )
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
