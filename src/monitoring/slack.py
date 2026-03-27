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
        strategy_name: str = "",
        confidence: float = 0.0,
        session: str = "",
        risk_amount: float = 0.0,
        rr_ratio: float = 0.0,
        equity: float = 0.0,
    ) -> bool:
        """Send notification when a trade is opened."""
        sl_str = f"\nSL: {stop_loss:.2f}" if stop_loss else ""
        tp_str = f" | TP: {take_profit:.2f}" if take_profit else ""
        strategy_str = f"\nStrategy: {strategy_name}" if strategy_name else ""
        conf_str = f" | Confidence: {confidence:.0%}" if confidence else ""
        session_str = f"\nSession: {session}" if session else ""
        risk_str = f"\nRisk: ${risk_amount:.2f}" if risk_amount else ""
        rr_str = f" | R:R 1:{rr_ratio:.1f}" if rr_ratio else ""
        equity_str = f"\nEquity: ${equity:.2f}" if equity else ""
        msg = (
            f":large_green_circle: *Trade Opened*\n"
            f"{symbol} {side} {volume} @ {price:.2f}"
            f"{sl_str}{tp_str}{strategy_str}{conf_str}"
            f"{session_str}{risk_str}{rr_str}{equity_str}"
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
        strategy_name: str = "",
        daily_pnl: float = 0.0,
        daily_wins: int = 0,
        daily_losses: int = 0,
        equity: float = 0.0,
        streak: int = 0,
    ) -> bool:
        """Send notification when a trade is closed."""
        emoji = ":moneybag:" if pnl >= 0 else ":red_circle:"
        strategy_str = f"\nStrategy: {strategy_name}" if strategy_name else ""
        streak_str = ""
        if streak > 0:
            streak_str = f"\n:fire: Win streak: {streak}"
        elif streak < 0:
            streak_str = f"\n:chart_with_downwards_trend: Loss streak: {abs(streak)}"
        daily_str = f"\nDaily: {daily_wins}W/{daily_losses}L | P&L: ${daily_pnl:+.2f}" if daily_wins + daily_losses > 0 else ""
        equity_str = f"\nEquity: ${equity:.2f}" if equity else ""
        msg = (
            f"{emoji} *Trade Closed*\n"
            f"{symbol} {side} {volume} @ {close_price:.2f}\n"
            f"P&L: ${pnl:+.2f} | Duration: {duration_hours:.1f}h"
            f"{strategy_str}{streak_str}{daily_str}{equity_str}"
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

    async def send_position_update(self, positions: list[dict]) -> bool:
        """Send periodic update of all open positions with unrealized P&L."""
        if not positions:
            return await self.send(":bar_chart: No open positions")

        lines = [":bar_chart: *Open Positions Update*"]
        total_pnl = 0.0
        for p in positions:
            emoji = ":green_circle:" if p.get("pnl", 0) >= 0 else ":red_circle:"
            lines.append(
                f"{emoji} #{p.get('ticket', '?')} {p.get('symbol', '')} {p.get('side', '')} "
                f"{p.get('volume', 0):.2f} lots | Entry: {p.get('entry', 0):.2f} | "
                f"Now: {p.get('price', 0):.2f} | P&L: ${p.get('pnl', 0):+.2f} | "
                f"Strategy: {p.get('strategy', 'unknown')}"
            )
            total_pnl += p.get("pnl", 0)
        lines.append(f"\n*Total unrealized: ${total_pnl:+.2f}*")
        return await self.send("\n".join(lines))

    async def send_profit_milestone(
        self, ticket: int, symbol: str, side: str, pnl: float, milestone: float,
    ) -> bool:
        """Alert when unrealized profit hits a milestone."""
        msg = (
            f":chart_with_upwards_trend: *Profit Milestone*\n"
            f"#{ticket} {symbol} {side} now +${pnl:.2f}! (hit ${milestone:.0f} milestone)"
        )
        return await self.send(msg)

    async def send_loss_warning(
        self, ticket: int, symbol: str, side: str, pnl: float,
    ) -> bool:
        """Warn when unrealized loss is significant."""
        msg = (
            f":warning: *Loss Warning*\n"
            f"#{ticket} {symbol} {side} now ${pnl:.2f}!"
        )
        return await self.send(msg)

    async def send_strategy_summary(self, strategies: list[dict]) -> bool:
        """Send per-strategy performance summary."""
        lines = [":clipboard: *Strategy Performance*"]
        for s in strategies:
            emoji = ":white_check_mark:" if s.get("pnl", 0) >= 0 else ":x:"
            lines.append(
                f"{emoji} {s.get('name', '?')}: {s.get('trades', 0)} trades | "
                f"{s.get('wins', 0)}W/{s.get('losses', 0)}L | P&L: ${s.get('pnl', 0):+.2f}"
            )
        return await self.send("\n".join(lines))
