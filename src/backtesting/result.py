"""Backtest result model and metrics calculation.

BacktestResult holds the output of a backtest run: trades, equity curve,
and computed performance metrics. The summary() method produces a Rich
table for CLI display.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd
from rich.console import Console
from rich.table import Table

from src.core.models import Trade


@dataclass
class BacktestResult:
    """Complete output of a backtest run."""

    strategy_name: str
    symbol: str
    start_date: datetime
    end_date: datetime
    initial_capital: float
    final_equity: float
    trades: list[Trade] = field(default_factory=list)
    equity_curve: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))

    # Computed metrics (filled by calculate_metrics)
    total_return_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    total_trades: int = 0
    avg_trade_pnl: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    max_consecutive_losses: int = 0
    max_consecutive_wins: int = 0
    avg_trade_duration_hours: float = 0.0
    best_trade_pnl: float = 0.0
    worst_trade_pnl: float = 0.0
    expectancy: float = 0.0
    monthly_returns: list[float] = field(default_factory=list)

    def summary(self) -> str:
        """Return a Rich-formatted summary table as a string."""
        console = Console(record=True, width=80)

        # Header
        console.print(f"\n[bold]Backtest Results: {self.strategy_name} on {self.symbol}[/bold]")
        console.print(f"  Period: {self.start_date:%Y-%m-%d} → {self.end_date:%Y-%m-%d}")
        console.print()

        # Metrics table
        table = Table(title="Performance Metrics")
        table.add_column("Metric", style="cyan", min_width=25)
        table.add_column("Value", justify="right", min_width=15)

        table.add_row("Initial Capital", f"${self.initial_capital:,.2f}")
        table.add_row("Final Equity", f"${self.final_equity:,.2f}")
        table.add_row("Total Return", f"{self.total_return_pct:+.2f}%")
        table.add_row("Max Drawdown", f"{self.max_drawdown_pct:.2f}%")
        table.add_row("Total Trades", str(self.total_trades))
        table.add_row("Win Rate", f"{self.win_rate:.1f}%")
        table.add_row("Profit Factor", f"{self.profit_factor:.2f}")
        table.add_row("Avg Trade P&L", f"${self.avg_trade_pnl:+.2f}")
        table.add_row("Sharpe Ratio", f"{self.sharpe_ratio:.2f}")
        table.add_row("Max Consecutive Losses", str(self.max_consecutive_losses))
        table.add_row("Avg Trade Duration", f"{self.avg_trade_duration_hours:.1f}h")

        console.print(table)
        return console.export_text()


def calculate_metrics(
    trades: list[Trade],
    equity_curve: pd.Series,
    initial_capital: float,
) -> dict:
    """Calculate backtest performance metrics from trades and equity curve.

    Returns a dict of metric names → values that can be unpacked into BacktestResult.
    """
    total_trades = len(trades)

    if total_trades == 0:
        return {
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "total_trades": 0,
            "avg_trade_pnl": 0.0,
            "sharpe_ratio": 0.0,
            "sortino_ratio": 0.0,
            "max_consecutive_losses": 0,
            "max_consecutive_wins": 0,
            "avg_trade_duration_hours": 0.0,
            "best_trade_pnl": 0.0,
            "worst_trade_pnl": 0.0,
            "expectancy": 0.0,
            "monthly_returns": [],
        }

    # Final equity from curve or trades
    final_equity = equity_curve.iloc[-1] if not equity_curve.empty else initial_capital
    total_return_pct = ((final_equity - initial_capital) / initial_capital) * 100

    # Win/loss stats
    pnls = [t.pnl for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]

    win_rate = (len(winners) / total_trades) * 100 if total_trades > 0 else 0.0

    gross_profit = sum(winners) if winners else 0.0
    gross_loss = abs(sum(losers)) if losers else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    avg_trade_pnl = sum(pnls) / total_trades

    # Max drawdown from equity curve
    max_drawdown_pct = _max_drawdown_pct(equity_curve) if not equity_curve.empty else 0.0

    # Sharpe ratio (annualized, from daily returns)
    sharpe_ratio = _sharpe_ratio(equity_curve) if not equity_curve.empty else 0.0

    # Sortino ratio (annualized, penalises downside only)
    sortino_ratio = _sortino_ratio(equity_curve) if not equity_curve.empty else 0.0

    # Consecutive streaks
    max_consecutive_losses = _max_consecutive_losses(pnls)
    max_consecutive_wins = _max_consecutive_wins(pnls)

    # Best/worst trade
    best_trade_pnl = max(pnls)
    worst_trade_pnl = min(pnls)

    # Expectancy: (win_rate * avg_win) - (loss_rate * avg_loss)
    avg_win = sum(winners) / len(winners) if winners else 0.0
    avg_loss = abs(sum(losers) / len(losers)) if losers else 0.0
    win_ratio = len(winners) / total_trades
    loss_ratio = len(losers) / total_trades
    expectancy = (win_ratio * avg_win) - (loss_ratio * avg_loss)

    # Monthly returns
    monthly_returns = _monthly_returns(trades)

    # Avg trade duration
    durations = [t.duration / 3600.0 for t in trades]  # hours
    avg_duration = sum(durations) / len(durations) if durations else 0.0

    return {
        "total_return_pct": total_return_pct,
        "max_drawdown_pct": max_drawdown_pct,
        "win_rate": win_rate,
        "profit_factor": profit_factor if not math.isinf(profit_factor) else 999.99,
        "total_trades": total_trades,
        "avg_trade_pnl": avg_trade_pnl,
        "sharpe_ratio": sharpe_ratio,
        "sortino_ratio": sortino_ratio,
        "max_consecutive_losses": max_consecutive_losses,
        "max_consecutive_wins": max_consecutive_wins,
        "avg_trade_duration_hours": avg_duration,
        "best_trade_pnl": best_trade_pnl,
        "worst_trade_pnl": worst_trade_pnl,
        "expectancy": expectancy,
        "monthly_returns": monthly_returns,
    }


def _max_drawdown_pct(equity_curve: pd.Series) -> float:
    """Calculate maximum drawdown as a percentage from equity curve."""
    if equity_curve.empty:
        return 0.0

    peak = equity_curve.expanding().max()
    drawdown = (equity_curve - peak) / peak * 100
    return abs(drawdown.min())


def _sharpe_ratio(equity_curve: pd.Series, risk_free_rate: float = 0.0) -> float:
    """Annualized Sharpe ratio from equity curve.

    Uses daily returns (resampled if sub-daily data).
    """
    if len(equity_curve) < 2:
        return 0.0

    # Calculate returns
    returns = equity_curve.pct_change().dropna()
    if returns.empty or returns.std() == 0:
        return 0.0

    # Annualize assuming ~252 trading days
    excess_returns = returns.mean() - (risk_free_rate / 252)
    annualized = excess_returns / returns.std() * np.sqrt(252)

    return float(annualized) if not (math.isnan(annualized) or math.isinf(annualized)) else 0.0


def _sortino_ratio(equity_curve: pd.Series, risk_free_rate: float = 0.0) -> float:
    """Annualized Sortino ratio from equity curve (penalises downside volatility only)."""
    if len(equity_curve) < 2:
        return 0.0

    returns = equity_curve.pct_change().dropna()
    if returns.empty:
        return 0.0

    excess_returns = returns.mean() - (risk_free_rate / 252)
    downside = returns[returns < 0]
    downside_std = downside.std() if len(downside) > 1 else 0.0

    if downside_std == 0:
        return 0.0

    annualized = excess_returns / downside_std * np.sqrt(252)
    return float(annualized) if not (math.isnan(annualized) or math.isinf(annualized)) else 0.0


def _max_consecutive_losses(pnls: list[float]) -> int:
    """Count maximum streak of consecutive losing trades."""
    max_streak = 0
    current = 0
    for pnl in pnls:
        if pnl <= 0:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0
    return max_streak


def _max_consecutive_wins(pnls: list[float]) -> int:
    """Count maximum streak of consecutive winning trades."""
    max_streak = 0
    current = 0
    for pnl in pnls:
        if pnl > 0:
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0
    return max_streak


def _monthly_returns(trades: list[Trade]) -> list[float]:
    """Calculate P&L grouped by calendar month. Returns list of monthly P&L values."""
    if not trades:
        return []

    monthly: dict[str, float] = {}
    for trade in trades:
        key = trade.close_time.strftime("%Y-%m")
        monthly[key] = monthly.get(key, 0.0) + trade.pnl

    return [monthly[k] for k in sorted(monthly)]
