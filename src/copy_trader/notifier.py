"""Slack notifier for mirror events.

Async, non-blocking — failures are logged but never propagate to the
poll loop. We never want a Slack outage to block trade execution.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)


class SlackNotifier:
    def __init__(
        self,
        webhook_url: str,
        channel_label: str = "copy-trader",
        enabled: bool = True,
    ) -> None:
        self._url = webhook_url
        self._label = channel_label
        self._enabled = enabled and bool(webhook_url)
        if not self._enabled:
            logger.info("SlackNotifier disabled (no webhook or enabled=false)")

    async def post(self, text: str, *, fields: Optional[dict[str, Any]] = None) -> None:
        if not self._enabled:
            return
        prefix = f"[{self._label}] "
        body: dict[str, Any] = {"text": prefix + text}
        if fields:
            body["attachments"] = [{
                "color": "#36a64f" if "✅" in text or "OPEN" in text or "CLOSE" in text else "#cc0000",
                "fields": [{"title": k, "value": str(v), "short": True} for k, v in fields.items()],
            }]
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
                async with session.post(self._url, json=body) as resp:
                    if resp.status >= 400:
                        logger.warning("Slack post returned %d: %s", resp.status, await resp.text())
        except Exception:
            logger.warning("Slack post failed", exc_info=True)

    async def open(self, src_ticket: int, dest_ticket: int, symbol: str, side: str, volume: float, price: float) -> None:
        await self.post(
            f"✅ MIRROR OPEN  {symbol} {side} {volume} @ {price:.2f}",
            fields={"src_ticket": src_ticket, "dest_ticket": dest_ticket},
        )

    async def close(self, src_ticket: int, dest_ticket: int, symbol: str) -> None:
        await self.post(
            f"🔒 MIRROR CLOSE  {symbol}",
            fields={"src_ticket": src_ticket, "dest_ticket": dest_ticket},
        )

    async def modify(self, src_ticket: int, dest_ticket: int, sl: Optional[float], tp: Optional[float]) -> None:
        await self.post(
            f"✏️  MIRROR MODIFY  SL={sl} TP={tp}",
            fields={"src_ticket": src_ticket, "dest_ticket": dest_ticket},
        )

    async def error(self, msg: str, fields: Optional[dict[str, Any]] = None) -> None:
        await self.post(f"❌ {msg}", fields=fields)
