#!/usr/bin/env python3
"""Scalping Strategy Optimizer — Walk-Forward + Monte Carlo.

Usage:
    python scripts/optimize_scalping.py --strategy m5_vwap_mean_reversion --metric expectancy
    python scripts/optimize_scalping.py --strategy m5_dual_supertrend --monte-carlo
    python scripts/optimize_scalping.py --strategy m5_keltner_squeeze --windows 3 --monte-carlo
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from src.backtesting.data_loader import load_from_csv
from src.backtesting.walk_forward import MonteCarloValidator, WalkForwardOptimizer
from src.backtesting.scalping_engine import ScalpingBacktestEngine
from src.backtesting.cost_model import CostModel
from src.analysis.sessions import SessionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)-30s %(message)s",
)
logger = logging.getLogger("optimize_scalping")

# Strategy class registry
STRATEGY_CLASSES: dict[str, type] = {}

_IMPORTS = {
    "m5_vwap_mean_reversion": ("src.analysis.strategies.m5_vwap_mean_reversion", "M5VwapMeanReversionStrategy"),
    "m5_dual_supertrend": ("src.analysis.strategies.m5_dual_supertrend", "M5DualSupertrendStrategy"),
    "m5_keltner_squeeze": ("src.analysis.strategies.m5_keltner_squeeze", "M5KeltnerSqueezeStrategy"),
    "m5_stochrsi_adx": ("src.analysis.strategies.m5_stochrsi_adx", "M5StochRsiAdxStrategy"),
    "m5_mtf_momentum": ("src.analysis.strategies.m5_mtf_momentum", "M5MtfMomentumStrategy"),
}

for name, (module_path, class_name) in _IMPORTS.items():
    try:
        import importlib
        mod = importlib.import_module(module_path)
        STRATEGY_CLASSES[name] = getattr(mod, class_name)
    except Exception:
        pass

# Predefined parameter grids
PARAM_GRIDS: dict[str, dict[str, list]] = {
    "m5_vwap_mean_reversion": {
        "vwap_std_mult": [1.5, 2.0, 2.5],
        "rsi_period": [5, 7, 9],
        "atr_period": [7, 10, 14],
    },
    "m5_dual_supertrend": {
        "fast_period": [5, 7, 10],
        "fast_mult": [1.5, 2.0, 2.5],
        "adx_threshold": [15.0, 20.0, 25.0],
    },
    "m5_keltner_squeeze": {
        "bb_std": [1.5, 2.0, 2.5],
        "kc_atr_mult": [1.0, 1.5, 2.0],
        "tp_channel_mult": [1.0, 1.5, 2.0],
    },
    "m5_stochrsi_adx": {
        "adx_threshold": [20.0, 25.0, 30.0],
        "ob_level": [70.0, 75.0, 80.0],
        "os_level": [20.0, 25.0, 30.0],
    },
    "m5_mtf_momentum": {
        "m5_fast_ema": [7, 9, 12],
        "m5_slow_ema": [18, 21, 26],
        "volume_mult": [1.2, 1.5, 2.0],
    },
}

M1_STRATEGIES = {"m1_ema_micro", "m1_heikin_ashi_momentum", "m1_rsi_scalp", "m1_supertrend_scalp"}


def _find_csv(symbol: str, timeframe: str) -> Path | None:
    cache_dir = Path("data/backtest_cache")
    for pattern in [f"{symbol}_{timeframe}_all.csv", f"{symbol}_{timeframe}_*.csv"]:
        matches = sorted(cache_dir.glob(pattern))
        if matches:
            return matches[-1]
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Scalping Strategy Optimizer")
    parser.add_argument("--strategy", required=True, choices=list(STRATEGY_CLASSES.keys()),
                        help="Strategy to optimize")
    parser.add_argument("--timeframe", default="M5", choices=["M1", "M5"])
    parser.add_argument("--csv-primary", help="Path to primary data CSV")
    parser.add_argument("--metric", default="expectancy",
                        choices=["expectancy", "sharpe_ratio", "profit_factor", "total_return_pct"],
                        help="Optimization target metric")
    parser.add_argument("--windows", type=int, default=5, help="Walk-forward windows")
    parser.add_argument("--is-ratio", type=float, default=0.7, help="In-sample ratio")
    parser.add_argument("--monte-carlo", action="store_true", help="Run Monte Carlo validation")
    parser.add_argument("--mc-sims", type=int, default=1000, help="Monte Carlo simulations")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--initial-capital", type=float, default=10000.0)
    parser.add_argument("--risk-pct", type=float, default=1.0)
    parser.add_argument("--output", help="Save results JSON to path")
    args = parser.parse_args()

    # Block M1 WFO
    if args.strategy in M1_STRATEGIES:
        logger.error(
            "M1 strategies have insufficient data for walk-forward optimization. "
            "Use --monte-carlo only for M1 strategies."
        )
        if not args.monte_carlo:
            sys.exit(1)

    # Load data
    csv_path = args.csv_primary
    if not csv_path:
        found = _find_csv(args.symbol, args.timeframe)
        if found:
            csv_path = str(found)
            logger.info("Auto-detected data: %s", csv_path)
        else:
            logger.error("No CSV found. Use --csv-primary to specify.")
            sys.exit(1)

    logger.info("Loading data from %s", csv_path)
    primary_data = load_from_csv(csv_path)
    logger.info("Loaded %d bars", len(primary_data))

    strategy_class = STRATEGY_CLASSES[args.strategy]
    param_grid = PARAM_GRIDS.get(args.strategy, {})

    engine_kwargs = {
        "symbol": args.symbol,
        "primary_timeframe": args.timeframe,
        "initial_capital": args.initial_capital,
        "risk_pct": args.risk_pct,
        "cost_model": CostModel(session_manager=SessionManager()),
    }

    results = {}

    # Walk-Forward Optimization
    if args.strategy not in M1_STRATEGIES and param_grid:
        logger.info("Starting Walk-Forward Optimization (%d windows, metric=%s)",
                     args.windows, args.metric)
        optimizer = WalkForwardOptimizer(
            strategy_class=strategy_class,
            param_grid=param_grid,
            engine_kwargs=engine_kwargs,
            is_ratio=args.is_ratio,
            n_windows=args.windows,
            optimization_metric=args.metric,
        )
        wfo_result = optimizer.run(primary_data)
        print("\n" + wfo_result.summary())
        results["walk_forward"] = {
            "overall_wfe": wfo_result.overall_wfe,
            "is_robust": wfo_result.is_robust,
            "best_params": wfo_result.best_params,
            "windows": [
                {
                    "idx": w.window_idx, "is": w.is_metric,
                    "oos": w.oos_metric, "wfe": w.wfe,
                    "params": w.best_params, "oos_trades": w.oos_trades,
                }
                for w in wfo_result.windows
            ],
        }

    # Monte Carlo Validation
    if args.monte_carlo:
        logger.info("Running Monte Carlo validation (%d simulations)", args.mc_sims)
        # Run a standard backtest first to get trades
        strategy = strategy_class()
        engine = ScalpingBacktestEngine(strategies=[strategy], **engine_kwargs)
        bt_result = engine.run(primary_data)
        logger.info("Baseline: %d trades, PF=%.2f, WR=%.1f%%",
                     bt_result.total_trades, bt_result.profit_factor, bt_result.win_rate)

        mc = MonteCarloValidator(
            trades=bt_result.trades,
            initial_capital=args.initial_capital,
            n_simulations=args.mc_sims,
        )
        mc_result = mc.run()
        print("\n" + mc_result.summary())
        results["monte_carlo"] = {
            "n_sims": mc_result.n_simulations,
            "original_equity": mc_result.original_final_equity,
            "p5_equity": mc_result.p5_final_equity,
            "p50_equity": mc_result.p50_final_equity,
            "p95_equity": mc_result.p95_final_equity,
            "p5_max_dd": mc_result.p5_max_drawdown,
            "ruin_prob": mc_result.ruin_probability,
            "fragile": mc_result.is_fragile,
        }

    # Save results
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(results, f, indent=2, default=str)
        logger.info("Results saved to %s", args.output)

    print("\nDone.")


if __name__ == "__main__":
    main()
