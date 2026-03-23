"""Simulated account state for backtesting.

Ported from v1 with adaptation: positions are opened directly from
StrategySignal (no FillEvent/EventBus dependency). P&L calculation
and SL/TP logic are identical to v1.

P&L formula for Gold (XAUUSD): (close - open) * volume * 100
  → Each 0.01 point move on 0.01 lot = $0.01 profit/loss
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

from src.core.enums import CloseReason, OrderSide
from src.core.models import AccountState, Position, Trade

logger = logging.getLogger(__name__)


class BacktestAccountManager:
    """Simulated account for backtesting. Opens positions directly."""

    def __init__(
        self,
        initial_capital: float,
        tick_value: float = 0.01,
        point_size: float = 0.01,
    ) -> None:
        self._initial_capital = initial_capital
        self._balance = initial_capital
        self._tick_value = tick_value
        self._point_size = point_size
        self._positions: list[Position] = []
        self._trades: list[Trade] = []
        self._equity_snapshots: list[tuple[datetime, float]] = []
        self._next_ticket = 1

    def open_position(
        self,
        symbol: str,
        side: OrderSide,
        volume: float,
        entry_price: float,
        stop_loss: float | None,
        take_profit: float | None,
        open_time: datetime,
        comment: str = "",
    ) -> Position:
        """Open a position directly (no event bus)."""
        ticket = self._next_ticket
        self._next_ticket += 1

        position = Position(
            ticket=ticket,
            symbol=symbol,
            side=side,
            volume=volume,
            open_price=entry_price,
            open_time=open_time,
            stop_loss=stop_loss,
            take_profit=take_profit,
            current_price=entry_price,
            profit=0.0,
            commission=0.0,
            magic=300000,
        )
        self._positions.append(position)

        logger.debug(
            "BT position opened: #%d %s %s %.2f @ %.5f SL=%s TP=%s",
            ticket, side.value, symbol, volume, entry_price, stop_loss, take_profit,
        )
        return position

    def close_position(
        self, ticket: int, close_price: float, close_time: datetime,
        close_reason: str = "",
    ) -> Trade | None:
        """Close a position by ticket. Returns the completed Trade."""
        pos = None
        for p in self._positions:
            if p.ticket == ticket:
                pos = p
                break

        if pos is None:
            return None

        # Calculate realized P&L using instrument specs
        # Formula: (price_diff / point_size) * tick_value * volume
        if pos.side == OrderSide.BUY:
            price_diff = close_price - pos.open_price
        else:
            price_diff = pos.open_price - close_price
        pnl = (price_diff / self._point_size) * self._tick_value * pos.volume

        trade = Trade(
            ticket=pos.ticket,
            symbol=pos.symbol,
            side=pos.side,
            volume=pos.volume,
            open_price=pos.open_price,
            close_price=close_price,
            open_time=pos.open_time,
            close_time=close_time,
            profit=pnl,
            commission=0.0,
            swap=0.0,
            magic=pos.magic,
            comment=close_reason,
            close_reason=close_reason,
        )

        self._balance += pnl
        self._positions = [p for p in self._positions if p.ticket != ticket]
        self._trades.append(trade)

        logger.debug(
            "BT position closed: #%d reason=%s pnl=%.2f balance=%.2f",
            ticket, close_reason, pnl, self._balance,
        )
        return trade

    def check_sl_tp(
        self, symbol: str, bar_high: float, bar_low: float, bar_time: datetime
    ) -> list[Trade]:
        """Check SL/TP hits. SL checked before TP (conservative).

        Close price = the exact SL/TP level, not bar close.
        """
        closed: list[Trade] = []

        for pos in list(self._positions):
            if pos.symbol != symbol:
                continue

            close_price: float | None = None
            reason = ""

            if pos.side == OrderSide.BUY:
                if pos.stop_loss is not None and bar_low <= pos.stop_loss:
                    close_price = pos.stop_loss
                    reason = CloseReason.STOP_LOSS
                elif pos.take_profit is not None and bar_high >= pos.take_profit:
                    close_price = pos.take_profit
                    reason = CloseReason.TAKE_PROFIT
            else:
                if pos.stop_loss is not None and bar_high >= pos.stop_loss:
                    close_price = pos.stop_loss
                    reason = CloseReason.STOP_LOSS
                elif pos.take_profit is not None and bar_low <= pos.take_profit:
                    close_price = pos.take_profit
                    reason = CloseReason.TAKE_PROFIT

            if close_price is not None:
                trade = self.close_position(pos.ticket, close_price, bar_time, reason)
                if trade is not None:
                    closed.append(trade)

        return closed

    def update_prices(self, symbol: str, price: float, timestamp: datetime) -> None:
        """Update unrealized P&L and record equity snapshot."""
        for pos in self._positions:
            if pos.symbol == symbol:
                pos.current_price = price
                if pos.side == OrderSide.BUY:
                    diff = price - pos.open_price
                else:
                    diff = pos.open_price - price
                pos.profit = (diff / self._point_size) * self._tick_value * pos.volume

        equity = self._get_equity()
        self._equity_snapshots.append((timestamp, equity))

    def has_position(self, symbol: str) -> bool:
        """Check if any open position exists for this symbol."""
        return any(p.symbol == symbol for p in self._positions)

    def get_account_state(self) -> AccountState:
        equity = self._get_equity()
        unrealized = sum(p.profit for p in self._positions)
        return AccountState(
            balance=self._balance,
            equity=equity,
            margin=0.0,
            free_margin=equity,
            margin_level=0.0,
            profit=unrealized,
            timestamp=datetime.now(timezone.utc),
        )

    def get_positions(self, symbol: str | None = None) -> list[Position]:
        if symbol is None:
            return list(self._positions)
        return [p for p in self._positions if p.symbol == symbol]

    def get_trades(self) -> list[Trade]:
        return list(self._trades)

    def get_equity_curve(self) -> pd.Series:
        if not self._equity_snapshots:
            return pd.Series(dtype=float)
        timestamps, values = zip(*self._equity_snapshots)
        return pd.Series(values, index=pd.DatetimeIndex(timestamps), name="equity")

    def _get_equity(self) -> float:
        return self._balance + sum(p.profit for p in self._positions)
