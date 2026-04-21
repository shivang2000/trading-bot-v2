"""Enhanced Slack Logger — Event-driven trade intelligence.

Subscribes to EventBus events and sends rich Slack notifications for:
- Signal scan results (which strategies fired, which were filtered)
- Signal rejections with reasons (R:R too low, daily limit, etc.)
- Regime changes (trending → ranging, etc.)
- Periodic position P&L snapshots
- Error/disconnect alerts

Complements the existing SlackNotifier (which handles trade open/close)
by adding visibility into the bot's decision-making process.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from src.core.events import Event, EventBus
from src.monitoring.slack import SlackNotifier

logger = logging.getLogger(__name__)


class SlackLogger:
    """Event-driven Slack logger for trade intelligence."""

    def __init__(
        self,
        slack: SlackNotifier,
        event_bus: EventBus,
        position_update_interval: int = 300,
        log_scans: bool = False,
        log_rejections: bool = True,
        log_regime_changes: bool = True,
        log_errors: bool = True,
    ) -> None:
        self._slack = slack
        self._event_bus = event_bus
        self._position_interval = position_update_interval
        self._log_scans = log_scans
        self._log_rejections = log_rejections
        self._log_regime_changes = log_regime_changes
        self._log_errors = log_errors

        self._last_regime: dict[str, str] = {}
        self._rejection_count: int = 0
        self._scan_count: int = 0
        self._running = False
        self._task: asyncio.Task | None = None

        # Batch rejections to avoid spam (send summary every 5 min)
        self._rejection_buffer: list[dict] = []

    async def start(self) -> None:
        """Start the logger and subscribe to events."""
        if not self._slack.enabled:
            logger.info("SlackLogger: Slack disabled, skipping")
            return

        self._running = True
        self._task = asyncio.create_task(self._periodic_loop())
        logger.info("SlackLogger started (rejections=%s, regime=%s, errors=%s)",
                     self._log_rejections, self._log_regime_changes, self._log_errors)

    async def stop(self) -> None:
        """Stop the logger."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        # Flush remaining rejections
        if self._rejection_buffer:
            await self._flush_rejections()

    async def _periodic_loop(self) -> None:
        """Background loop for periodic updates."""
        while self._running:
            await asyncio.sleep(self._position_interval)
            try:
                # Flush batched rejections
                if self._rejection_buffer:
                    await self._flush_rejections()
            except Exception:
                logger.debug("SlackLogger periodic error", exc_info=True)

    # ── Public methods called by other components ──

    async def log_signal_generated(
        self,
        symbol: str,
        strategy: str,
        direction: str,
        entry: float,
        sl: float,
        tp: float,
        confidence: float = 0.0,
        reason: str = "",
    ) -> None:
        """Log when a strategy generates a signal (before risk check)."""
        if not self._log_scans or not self._slack.enabled:
            return

        self._scan_count += 1
        rr = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0

        msg = (
            f":signal_strength: *Signal Generated*\n"
            f"{symbol} {direction} by `{strategy}`\n"
            f"Entry: {entry:.2f} | SL: {sl:.2f} | TP: {tp:.2f}\n"
            f"R:R 1:{rr:.1f} | Confidence: {confidence:.0%}\n"
            f"Reason: {reason}"
        )
        await self._slack.send(msg)

    async def log_signal_rejected(
        self,
        symbol: str,
        source: str,
        reason: str,
        limit_name: str = "",
        limit_value: float = 0.0,
        limit_max: float = 0.0,
    ) -> None:
        """Log when risk manager rejects a signal. Batched to avoid spam."""
        if not self._log_rejections or not self._slack.enabled:
            return

        self._rejection_count += 1
        self._rejection_buffer.append({
            "symbol": symbol,
            "source": source,
            "reason": reason,
            "limit": limit_name,
            "value": limit_value,
            "max": limit_max,
            "time": datetime.now(timezone.utc).strftime("%H:%M"),
        })

        # Flush if buffer gets large
        if len(self._rejection_buffer) >= 10:
            await self._flush_rejections()

    async def log_regime_change(
        self,
        symbol: str,
        old_regime: str,
        new_regime: str,
    ) -> None:
        """Log when market regime changes for a symbol."""
        if not self._log_regime_changes or not self._slack.enabled:
            return

        # Only log actual changes
        prev = self._last_regime.get(symbol)
        if prev == new_regime:
            return
        self._last_regime[symbol] = new_regime

        if prev is None:
            return  # Skip first detection (no change)

        emoji_map = {
            "trending_up": ":chart_with_upwards_trend:",
            "trending_down": ":chart_with_downwards_trend:",
            "ranging": ":left_right_arrow:",
            "choppy": ":ocean:",
            "volatile_trend": ":zap:",
        }
        emoji = emoji_map.get(new_regime, ":arrows_counterclockwise:")

        msg = (
            f"{emoji} *Regime Change: {symbol}*\n"
            f"{old_regime} → *{new_regime}*"
        )
        await self._slack.send(msg)

    async def log_error(self, component: str, error: str) -> None:
        """Log errors/disconnections."""
        if not self._log_errors or not self._slack.enabled:
            return

        await self._slack.send_error_alert(f"`{component}`: {error}")

    async def log_partial_close(
        self,
        symbol: str,
        ticket: int,
        level: int,
        volume: float,
        new_sl: float,
    ) -> None:
        """Log partial profit close."""
        if not self._slack.enabled:
            return

        msg = (
            f":scissors: *Partial Close*\n"
            f"#{ticket} {symbol} TP{level} hit\n"
            f"Closed {volume:.2f} lots | SL → {new_sl:.2f}"
        )
        await self._slack.send(msg)

    # ── Private helpers ──

    async def _flush_rejections(self) -> None:
        """Send batched rejection summary."""
        if not self._rejection_buffer:
            return

        count = len(self._rejection_buffer)
        # Group by reason
        reasons: dict[str, int] = {}
        for r in self._rejection_buffer:
            key = r.get("limit") or r.get("reason", "unknown")
            reasons[key] = reasons.get(key, 0) + 1

        lines = [f":no_entry: *{count} Signals Rejected*"]
        for reason, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
            lines.append(f"  • {reason}: {cnt}x")

        # Show last few details
        for r in self._rejection_buffer[-3:]:
            lines.append(
                f"  `{r['time']}` {r['symbol']} from {r['source']}: {r['reason']}"
            )

        await self._slack.send("\n".join(lines))
        self._rejection_buffer.clear()
