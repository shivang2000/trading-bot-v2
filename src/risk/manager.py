"""Risk Manager: the gatekeeper between signals and orders.

Subscribes to SIGNAL and SIGNAL_AMENDMENT events. For new signals:
1. Syncs position state from MT5
2. Validates against risk limits (no performance gate — that caused deadlock in v1)
3. Calculates position size using fixed risk %
4. Publishes an ORDER event

For amendments:
1. Finds the open position to modify
2. Publishes a MODIFY_ORDER event
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable

from src.config.schema import AppConfig
from src.core.enums import OrderSide, OrderType, SignalAction
from src.core.events import (
    Event,
    EventBus,
    ModifyOrderEvent,
    OrderEvent,
    SignalAmendmentEvent,
    SignalEvent,
)
from src.core.exceptions import RiskLimitExceeded
from src.core.models import AccountState, Order, Position, Signal
from src.risk.position_sizer import PositionSizer

logger = logging.getLogger(__name__)

# Maps close actions to the order side needed to close the position
_CLOSE_ACTION_SIDE = {
    SignalAction.CLOSE_BUY: OrderSide.SELL,
    SignalAction.CLOSE_SELL: OrderSide.BUY,
}


class RiskManager:
    """Validates signals against risk rules and publishes orders."""

    def __init__(
        self,
        config: AppConfig,
        event_bus: EventBus,
        symbol_info_func: Callable[[str], Any],
        account_state_func: Callable[[], AccountState],
        positions_func: Callable[[str | None], list[Position]],
    ) -> None:
        self._config = config
        self._event_bus = event_bus
        self._symbol_info_func = symbol_info_func
        self._account_state_func = account_state_func
        self._positions_func = positions_func
        self._sizer = PositionSizer(config.account)

        # Track daily state
        self._daily_trade_count = 0
        self._session_start_equity = config.account.initial_balance
        self._peak_equity = config.account.initial_balance

    async def initialize(self) -> None:
        """Subscribe to events and sync initial state."""
        self._event_bus.subscribe("SIGNAL", self._on_signal)
        self._event_bus.subscribe("SIGNAL_AMENDMENT", self._on_amendment)

        # Get actual account state
        try:
            account = self._account_state_func()
            self._session_start_equity = account.equity
            self._peak_equity = account.equity
        except Exception:
            logger.warning("Could not sync initial account state")

        logger.info("RiskManager initialized")

    def set_daily_trade_count(self, count: int) -> None:
        """Restore daily trade count from persisted state."""
        self._daily_trade_count = count
        if count > 0:
            logger.info("Restored daily trade count: %d", count)

    async def _on_signal(self, event: Event) -> None:
        """Handle incoming signal event."""
        if not isinstance(event, SignalEvent) or event.signal is None:
            return

        signal = event.signal
        logger.info(
            "Processing signal: %s %s %s strength=%.2f",
            signal.source, signal.action.value, signal.symbol, signal.strength,
        )

        try:
            # Close signals skip risk checks
            is_close = signal.action in (
                SignalAction.CLOSE_BUY, SignalAction.CLOSE_SELL, SignalAction.CLOSE_ALL
            )

            if not is_close:
                # Confidence gate
                min_conf = self._config.signal_parser.min_confidence
                if signal.strength < min_conf:
                    logger.warning(
                        "Signal REJECTED [%s]: confidence %.2f < %.2f threshold",
                        signal.source, signal.strength, min_conf,
                    )
                    return

                self._validate_risk_limits(signal)

            if signal.action == SignalAction.CLOSE_ALL:
                await self._close_all_positions(signal)
                return

            order = self._signal_to_order(signal)
            await self._event_bus.publish(
                OrderEvent(timestamp=signal.timestamp, order=order)
            )

            self._daily_trade_count += 1
            logger.info(
                "Signal approved → Order: %s %s %.2f lots sl=%s tp=%s",
                order.side.value, order.symbol, order.volume,
                order.stop_loss, order.take_profit,
            )

        except RiskLimitExceeded as e:
            logger.warning("Signal REJECTED [%s]: %s", signal.source, e)
        except Exception:
            logger.exception("Error processing signal from %s", signal.source)

    async def _on_amendment(self, event: Event) -> None:
        """Handle signal amendment (SL/TP update)."""
        if not isinstance(event, SignalAmendmentEvent) or event.modify_order is None:
            return

        modify = event.modify_order
        logger.info(
            "Processing amendment: ticket=%d SL=%s TP=%s",
            modify.ticket,
            modify.stop_loss,
            modify.take_profit,
        )

        await self._event_bus.publish(
            ModifyOrderEvent(
                timestamp=datetime.now(timezone.utc),
                modify_order=modify,
            )
        )

    def _validate_risk_limits(self, signal: Signal) -> None:
        """Check all risk limits. Raises RiskLimitExceeded on failure."""
        risk = self._config.risk

        # 1. Max open positions
        positions = self._positions_func(None)
        open_count = len(positions)
        if open_count >= risk.max_open_positions:
            raise RiskLimitExceeded(
                "max_open_positions", open_count, risk.max_open_positions
            )

        # 2. Max positions per symbol (strategy-aware for scalping)
        symbol_positions = [p for p in positions if p.symbol == signal.symbol]

        # Get incoming strategy name from signal
        incoming_strategy = ""
        if hasattr(signal, 'metadata') and signal.metadata:
            incoming_strategy = signal.metadata.get("strategy", "")

        # Count positions from THIS strategy only
        if incoming_strategy:
            strategy_positions = [p for p in symbol_positions if p.comment.startswith(incoming_strategy + ":")]
            if len(strategy_positions) >= 1:  # max 1 per strategy
                raise RiskLimitExceeded("strategy_position", len(strategy_positions), 1)
        else:
            # Fallback: original behavior for non-scalping signals
            if len(symbol_positions) >= risk.max_positions_per_symbol:
                raise RiskLimitExceeded(
                    "max_positions_per_symbol",
                    len(symbol_positions),
                    risk.max_positions_per_symbol,
                )

        # 3. Daily trade limit
        if self._daily_trade_count >= risk.max_daily_trades:
            raise RiskLimitExceeded(
                "max_daily_trades", self._daily_trade_count, risk.max_daily_trades
            )

        # 4. Daily loss limit
        account = self._account_state_func()
        daily_pnl_pct = (
            (account.equity - self._session_start_equity) / self._session_start_equity * 100
        )
        if daily_pnl_pct < -risk.max_daily_loss_pct:
            raise RiskLimitExceeded(
                "max_daily_loss_pct", abs(daily_pnl_pct), risk.max_daily_loss_pct
            )

        # 5. Drawdown limit
        if account.equity > self._peak_equity:
            self._peak_equity = account.equity
        dd_pct = (self._peak_equity - account.equity) / self._peak_equity * 100
        if dd_pct > risk.max_drawdown_pct:
            raise RiskLimitExceeded(
                "max_drawdown_pct", dd_pct, risk.max_drawdown_pct
            )

        # 6. Free margin check (prevent "No money" errors)
        if account.free_margin < account.equity * 0.2:
            raise RiskLimitExceeded(
                "free_margin", account.free_margin, account.equity * 0.2
            )

        # 7. Spread check (prevent bad entries during high spread)
        symbol_info = self._symbol_info_func(signal.symbol)
        if symbol_info:
            spread = symbol_info.get("spread", 0)
            max_spread = symbol_info.get("max_spread", 100)
            if spread > max_spread:
                raise RiskLimitExceeded("spread", spread, max_spread)

    def _signal_to_order(self, signal: Signal) -> Order:
        """Convert an approved signal to an executable order."""
        symbol_info = self._symbol_info_func(signal.symbol)
        account_state = self._account_state_func()

        is_close = signal.action in (SignalAction.CLOSE_BUY, SignalAction.CLOSE_SELL)

        if is_close:
            side = _CLOSE_ACTION_SIDE[signal.action]
            positions = self._positions_func(signal.symbol)
            volume = positions[0].volume if positions else 0.01
        else:
            side = OrderSide.BUY if signal.action == SignalAction.BUY else OrderSide.SELL
            volume = self._sizer.calculate(signal, account_state, symbol_info)

        signal_id = signal.metadata.get("signal_id") if signal.metadata else None

        return Order(
            symbol=signal.symbol,
            side=side,
            order_type=OrderType.MARKET,
            volume=volume,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            magic=200000,
            comment=f"tg:{signal.source[:20]} {signal.action.value}",
            signal_id=signal_id,
        )

    async def _close_all_positions(self, signal: Signal) -> None:
        """Generate a close order for every open position of the signal's symbol."""
        positions = self._positions_func(signal.symbol)
        if not positions:
            logger.info("CLOSE_ALL: no open positions for %s", signal.symbol)
            return

        for pos in positions:
            close_side = OrderSide.SELL if pos.side == OrderSide.BUY else OrderSide.BUY
            order = Order(
                symbol=signal.symbol,
                side=close_side,
                order_type=OrderType.MARKET,
                volume=pos.volume,
                magic=200000,
                comment=f"tg:{signal.source[:20]} CLOSE_ALL",
            )
            await self._event_bus.publish(
                OrderEvent(timestamp=signal.timestamp, order=order)
            )
            logger.info(
                "CLOSE_ALL → Order: %s %s %.2f lots (ticket %d)",
                close_side.value, signal.symbol, pos.volume, pos.ticket,
            )
