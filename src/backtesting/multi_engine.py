"""Multi-instrument backtester — shared account across all instruments.

Simulates real trading: all instruments share the same equity, margin,
and position limits. A Gold trade uses margin that blocks a BTC trade.

Approach: Run single-instrument backtests first, collect all trade events,
then simulate shared-account execution walking through the combined timeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

import pandas as pd

from src.backtesting.result import BacktestResult, calculate_metrics
from src.core.enums import OrderSide

logger = logging.getLogger(__name__)


@dataclass
class TradeEvent:
    """A trade open or close event from a single-instrument backtest."""

    timestamp: datetime
    event_type: str  # "open" or "close"
    ticket: int
    symbol: str
    side: OrderSide
    volume: float
    price: float
    stop_loss: float | None = None
    take_profit: float | None = None
    pnl: float = 0.0  # only for close events
    close_reason: str = ""
    strategy: str = ""


@dataclass
class OpenPosition:
    """A position currently open in the shared account."""

    ticket: int
    symbol: str
    side: OrderSide
    volume: float
    open_price: float
    open_time: datetime
    stop_loss: float | None = None
    take_profit: float | None = None
    margin_used: float = 0.0


@dataclass
class MultiBacktestResult:
    """Results from multi-instrument shared-account backtest."""

    initial_capital: float
    final_equity: float
    total_return_pct: float
    max_drawdown_pct: float
    total_trades: int
    blocked_trades: int
    win_rate: float
    profit_factor: float
    avg_trade_pnl: float
    sharpe_ratio: float
    max_consecutive_losses: int
    trades_by_instrument: dict[str, int] = field(default_factory=dict)
    trades_by_strategy: dict[str, int] = field(default_factory=dict)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))

    def summary(self) -> str:
        lines = [
            f"\nMulti-Instrument Backtest Results",
            f"  Initial: ${self.initial_capital:.2f} → Final: ${self.final_equity:.2f}",
            f"",
            f"  Return:           {self.total_return_pct:+.2f}%",
            f"  Max Drawdown:     {self.max_drawdown_pct:.2f}%",
            f"  Total Trades:     {self.total_trades} ({self.blocked_trades} blocked)",
            f"  Win Rate:         {self.win_rate:.1f}%",
            f"  Profit Factor:    {self.profit_factor:.2f}",
            f"  Avg Trade P&L:    ${self.avg_trade_pnl:.2f}",
            f"  Sharpe:           {self.sharpe_ratio:.2f}",
            f"  Max Consec Loss:  {self.max_consecutive_losses}",
            f"",
            f"  By Instrument: {self.trades_by_instrument}",
            f"  By Strategy:   {self.trades_by_strategy}",
        ]
        return "\n".join(lines)


# Margin requirements per 0.01 lot at 1:500 leverage (approximate)
MARGIN_PER_LOT = {
    "XAUUSD": 850.0,   # 0.01 lot ≈ $8.50
    "XAGUSD": 60.0,    # 0.01 lot ≈ $0.60
    "BTCUSD": 140.0,   # 0.01 lot ≈ $1.40
    "ETHUSD": 4.0,     # 0.01 lot ≈ $0.04
}


def run_multi_instrument(
    trade_results: dict[str, list[dict]],
    initial_capital: float = 30.0,
    max_open_positions: int = 2,
    max_per_symbol: int = 1,
    tick_values: dict[str, float] | None = None,
    point_sizes: dict[str, float] | None = None,
) -> MultiBacktestResult:
    """Run a shared-account simulation from per-instrument backtest results.

    Args:
        trade_results: dict of {symbol: [list of trade dicts from backtest JSON]}
        initial_capital: starting balance
        max_open_positions: global position limit
        max_per_symbol: per-symbol position limit
        tick_values: {symbol: tick_value} for P&L calculation
        point_sizes: {symbol: point_size} for P&L calculation

    Returns:
        MultiBacktestResult with combined metrics
    """
    tick_values = tick_values or {"XAUUSD": 1.0, "XAGUSD": 5.0, "BTCUSD": 0.01, "ETHUSD": 0.01}
    point_sizes = point_sizes or {"XAUUSD": 0.01, "XAGUSD": 0.001, "BTCUSD": 0.01, "ETHUSD": 0.01}

    # Collect all trade events sorted by time
    all_events: list[TradeEvent] = []

    for symbol, trades in trade_results.items():
        for t in trades:
            side = OrderSide.BUY if t["side"] == "BUY" else OrderSide.SELL

            # Open event
            all_events.append(TradeEvent(
                timestamp=pd.Timestamp(t["open_time"]).to_pydatetime(),
                event_type="open",
                ticket=t["ticket"],
                symbol=symbol,
                side=side,
                volume=0.01,  # will recalculate with shared equity
                price=t["open_price"],
                stop_loss=t.get("stop_loss"),
                take_profit=t.get("take_profit"),
                strategy=t.get("comment", ""),
            ))

            # Close event
            all_events.append(TradeEvent(
                timestamp=pd.Timestamp(t["close_time"]).to_pydatetime(),
                event_type="close",
                ticket=t["ticket"],
                symbol=symbol,
                side=side,
                volume=0.01,
                price=t["close_price"],
                pnl=t["pnl"],
                close_reason=t.get("close_reason", ""),
            ))

    # Sort by timestamp
    all_events.sort(key=lambda e: e.timestamp)

    # Simulate shared account
    balance = initial_capital
    equity_snapshots: list[tuple[datetime, float]] = []
    open_positions: dict[int, OpenPosition] = {}
    completed_pnls: list[float] = []
    blocked_count = 0
    trades_by_instrument: dict[str, int] = {}
    trades_by_strategy: dict[str, int] = {}

    for event in all_events:
        if event.event_type == "open":
            # Check position limits
            if len(open_positions) >= max_open_positions:
                blocked_count += 1
                continue

            symbol_count = sum(
                1 for p in open_positions.values() if p.symbol == event.symbol
            )
            if symbol_count >= max_per_symbol:
                blocked_count += 1
                continue

            # Check margin
            margin_req = MARGIN_PER_LOT.get(event.symbol, 10.0) * event.volume / 0.01
            used_margin = sum(p.margin_used for p in open_positions.values())
            free_margin = balance - used_margin
            if free_margin < margin_req:
                blocked_count += 1
                continue

            # Open position
            open_positions[event.ticket] = OpenPosition(
                ticket=event.ticket,
                symbol=event.symbol,
                side=event.side,
                volume=event.volume,
                open_price=event.price,
                open_time=event.timestamp,
                stop_loss=event.stop_loss,
                take_profit=event.take_profit,
                margin_used=margin_req,
            )

            trades_by_instrument[event.symbol] = trades_by_instrument.get(event.symbol, 0) + 1
            strategy = event.strategy.split(":")[-1] if ":" in event.strategy else event.strategy
            if strategy:
                trades_by_strategy[strategy] = trades_by_strategy.get(strategy, 0) + 1

        elif event.event_type == "close":
            pos = open_positions.pop(event.ticket, None)
            if pos is None:
                continue  # was blocked at open time

            # Calculate P&L with shared account
            tv = tick_values.get(pos.symbol, 1.0)
            ps = point_sizes.get(pos.symbol, 0.01)
            if pos.side == OrderSide.BUY:
                diff = event.price - pos.open_price
            else:
                diff = pos.open_price - event.price
            pnl = (diff / ps) * tv * pos.volume

            balance += pnl
            completed_pnls.append(pnl)

        equity_snapshots.append((event.timestamp, balance))

    # Calculate metrics
    if not equity_snapshots:
        return MultiBacktestResult(
            initial_capital=initial_capital, final_equity=initial_capital,
            total_return_pct=0, max_drawdown_pct=0, total_trades=0,
            blocked_trades=0, win_rate=0, profit_factor=0, avg_trade_pnl=0,
            sharpe_ratio=0, max_consecutive_losses=0,
        )

    equity_series = pd.Series(
        [v for _, v in equity_snapshots],
        index=pd.DatetimeIndex([t for t, _ in equity_snapshots]),
    )

    total_trades = len(completed_pnls)
    wins = sum(1 for p in completed_pnls if p > 0)
    losses = sum(1 for p in completed_pnls if p <= 0)
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

    gross_profit = sum(p for p in completed_pnls if p > 0)
    gross_loss = abs(sum(p for p in completed_pnls if p <= 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 999.99

    avg_pnl = sum(completed_pnls) / total_trades if total_trades > 0 else 0

    # Max drawdown
    peak = equity_series.expanding().max()
    dd = (equity_series - peak) / peak * 100
    max_dd = abs(dd.min()) if len(dd) > 0 else 0

    # Sharpe
    returns = equity_series.pct_change().dropna()
    sharpe = (returns.mean() / returns.std() * (252 ** 0.5)) if len(returns) > 1 and returns.std() > 0 else 0

    # Max consecutive losses
    max_consec = 0
    current_streak = 0
    for p in completed_pnls:
        if p <= 0:
            current_streak += 1
            max_consec = max(max_consec, current_streak)
        else:
            current_streak = 0

    return MultiBacktestResult(
        initial_capital=initial_capital,
        final_equity=balance,
        total_return_pct=((balance - initial_capital) / initial_capital) * 100,
        max_drawdown_pct=max_dd,
        total_trades=total_trades,
        blocked_trades=blocked_count,
        win_rate=win_rate,
        profit_factor=profit_factor,
        avg_trade_pnl=avg_pnl,
        sharpe_ratio=sharpe,
        max_consecutive_losses=max_consec,
        trades_by_instrument=trades_by_instrument,
        trades_by_strategy=trades_by_strategy,
        equity_curve=equity_series,
    )
