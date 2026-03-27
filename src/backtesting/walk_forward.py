"""Walk-Forward Optimization and Monte Carlo Validation.

WalkForwardOptimizer: n-window walk-forward with 70/30 IS/OOS split.
MonteCarloValidator: 1000-iteration trade reshuffling for robustness.

WFO restricted to M5 strategies (369+ days). M1 (75 days) uses Monte Carlo only.
"""

from __future__ import annotations

import itertools
import logging
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from src.backtesting.result import BacktestResult, calculate_metrics
from src.backtesting.scalping_engine import ScalpingBacktestEngine
from src.backtesting.cost_model import CostModel
from src.core.models import Trade

logger = logging.getLogger(__name__)


@dataclass
class WindowResult:
    """Result for a single walk-forward window."""

    window_idx: int
    is_metric: float
    oos_metric: float
    wfe: float
    best_params: dict
    is_trades: int
    oos_trades: int


@dataclass
class WalkForwardResult:
    """Complete walk-forward optimization result."""

    windows: list[WindowResult] = field(default_factory=list)
    overall_wfe: float = 0.0
    best_params: dict = field(default_factory=dict)
    is_robust: bool = False

    def summary(self) -> str:
        lines = [
            f"Walk-Forward Optimization: {len(self.windows)} windows",
            f"Overall WFE: {self.overall_wfe:.3f} ({'PASS' if self.overall_wfe > 0.5 else 'FAIL'})",
            f"Best params: {self.best_params}",
            f"Robust: {self.is_robust}",
        ]
        for w in self.windows:
            lines.append(
                f"  Window {w.window_idx}: IS={w.is_metric:.3f} "
                f"OOS={w.oos_metric:.3f} WFE={w.wfe:.3f} "
                f"trades={w.oos_trades}"
            )
        return "\n".join(lines)


@dataclass
class MonteCarloResult:
    """Monte Carlo simulation result."""

    n_simulations: int
    original_final_equity: float
    p5_final_equity: float
    p50_final_equity: float
    p95_final_equity: float
    p5_max_drawdown: float
    p95_max_drawdown: float
    ruin_probability: float
    is_fragile: bool

    def summary(self) -> str:
        lines = [
            f"Monte Carlo: {self.n_simulations} simulations",
            f"Original equity: ${self.original_final_equity:.2f}",
            f"P5/P50/P95 equity: ${self.p5_final_equity:.2f} / "
            f"${self.p50_final_equity:.2f} / ${self.p95_final_equity:.2f}",
            f"P5/P95 max DD: {self.p5_max_drawdown:.1f}% / {self.p95_max_drawdown:.1f}%",
            f"Ruin probability: {self.ruin_probability:.1f}%",
            f"Fragile: {self.is_fragile}",
        ]
        return "\n".join(lines)


class WalkForwardOptimizer:
    """Walk-forward optimization with IS/OOS splits.

    Divides data into n overlapping windows. For each window, grid-searches
    parameters on the in-sample portion, then evaluates on out-of-sample.
    Walk-Forward Efficiency (WFE) = OOS metric / IS metric.
    WFE > 0.5 indicates the strategy generalizes and is not overfit.
    """

    def __init__(
        self,
        strategy_class,
        param_grid: dict[str, list],
        engine_kwargs: dict | None = None,
        is_ratio: float = 0.7,
        n_windows: int = 5,
        optimization_metric: str = "expectancy",
    ) -> None:
        self._strategy_class = strategy_class
        self._param_grid = param_grid
        self._engine_kwargs = engine_kwargs or {}
        self._is_ratio = is_ratio
        self._n_windows = n_windows
        self._metric = optimization_metric

    def run(
        self,
        primary_data: pd.DataFrame,
        h1_data: pd.DataFrame | None = None,
    ) -> WalkForwardResult:
        """Run walk-forward optimization across n windows."""
        total_bars = len(primary_data)
        window_size = total_bars // self._n_windows

        if window_size < 500:
            logger.warning(
                "Window size %d bars is very small. Consider fewer windows.", window_size
            )

        results: list[WindowResult] = []
        param_votes: dict[str, list] = {k: [] for k in self._param_grid}

        for w in range(self._n_windows):
            start_idx = w * window_size
            end_idx = min(start_idx + window_size, total_bars)
            window_data = primary_data.iloc[start_idx:end_idx].reset_index(drop=True)

            split_idx = int(len(window_data) * self._is_ratio)
            is_data = window_data.iloc[:split_idx].reset_index(drop=True)
            oos_data = window_data.iloc[split_idx:].reset_index(drop=True)

            best_params, best_is_metric, is_trades = self._grid_search(is_data, h1_data)
            oos_metric, oos_trades = self._evaluate(oos_data, h1_data, best_params)

            wfe = oos_metric / best_is_metric if best_is_metric > 0 else 0.0

            results.append(
                WindowResult(
                    window_idx=w,
                    is_metric=best_is_metric,
                    oos_metric=oos_metric,
                    wfe=wfe,
                    best_params=best_params,
                    is_trades=is_trades,
                    oos_trades=oos_trades,
                )
            )

            for k, v in best_params.items():
                param_votes[k].append(v)

            logger.info(
                "Window %d/%d: IS=%.3f OOS=%.3f WFE=%.3f (IS trades=%d, OOS trades=%d)",
                w + 1, self._n_windows, best_is_metric, oos_metric, wfe, is_trades, oos_trades,
            )

        overall_wfe = float(np.mean([w.wfe for w in results])) if results else 0.0

        best_params = {}
        for k, votes in param_votes.items():
            if votes:
                best_params[k] = Counter(votes).most_common(1)[0][0]

        return WalkForwardResult(
            windows=results,
            overall_wfe=overall_wfe,
            best_params=best_params,
            is_robust=overall_wfe > 0.5 and all(w.oos_trades >= 10 for w in results),
        )

    def _grid_search(
        self, data: pd.DataFrame, h1_data: pd.DataFrame | None
    ) -> tuple[dict, float, int]:
        """Grid search over param_grid. Returns (best_params, best_metric, trade_count)."""
        keys = list(self._param_grid.keys())
        values = list(self._param_grid.values())

        best_metric = -float("inf")
        best_params = {k: v[0] for k, v in self._param_grid.items()}
        best_trades = 0

        combos = list(itertools.product(*values))
        logger.info("  Grid search: %d combinations", len(combos))

        for combo in combos:
            params = dict(zip(keys, combo))
            metric, trades = self._evaluate(data, h1_data, params)
            if metric > best_metric and trades >= 5:
                best_metric = metric
                best_params = params
                best_trades = trades

        return best_params, best_metric, best_trades

    def _evaluate(
        self, data: pd.DataFrame, h1_data: pd.DataFrame | None, params: dict
    ) -> tuple[float, int]:
        """Run backtest with given params. Returns (metric_value, trade_count)."""
        try:
            strategy = self._strategy_class(**params)
            engine = ScalpingBacktestEngine(
                strategies=[strategy], **self._engine_kwargs
            )
            result = engine.run(data, h1_data)
            metric_val = getattr(result, self._metric, 0.0)
            if metric_val is None or np.isnan(metric_val):
                metric_val = 0.0
            return float(metric_val), result.total_trades
        except Exception as e:
            logger.warning("Evaluation failed for %s: %s", params, e)
            return 0.0, 0


class MonteCarloValidator:
    """Monte Carlo validation via trade reshuffling.

    Shuffles trade order 1000 times to assess whether the equity curve
    and drawdown are robust to sequence risk. A strategy whose 5th
    percentile max drawdown exceeds 15% is flagged as fragile.
    """

    def __init__(
        self,
        trades: list,
        initial_capital: float = 10_000.0,
        n_simulations: int = 1000,
    ) -> None:
        self._trades = trades
        self._initial_capital = initial_capital
        self._n_sims = n_simulations

    def run(self) -> MonteCarloResult:
        """Run Monte Carlo simulations by reshuffling trade order."""
        if not self._trades:
            return MonteCarloResult(
                n_simulations=0,
                original_final_equity=self._initial_capital,
                p5_final_equity=self._initial_capital,
                p50_final_equity=self._initial_capital,
                p95_final_equity=self._initial_capital,
                p5_max_drawdown=0.0,
                p95_max_drawdown=0.0,
                ruin_probability=0.0,
                is_fragile=False,
            )

        pnls = [getattr(t, "pnl", 0.0) for t in self._trades]
        original_equity = self._initial_capital + sum(pnls)

        final_equities = []
        max_drawdowns = []
        ruin_count = 0

        for _ in range(self._n_sims):
            shuffled = pnls.copy()
            random.shuffle(shuffled)

            equity = self._initial_capital
            peak = equity
            max_dd = 0.0

            for pnl in shuffled:
                equity += pnl
                if equity > peak:
                    peak = equity
                dd_pct = ((peak - equity) / peak) * 100 if peak > 0 else 0.0
                max_dd = max(max_dd, dd_pct)

            final_equities.append(equity)
            max_drawdowns.append(max_dd)

            if equity < self._initial_capital * 0.5:
                ruin_count += 1

        final_equities.sort()
        max_drawdowns.sort()

        n = len(final_equities)

        def _percentile(arr: list, pct: float) -> float:
            idx = min(int(n * pct), n - 1)
            return arr[idx]

        return MonteCarloResult(
            n_simulations=self._n_sims,
            original_final_equity=original_equity,
            p5_final_equity=_percentile(final_equities, 0.05),
            p50_final_equity=_percentile(final_equities, 0.50),
            p95_final_equity=_percentile(final_equities, 0.95),
            p5_max_drawdown=_percentile(max_drawdowns, 0.95),
            p95_max_drawdown=_percentile(max_drawdowns, 0.05),
            ruin_probability=(ruin_count / self._n_sims) * 100,
            is_fragile=_percentile(max_drawdowns, 0.95) > 15.0,
        )
