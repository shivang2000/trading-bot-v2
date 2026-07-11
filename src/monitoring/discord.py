"""Discord notification service.

Parallel to SlackNotifier — uses Discord Incoming Webhooks (POST with JSON,
same simplicity as Slack). Auto-disabled when DISCORD_WEBHOOK_URL is unset, so
the bot runs fine without it. Recommended to be set up as a secondary sink
alongside Slack or Telegram — the post-mortem lesson #1: silent failure on a
funded account is exactly how the previous $5k bust happened.

To create a webhook:
  1. Open Discord → server → #trading-bot-fundingpips channel
  2. Channel settings → Integrations → Webhooks → New Webhook
  3. Copy the webhook URL
  4. Set DISCORD_WEBHOOK_URL in .env (or EC2 SSM parameter for production)
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)


class DiscordConfig:
    """Minimal config — pulls from env vars directly.

    Two modes:
    1. WEBHOOK: set DISCORD_WEBHOOK_URL — simplest, one-way, no bot needed.
    2. BOT TOKEN: set DISCORD_BOT_TOKEN + DISCORD_ALERT_CHANNEL_ID — posts as
       the Hermes bot (hermesagent#5141), same identity as the gateway.

    Keeping it simple: no Pydantic schema entry needed since this is
    a pure-deployment-time decision. The bot never *requires* Discord.
    """

    def __init__(self) -> None:
        self.webhook_url: str = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
        # Bot-token mode: post to a channel using the Hermes bot's token.
        # On EC2, set these in .env or AWS SSM. The channel ID for #trading-bot
        # is 1525476646176690297 (found via `hermes send --list discord`).
        self.bot_token: str = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
        self.alert_channel_id: str = os.environ.get("DISCORD_ALERT_CHANNEL_ID", "").strip()
        # Optional: tag every message with a username (shows as the bot's name)
        self.username: str = os.environ.get("DISCORD_BOT_USERNAME", "FundingPips Bot").strip()
        # Optional: avatar URL for the webhook
        self.avatar_url: str = os.environ.get("DISCORD_BOT_AVATAR", "").strip()
        # Optional: ping a role on critical alerts (e.g. "@here" or "<@&ROLE_ID>")
        self.critical_ping: str = os.environ.get("DISCORD_CRITICAL_PING", "").strip()

    @property
    def enabled(self) -> bool:
        return (
            bool(self.webhook_url)
            and self.webhook_url.startswith(
                ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
            )
        ) or (
            bool(self.bot_token)
            and bool(self.alert_channel_id)
            and self.bot_token.startswith(("MTA", "MTU", "MTI"))
        )

    @property
    def mode(self) -> str:
        """Returns 'webhook', 'bot_token', or 'disabled'."""
        if self.webhook_url and self.webhook_url.startswith(
            ("https://discord.com/api/webhooks/", "https://discordapp.com/api/webhooks/")
        ):
            return "webhook"
        if self.bot_token and self.alert_channel_id:
            return "bot_token"
        return "disabled"


class DiscordNotifier:
    """Sends notifications via Discord Incoming Webhooks.

    Mirrors SlackNotifier's surface so the existing call sites work with
    either. Failures are logged and swallowed — Discord is a secondary
    channel; the bot must not stop trading if Discord is down.
    """

    def __init__(self, config: DiscordConfig | None = None) -> None:
        self._config = config or DiscordConfig()
        self._enabled = self._config.enabled
        if self._enabled:
            logger.info(
                "DiscordNotifier: enabled (mode=%s, username=%s, critical_ping=%s)",
                self._config.mode,
                self._config.username,
                "yes" if self._config.critical_ping else "no",
            )
        else:
            logger.debug(
                "DiscordNotifier: disabled (no webhook URL or bot token + channel ID)"
            )

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def send(self, message: str, *, critical: bool = False) -> bool:
        """Send a text message to Discord. Returns True on success.

        Args:
            message: The message body. Discord webhooks support Markdown.
            critical: If True, prepend the configured critical_ping mention
                      (e.g. "<@&1234567890>" or "@here") to draw attention.
        """
        if not self._enabled:
            return False

        # Discord has a 2000-char limit per message; truncate with a marker.
        body = message
        if critical and self._config.critical_ping:
            body = f"{self._config.critical_ping} {body}"
        if len(body) > 1900:
            body = body[:1900] + "\n…(truncated)"

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                if self._config.mode == "bot_token":
                    # Bot-token mode: POST to Discord's channel message endpoint.
                    # Posts as hermesagent#5141 (same identity as Hermes gateway).
                    url = (
                        f"https://discord.com/api/v10/channels/"
                        f"{self._config.alert_channel_id}/messages"
                    )
                    headers = {
                        "Authorization": f"Bot {self._config.bot_token}",
                        "Content-Type": "application/json",
                    }
                    payload = {"content": body}
                    resp = await client.post(url, json=payload, headers=headers)
                    if resp.status_code in (200, 201, 204):
                        return True
                    logger.warning(
                        "Discord send failed (bot_token): %d %s",
                        resp.status_code, resp.text[:200],
                    )
                    return False
                else:
                    # Webhook mode: POST to the webhook URL.
                    payload: dict = {
                        "content": body,
                        "username": self._config.username,
                    }
                    if self._config.avatar_url:
                        payload["avatar_url"] = self._config.avatar_url
                    resp = await client.post(
                        self._config.webhook_url, json=payload,
                    )
                    # Discord webhooks return 204 No Content on success
                    if resp.status_code in (200, 204):
                        return True
                    logger.warning(
                        "Discord send failed (webhook): %d %s",
                        resp.status_code, resp.text[:200],
                    )
                    return False
        except Exception:
            logger.exception("Discord send error")
            return False

    # ── Mirrored Slack surface — used by SlackLogger (if wired) and bot code ──

    async def send_foreign_position(
        self,
        ticket: int,
        symbol: str,
        side: str,
        volume: float,
        entry_price: float,
        magic: int,
        comment: str = "",
        account_label: str = "",
    ) -> bool:
        """Alert on a position not placed by the bot (magic != BOT_MAGIC)."""
        acct = f" — {account_label}" if account_label else ""
        msg = (
            f"🚨 **FOREIGN POSITION DETECTED**{acct}\n"
            f"Bot did **NOT** place this trade.\n"
            f"Ticket: `{ticket}` | {symbol} {side} {volume} lots @ {entry_price}\n"
            f"Magic: `{magic}` (expected `200000`)\n"
            f"Comment: `{comment or '<empty>'}`\n"
            f"\nPossible causes: manual trade, leaked master password, or another EA running."
        )
        return await self.send(msg, critical=True)

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
        sl_str = f"\nSL: {stop_loss:.2f}" if stop_loss else ""
        tp_str = f" | TP: {take_profit:.2f}" if take_profit else ""
        strategy_str = f"\nStrategy: {strategy_name}" if strategy_name else ""
        conf_str = f" | Confidence: {confidence:.0%}" if confidence else ""
        session_str = f"\nSession: {session}" if session else ""
        risk_str = f"\nRisk: ${risk_amount:.2f}" if risk_amount else ""
        rr_str = f" | R:R 1:{rr_ratio:.1f}" if rr_ratio else ""
        equity_str = f"\nEquity: ${equity:.2f}" if equity else ""
        msg = (
            f"🟢 **Trade Opened**\n"
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
        emoji = "💰" if pnl >= 0 else "🔴"
        strategy_str = f"\nStrategy: {strategy_name}" if strategy_name else ""
        streak_str = ""
        if streak > 0:
            streak_str = f"\n🔥 Win streak: {streak}"
        elif streak < 0:
            streak_str = f"\n📉 Loss streak: {abs(streak)}"
        daily_str = (
            f"\nDaily: {daily_wins}W/{daily_losses}L | P&L: ${daily_pnl:+.2f}"
            if daily_wins + daily_losses > 0 else ""
        )
        equity_str = f"\nEquity: ${equity:.2f}" if equity else ""
        msg = (
            f"{emoji} **Trade Closed**\n"
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
        sl_str = f"SL: {stop_loss:.2f}" if stop_loss is not None else ""
        tp_str = f"TP: {take_profit:.2f}" if take_profit is not None else ""
        levels = " | ".join(filter(None, [sl_str, tp_str]))
        msg = f"✏️ **Position Modified**\n{symbol} (ticket {ticket})\n{levels}"
        return await self.send(msg)

    async def send_daily_summary(self, stats: dict) -> bool:
        msg = (
            f"📊 **Daily Summary**\n"
            f"Trades: {stats.get('trades', 0)}\n"
            f"Wins: {stats.get('wins', 0)} | Losses: {stats.get('losses', 0)}\n"
            f"P&L: ${stats.get('pnl', 0.0):+.2f}\n"
            f"Balance: ${stats.get('balance', 0.0):.2f}\n"
            f"Equity: ${stats.get('equity', 0.0):.2f}"
        )
        return await self.send(msg)

    async def send_error_alert(self, error: str) -> bool:
        return await self.send(f"⚠️ **Error Alert**\n{error}", critical=True)

    async def send_emergency_stop(self, reason: str) -> bool:
        return await self.send(f"🚨 **EMERGENCY STOP**\n{reason}", critical=True)

    async def send_position_update(self, positions: list[dict]) -> bool:
        if not positions:
            return await self.send("📊 No open positions")
        lines = ["📊 **Open Positions Update**"]
        total_pnl = 0.0
        for p in positions:
            emoji = "🟢" if p.get("pnl", 0) >= 0 else "🔴"
            lines.append(
                f"{emoji} #{p.get('ticket', '?')} {p.get('symbol', '')} {p.get('side', '')} "
                f"{p.get('volume', 0):.2f} lots | Entry: {p.get('entry', 0):.2f} | "
                f"Now: {p.get('price', 0):.2f} | P&L: ${p.get('pnl', 0):+.2f} | "
                f"Strategy: {p.get('strategy', 'unknown')}"
            )
            total_pnl += p.get("pnl", 0)
        lines.append(f"\n**Total unrealized: ${total_pnl:+.2f}**")
        return await self.send("\n".join(lines))

    async def send_profit_milestone(
        self, ticket: int, symbol: str, side: str, pnl: float, milestone: float,
    ) -> bool:
        msg = (
            f"📈 **Profit Milestone**\n"
            f"#{ticket} {symbol} {side} now +${pnl:.2f}! (hit ${milestone:.0f} milestone)"
        )
        return await self.send(msg)

    async def send_loss_warning(
        self, ticket: int, symbol: str, side: str, pnl: float,
    ) -> bool:
        return await self.send(
            f"⚠️ **Loss Warning**\n#{ticket} {symbol} {side} now ${pnl:.2f}!",
            critical=True,
        )

    async def send_strategy_summary(self, strategies: list[dict]) -> bool:
        lines = ["📋 **Strategy Performance**"]
        for s in strategies:
            emoji = "✅" if s.get("pnl", 0) >= 0 else "❌"
            lines.append(
                f"{emoji} {s.get('name', '?')}: {s.get('trades', 0)} trades | "
                f"{s.get('wins', 0)}W/{s.get('losses', 0)}L | P&L: ${s.get('pnl', 0):+.2f}"
            )
        return await self.send("\n".join(lines))
