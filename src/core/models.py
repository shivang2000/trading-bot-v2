"""Core domain models used throughout the trading bot."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from src.core.enums import OrderSide, OrderType, SignalAction


@dataclass(frozen=True, slots=True)
class Bar:
    """A single OHLCV bar (candlestick)."""

    symbol: str
    timeframe: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    tick_volume: int
    spread: int


@dataclass(frozen=True, slots=True)
class Tick:
    """A single market tick (bid/ask snapshot)."""

    symbol: str
    timestamp: datetime
    bid: float
    ask: float
    last: float
    volume: float


@dataclass
class Signal:
    """A trading signal from any source (Telegram, manual, etc.)."""

    source: str  # e.g. "telegram:channel_name" or "manual"
    symbol: str
    action: SignalAction
    strength: float  # 0.0 to 1.0 confidence
    timestamp: datetime
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    channel_id: Optional[str] = None
    message_id: Optional[int] = None
    metadata: dict = field(default_factory=dict)


@dataclass
class Order:
    """A trade order to be submitted to the broker."""

    symbol: str
    side: OrderSide
    order_type: OrderType
    volume: float  # lot size
    price: Optional[float] = None
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    magic: int = 200000  # v2 magic number
    comment: str = ""
    ticket: Optional[int] = None
    signal_id: Optional[int] = None  # Link back to parsed_signals table


@dataclass
class ModifyOrder:
    """A request to modify an existing position's SL/TP."""

    ticket: int
    symbol: str
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    signal_id: Optional[int] = None


@dataclass
class Position:
    """An open trading position."""

    ticket: int
    symbol: str
    side: OrderSide
    volume: float
    open_price: float
    open_time: datetime
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    current_price: float = 0.0
    profit: float = 0.0
    swap: float = 0.0
    commission: float = 0.0
    magic: int = 0
    comment: str = ""


@dataclass
class Trade:
    """A completed (closed) trade for reporting."""

    ticket: int
    symbol: str
    side: OrderSide
    volume: float
    open_price: float
    close_price: float
    open_time: datetime
    close_time: datetime
    profit: float
    commission: float = 0.0
    swap: float = 0.0
    magic: int = 0
    comment: str = ""
    close_reason: str = ""

    @property
    def pnl(self) -> float:
        """Net P&L including commission and swap."""
        return self.profit + self.commission + self.swap

    @property
    def duration(self) -> float:
        """Trade duration in seconds."""
        return (self.close_time - self.open_time).total_seconds()

    @property
    def is_winner(self) -> bool:
        return self.pnl > 0


@dataclass
class AccountState:
    """Snapshot of account state from the broker."""

    balance: float
    equity: float
    margin: float
    free_margin: float
    margin_level: float
    profit: float
    timestamp: datetime
