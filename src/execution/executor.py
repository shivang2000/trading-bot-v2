"""Order Executor: translates orders to MT5 requests and executes them.

Subscribes to ORDER and MODIFY_ORDER events from the EventBus.

For new orders:
1. Builds the MT5 request dict
2. Pre-validates with order_check()
3. Executes with order_send()
4. Publishes a FILL event on success

For modifications (signal amendments):
1. Sends TRADE_ACTION_SLTP to MT5
2. Logs the result

Uses AsyncMT5Client for non-blocking broker communication.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from src.core.enums import OrderSide
from src.core.events import Event, EventBus, FillEvent, ModifyOrderEvent, OrderEvent
from src.core.exceptions import OrderRejectedError
from src.core.models import ModifyOrder, Order
from src.execution.filling_modes import DEFAULT_FILLING_MODES, preferred_filling_modes
from src.mt5.client import AsyncMT5Client

logger = logging.getLogger(__name__)

# MT5 retcodes that are transient (worth retrying)
_RETRYABLE_RETCODES = {
    10004,  # TRADE_RETCODE_REQUOTE
    10006,  # TRADE_RETCODE_REJECT (temporary)
    10021,  # TRADE_RETCODE_PRICE_OFF
}


class OrderExecutor:
    """Executes orders on MT5 and publishes fill events."""

    def __init__(
        self,
        event_bus: EventBus,
        mt5_client: AsyncMT5Client,
        deviation_points: int = 20,
        max_retries: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        self._event_bus = event_bus
        self._mt5 = mt5_client
        self._deviation = deviation_points
        self._max_retries = max_retries
        self._retry_delay = retry_delay

    async def initialize(self) -> None:
        """Subscribe to ORDER and MODIFY_ORDER events."""
        self._event_bus.subscribe("ORDER", self._on_order)
        self._event_bus.subscribe("MODIFY_ORDER", self._on_modify_order)
        logger.info("OrderExecutor initialized (deviation=%d)", self._deviation)

    async def _on_order(self, event: Event) -> None:
        """Handle incoming order event with retry on transient failures."""
        if not isinstance(event, OrderEvent) or event.order is None:
            return

        order = event.order

        # Naked-trade guard: never OPEN a new position without a broker-side stop loss.
        # Closes set position_ticket and are exempt. This is the safety net that prevents a
        # disconnected/crashed bot from leaving an unmanaged, unprotected position open
        # (root cause of the +$965 gold trade that ran with SL=0).
        is_opening = order.position_ticket is None
        if is_opening and not order.stop_loss:
            logger.error(
                "REJECTED naked entry: %s %s %.2f lots has no stop loss (sl=%s) — "
                "refusing to open an unprotected position",
                order.side.value, order.symbol, order.volume, order.stop_loss,
            )
            return

        logger.info(
            "Executing: %s %s %.2f lots (magic=%d)",
            order.side.value, order.symbol, order.volume, order.magic,
        )

        last_error: Exception | None = None
        filling_modes = await self._get_filling_modes(order.symbol)

        for filling_mode in filling_modes:
            for attempt in range(1, self._max_retries + 1):
                try:
                    request = self._build_mt5_request(order, type_filling=filling_mode)

                    # Pre-flight validation
                    check = await self._mt5.order_check(request)
                    check_retcode = check.get("retcode", -1)
                    check_comment = str(check.get("comment", "Unknown"))
                    if check_retcode != 0:
                        if self._is_unsupported_filling(check_retcode, check_comment):
                            logger.warning(
                                "Unsupported filling mode %d for %s on pre-flight: %s",
                                filling_mode,
                                order.symbol,
                                check_comment,
                            )
                            break
                        raise OrderRejectedError(
                            f"Pre-flight failed: {check_comment}",
                            retcode=check_retcode,
                        )

                    # Execute
                    result = await self._mt5.order_send(request)
                    retcode = result.get("retcode", -1)
                    comment = str(result.get("comment", "Unknown"))

                    if retcode == 10009:  # TRADE_RETCODE_DONE
                        await self._publish_fill(order, result)
                        return

                    if self._is_unsupported_filling(retcode, comment):
                        logger.warning(
                            "Unsupported filling mode %d for %s on send: %s",
                            filling_mode,
                            order.symbol,
                            comment,
                        )
                        break

                    if retcode in _RETRYABLE_RETCODES and attempt < self._max_retries:
                        logger.warning(
                            "Retryable error (retcode=%d, attempt %d/%d): %s",
                            retcode,
                            attempt,
                            self._max_retries,
                            comment,
                        )
                        last_error = OrderRejectedError(
                            f"Execution failed: {comment}",
                            retcode=retcode,
                        )
                        await asyncio.sleep(self._retry_delay)
                        continue

                    raise OrderRejectedError(
                        f"Execution failed: {comment}",
                        retcode=retcode,
                    )

                except OrderRejectedError as exc:
                    if exc.retcode in _RETRYABLE_RETCODES and attempt < self._max_retries:
                        logger.warning(
                            "Retryable rejection (attempt %d/%d): %s",
                            attempt,
                            self._max_retries,
                            exc,
                        )
                        last_error = exc
                        await asyncio.sleep(self._retry_delay)
                        continue
                    logger.error("Order REJECTED: %s (retcode=%s)", exc, exc.retcode)
                    return
                except Exception:
                    logger.exception(
                        "Unexpected error executing %s %s", order.side.value, order.symbol
                    )
                    return

        if last_error:
            logger.error("Order FAILED after %d attempts: %s", self._max_retries, last_error)
        else:
            logger.error("Order FAILED: no supported filling mode for %s", order.symbol)

    async def _on_modify_order(self, event: Event) -> None:
        """Handle position modification (signal amendment SL/TP update)."""
        if not isinstance(event, ModifyOrderEvent) or event.modify_order is None:
            return

        modify = event.modify_order
        logger.info(
            "Modifying position: ticket=%d SL=%s TP=%s",
            modify.ticket, modify.stop_loss, modify.take_profit,
        )

        request: dict[str, Any] = {
            # TRADE_ACTION_SLTP = 6 (verified against the live MT5 module
            # 2026-06-10). This was 3 — an UNDEFINED action value — so every
            # SL/TP modify the bot ever sent was malformed; brokers rejected
            # it with assorted retcodes (10013/10030/10014) that were each
            # misread as broker quirks. Do not "fix" back to 3.
            "action": 6,
            "position": modify.ticket,
            "symbol": modify.symbol,  # required by MT5 for SLTP
        }
        if modify.stop_loss is not None:
            request["sl"] = modify.stop_loss
        if modify.take_profit is not None:
            request["tp"] = modify.take_profit

        # Clamp the proposed SL to the broker's minimum stop distance instead of
        # skipping. Brokers reject (retcode 10013) an SL closer to market than
        # trade_stops_level. The previous version *skipped* such ticks AND ran an
        # order_check pre-flight that, on VT Markets, returns a non-DONE code for
        # TRADE_ACTION_SLTP — so EVERY trailing modify was silently skipped and a
        # +$11 winner reverted to its entry SL (6 trail updates logged, 0 applied,
        # closed at the original stop). We now clamp-and-send and trust
        # order_send's real retcode. Trailing fires on the 30s poll, so a genuine
        # reject logs at most ~1/30s per ticket — not the old 258-line spam.
        # Fetch symbol_info once — used for both SL clamping and filling-mode choice.
        try:
            info = await self._mt5.symbol_info(modify.symbol)
        except Exception:
            info = None

        if modify.stop_loss and isinstance(info, dict):
            point = float(info.get("point") or 0.0)
            stops_level = int(info.get("trade_stops_level") or 0)
            bid = float(info.get("bid") or 0.0)
            ask = float(info.get("ask") or 0.0)
            digits = int(info.get("digits") or 5)
            if point > 0 and bid > 0 and ask > 0:
                min_dist = (stops_level + 1) * point
                mid = (bid + ask) / 2.0
                sl = float(modify.stop_loss)
                # Infer the protective side from SL vs market: an SL below
                # market protects a BUY; above market protects a SELL. If the
                # stop sits inside the min-distance band, push it just outside
                # so the broker accepts the modify rather than rejecting it.
                if sl < mid:  # BUY-side stop (below market)
                    max_sl = bid - min_dist
                    if sl > max_sl:
                        sl = max_sl
                else:         # SELL-side stop (above market)
                    min_sl = ask + min_dist
                    if sl < min_sl:
                        sl = min_sl
                request["sl"] = round(sl, digits)

        # TRADE_ACTION_SLTP per MT5 spec needs only {action, symbol, position,
        # sl, tp}. VT Markets REJECTS SLTP requests that carry type_filling with
        # retcode 10014 "Invalid volume" (observed live 2026-06-10: every trail
        # update on NZDUSD + US30 failed; profit-lock never engaged). The
        # type_filling was added for an earlier broker that answered a bare
        # SLTP with 10030 "Unsupported filling mode" — keep that as a FALLBACK:
        # send the bare spec-compliant request first, and only when the broker
        # complains about the filling mode retry with each supported mode.
        last_rc, last_comment = -1, "Unknown"
        attempts: list[dict[str, Any]] = [request] + [
            {**request, "type_filling": mode}
            for mode in preferred_filling_modes(info if isinstance(info, dict) else None)
        ]
        for req in attempts:
            try:
                result = await self._mt5.order_send(req)
            except Exception:
                logger.exception("Error modifying position ticket=%d", modify.ticket)
                return
            retcode = result.get("retcode", -1)
            comment = str(result.get("comment", "Unknown"))
            if retcode == 10009:  # TRADE_RETCODE_DONE
                logger.info(
                    "Position modified: ticket=%d SL=%s TP=%s",
                    modify.ticket, request.get("sl"), modify.take_profit,
                )
                return
            last_rc, last_comment = retcode, comment
            if not self._is_unsupported_filling(retcode, comment):
                break  # genuine error (not a filling-mode issue) — stop trying modes
        logger.warning(
            "Position modify FAILED: ticket=%d retcode=%s comment=%s",
            modify.ticket, last_rc, last_comment,
        )

    def _build_mt5_request(self, order: Order, type_filling: int = 1) -> dict[str, Any]:
        """Convert Order model to MT5 request dict."""
        mt5_type = 0 if order.side == OrderSide.BUY else 1

        request: dict[str, Any] = {
            "action": 1,              # TRADE_ACTION_DEAL
            "symbol": order.symbol,
            "volume": order.volume,
            "type": mt5_type,
            "price": 0.0,            # MT5 uses market price for DEAL action
            "deviation": self._deviation,
            "magic": order.magic,
            "comment": order.comment[:31],  # MT5 limit
            "type_time": 0,           # ORDER_TIME_GTC
            "type_filling": type_filling,
        }

        if order.stop_loss is not None:
            request["sl"] = order.stop_loss
        if order.take_profit is not None:
            request["tp"] = order.take_profit
        # For partial closes: link to existing position
        if order.position_ticket is not None:
            request["position"] = order.position_ticket

        return request

    async def _get_filling_modes(self, symbol: str) -> list[int]:
        """Resolve candidate filling modes from symbol info with safe defaults."""
        try:
            info = await self._mt5.symbol_info(symbol)
            if isinstance(info, dict):
                return preferred_filling_modes(info)
        except Exception:
            logger.debug(
                "Failed to fetch symbol_info for %s, using defaults",
                symbol,
                exc_info=True,
            )
        return list(DEFAULT_FILLING_MODES)

    @staticmethod
    def _is_unsupported_filling(retcode: int, comment: str) -> bool:
        return retcode == 10030 or "unsupported filling mode" in comment.lower()

    async def _publish_fill(self, order: Order, result: dict[str, Any]) -> None:
        """Publish FillEvent after successful execution."""
        fill_price = result.get("price", 0.0)
        fill_volume = result.get("volume", order.volume)
        ticket = result.get("order", 0)

        order.ticket = ticket

        fill = FillEvent(
            timestamp=datetime.now(timezone.utc),
            order=order,
            fill_price=fill_price,
            fill_volume=fill_volume,
            commission=0.0,
            slippage=0.0,
        )
        await self._event_bus.publish(fill)

        logger.info(
            "FILLED: ticket=%d %s %s %.2f lots @ %.2f",
            ticket, order.side.value, order.symbol, fill_volume, fill_price,
        )
