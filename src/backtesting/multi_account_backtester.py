"""Multi-Account Backtester -- runs each strategy on its own isolated account.

Each strategy gets allocated capital and runs independently through
ScalpingBacktestEngine (M5) or BacktestEngine (M15). Results are combined
to show total portfolio performance without shared equity interference.

This solves the problem where Dual Supertrend's 88% DD drags down
other strategies in a shared account.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pandas as pd

from src.backtesting.cost_model import CostModel
from src.backtesting.result import BacktestResult
from src.backtesting.scalping_engine import ScalpingBacktestEngine

logger = logging.getLogger(__name__)

# M15-based strategy names that route to BacktestEngine
_M15_STRATEGIES = frozenset({"ema_pullback", "london_breakout"})


@dataclass
class StrategyAccountConfig:
    """Configuration for a single strategy's isolated account."""

    name: str
    strategy_class: type
    capital: float = 100.0
    risk_pct: float = 1.0
    profit_growth: float = 0.50
    strategy_kwargs: dict = field(default_factory=dict)

    def create_strategy(self):
        """Instantiate the strategy with stored kwargs."""
        return self.strategy_class(**self.strategy_kwargs)


@dataclass
class MultiAccountResult:
    """Combined results from running multiple strategies independently."""

    per_strategy: dict[str, BacktestResult] = field(default_factory=dict)
    total_initial_capital: float = 0.0
    total_final_equity: float = 0.0
    total_return_pct: float = 0.0
    total_trades: int = 0
    combined_pf: float = 0.0

    def summary(self) -> str:
        """Return a human-readable multi-account summary."""
        lines = [
            "=" * 60,
            "MULTI-ACCOUNT BACKTEST RESULTS",
            "=" * 60,
            f"Total Capital: ${self.total_initial_capital:.2f}",
            f"Total Final Equity: ${self.total_final_equity:.2f}",
            f"Total Return: {self.total_return_pct:+.2f}%",
            f"Total Trades: {self.total_trades}",
            f"Combined PF: {self.combined_pf:.2f}",
            "",
            "Per-Strategy Breakdown:",
            "-" * 60,
        ]
        for name, result in self.per_strategy.items():
            lines.append(
                f"  {name}: ${result.initial_capital:.0f} -> "
                f"${result.final_equity:.2f} "
                f"({result.total_return_pct:+.1f}%) | "
                f"PF {result.profit_factor:.2f} | "
                f"{result.total_trades} trades | "
                f"DD {result.max_drawdown_pct:.1f}%"
            )
        lines.append("=" * 60)
        return "\n".join(lines)


class MultiAccountBacktester:
    """Runs each strategy on its own isolated account.

    M5 strategies are routed through ScalpingBacktestEngine.
    M15 strategies (ema_pullback, london_breakout) are routed through
    the original BacktestEngine.
    """

    def __init__(
        self,
        symbol: str,
        strategy_configs: list[StrategyAccountConfig],
        cost_model: CostModel | None = None,
        point_size: float = 0.01,
        tick_value: float = 0.01,
    ) -> None:
        self._symbol = symbol
        self._configs = strategy_configs
        self._cost_model = cost_model
        self._point_size = point_size
        self._tick_value = tick_value

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        m5_data: pd.DataFrame,
        h1_data: pd.DataFrame | None = None,
        m15_data: pd.DataFrame | None = None,
    ) -> MultiAccountResult:
        """Run each strategy independently on its own capital allocation.

        Args:
            m5_data: Primary M5 OHLCV bars (required for scalping strategies).
            h1_data: Optional H1 bars for regime detection.
            m15_data: Optional M15 bars for ema_pullback / london_breakout.

        Returns:
            MultiAccountResult with per-strategy and combined portfolio metrics.
        """
        results: dict[str, BacktestResult] = {}

        for config in self._configs:
            logger.info(
                "Running %s: $%.2f capital, %.1f%% risk, %.0f%% profit growth",
                config.name,
                config.capital,
                config.risk_pct,
                config.profit_growth * 100,
            )

            if config.name in _M15_STRATEGIES and m15_data is not None:
                result = self._run_m15_strategy(config, m15_data, h1_data)
            else:
                result = self._run_m5_strategy(config, m5_data, h1_data)

            results[config.name] = result
            logger.info(
                "  %s done: %d trades, %.2f%% return, PF %.2f, DD %.2f%%",
                config.name,
                result.total_trades,
                result.total_return_pct,
                result.profit_factor,
                result.max_drawdown_pct,
            )

        return self._combine(results)

    # ------------------------------------------------------------------
    # Engine dispatch
    # ------------------------------------------------------------------

    def _run_m5_strategy(
        self,
        config: StrategyAccountConfig,
        m5_data: pd.DataFrame,
        h1_data: pd.DataFrame | None,
    ) -> BacktestResult:
        """Run an M5 strategy through ScalpingBacktestEngine."""
        strategy = config.create_strategy()
        engine = ScalpingBacktestEngine(
            symbol=self._symbol,
            strategies=[strategy],
            initial_capital=config.capital,
            cost_model=self._cost_model,
            profit_growth_factor=config.profit_growth,
            point_size=self._point_size,
            tick_value=self._tick_value,
            risk_pct=config.risk_pct,
            max_daily_trades=50,
        )
        return engine.run(m5_data, h1_data)

    def _run_m15_strategy(
        self,
        config: StrategyAccountConfig,
        m15_data: pd.DataFrame,
        h1_data: pd.DataFrame | None,
    ) -> BacktestResult:
        """Run an M15 strategy through BacktestEngine."""
        from src.backtesting.engine import BacktestEngine
        from src.config.loader import load_config

        app_config = load_config()
        app_config.account.risk_per_trade_pct = config.risk_pct

        engine = BacktestEngine(
            symbol=self._symbol,
            strategy=config.name,
            initial_capital=config.capital,
            config=app_config,
            point_size=self._point_size,
        )
        return engine.run(m15_data, h1_data or pd.DataFrame())

    # ------------------------------------------------------------------
    # Result aggregation
    # ------------------------------------------------------------------

    def _combine(self, results: dict[str, BacktestResult]) -> MultiAccountResult:
        """Combine per-strategy results into a portfolio-level view."""
        if not results:
            return MultiAccountResult()

        total_initial = sum(r.initial_capital for r in results.values())
        total_final = sum(r.final_equity for r in results.values())
        total_trades = sum(r.total_trades for r in results.values())

        # Combined profit factor from all individual trades
        all_trades = []
        for r in results.values():
            all_trades.extend(r.trades)

        gross_profit = sum(t.pnl for t in all_trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in all_trades if t.pnl <= 0))
        combined_pf = gross_profit / gross_loss if gross_loss > 0 else 0.0

        total_return = (
            ((total_final - total_initial) / total_initial) * 100
            if total_initial > 0
            else 0.0
        )

        return MultiAccountResult(
            per_strategy=results,
            total_initial_capital=total_initial,
            total_final_equity=total_final,
            total_return_pct=total_return,
            total_trades=total_trades,
            combined_pf=combined_pf,
        )
