"""Slack notification service.

Sends trade alerts, daily summaries, and error notifications
via Slack Incoming Webhooks. Uses httpx for async HTTP requests.
"""

from __future__ import annotations

import logging

import httpx

from src.config.schema import SlackConfig

logger = logging.getLogger(__name__)


class SlackNotifier:
    """Sends notifications via Slack Incoming Webhooks."""

    def __init__(self, config: SlackConfig) -> None:
        self._config = config
        self._enabled = config.enabled and bool(config.webhook_url)

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send(self, message: str) -> bool:
        """Send a text message. Returns True on success."""
        if not self._enabled:
            logger.debug("Slack disabled, skipping: %s", message[:50])
            return False

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self._config.webhook_url,
                    json={"text": message},
                )
                if resp.status_code == 200:
                    return True
                logger.warning("Slack send failed: %d %s", resp.status_code, resp.text)
                return False
        except Exception:
            logger.exception("Slack send error")
            return False

    async def send_trade_opened(
        self,
        symbol: str,
        side: str,
        volume: float,
        price: float,
        stop_loss: float | None = None,
        take_profit: float | None = None,
        source: str = "",
    ) -> bool:
        """Send notification when a trade is opened."""
        sl_str = f"\nSL: {stop_loss:.2f}" if stop_loss else ""
        tp_str = f" | TP: {take_profit:.2f}" if take_profit else ""
        source_str = f"\nSource: {source}" if source else ""
        msg = (
            f":large_green_circle: *Trade Opened*\n"
            f"{symbol} {side} {volume} @ {price:.2f}"
            f"{sl_str}{tp_str}{source_str}"
        )
        return await self.send(msg)

    async def send_trade_closed(
        self,
        symbol: str,
        side: str,
        volume: float,
        close_price: float,
        pnl: float,
        duration_hours: float = 0.0,
        source: str = "",
    ) -> bool:
        """Send notification when a trade is closed."""
        emoji = ":moneybag:" if pnl >= 0 else ":red_circle:"
        source_str = f"\nSource: {source}" if source else ""
        msg = (
            f"{emoji} *Trade Closed*\n"
            f"{symbol} {side} {volume} @ {close_price:.2f}\n"
            f"P&L: ${pnl:+.2f}\n"
            f"Duration: {duration_hours:.1f}h{source_str}"
        )
        return await self.send(msg)

    async def send_position_modified(
        self,
        symbol: str,
        ticket: int,
        stop_loss: float | None = None,
        take_profit: float | None = None,
    ) -> bool:
        """Send notification when a position's SL/TP is modified."""
        sl_str = f"SL: {stop_loss:.2f}" if stop_loss is not None else ""
        tp_str = f"TP: {take_profit:.2f}" if take_profit is not None else ""
        levels = " | ".join(filter(None, [sl_str, tp_str]))
        msg = (
            f":pencil2: *Position Modified*\n"
            f"{symbol} (ticket {ticket})\n"
            f"{levels}"
        )
        return await self.send(msg)

    async def send_daily_summary(self, stats: dict) -> bool:
        """Send end-of-day summary."""
        msg = (
            f":bar_chart: *Daily Summary*\n"
            f"Trades: {stats.get('trades', 0)}\n"
            f"Wins: {stats.get('wins', 0)} | Losses: {stats.get('losses', 0)}\n"
            f"P&L: ${stats.get('pnl', 0.0):+.2f}\n"
            f"Balance: ${stats.get('balance', 0.0):.2f}\n"
            f"Equity: ${stats.get('equity', 0.0):.2f}"
        )
        return await self.send(msg)

    async def send_error_alert(self, error: str) -> bool:
        """Send an error notification."""
        msg = f":warning: *Error Alert*\n{error}"
        return await self.send(msg)

    async def send_emergency_stop(self, reason: str) -> bool:
        """Send emergency stop notification."""
        msg = f":rotating_light: *EMERGENCY STOP*\n{reason}"
        return await self.send(msg)
