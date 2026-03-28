#!/usr/bin/env python3
"""Scalping Backtest CLI -- runs M1/M5 strategies with proper data resolution.

Usage:
    python scripts/backtest_scalping.py --symbol XAUUSD --timeframe M5 --strategy all
    python scripts/backtest_scalping.py --symbol XAUUSD --timeframe M1 --strategy m1_heikin_ashi_momentum
    python scripts/backtest_scalping.py --symbol XAUUSD --timeframe M5 --compare --report
    python scripts/backtest_scalping.py --symbol XAUUSD --multi-period --report
"""
from __future__ import annotations

import argparse, importlib, json, logging, sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtesting.data_loader import load_from_csv
from src.backtesting.scalping_engine import ScalpingBacktestEngine
from src.backtesting.cost_model import CostModel
from src.analysis.sessions import SessionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)-30s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backtest_scalping")

# -- Strategy registry (graceful imports) -----------------------------------

M5_STRATEGIES: dict[str, type] = {}
M1_STRATEGIES: dict[str, type] = {}

_IMPORTS: list[tuple[dict, str, str, str]] = [
    (M5_STRATEGIES, "m5_vwap_mean_reversion", "src.analysis.strategies.m5_vwap_mean_reversion", "M5VwapMeanReversionStrategy"),
    (M5_STRATEGIES, "m5_dual_supertrend", "src.analysis.strategies.m5_dual_supertrend", "M5DualSupertrendStrategy"),
    (M5_STRATEGIES, "m5_keltner_squeeze", "src.analysis.strategies.m5_keltner_squeeze", "M5KeltnerSqueezeStrategy"),
    (M5_STRATEGIES, "m5_stochrsi_adx", "src.analysis.strategies.m5_stochrsi_adx", "M5StochRsiAdxStrategy"),
    (M5_STRATEGIES, "m5_mtf_momentum", "src.analysis.strategies.m5_mtf_momentum", "M5MtfMomentumStrategy"),
    (M5_STRATEGIES, "m5_bb_squeeze", "src.analysis.strategies.m5_bb_squeeze", "M5BbSqueezeStrategy"),
    (M5_STRATEGIES, "m5_mean_reversion", "src.analysis.strategies.m5_mean_reversion", "M5MeanReversionStrategy"),
    (M1_STRATEGIES, "m1_heikin_ashi_momentum", "src.analysis.strategies.m1_heikin_ashi_momentum", "M1HeikinAshiMomentumStrategy"),
    (M1_STRATEGIES, "m1_rsi_scalp", "src.analysis.strategies.m1_rsi_scalp", "M1RsiScalpStrategy"),
    (M1_STRATEGIES, "m1_supertrend_scalp", "src.analysis.strategies.m1_supertrend_scalp", "M1SupertrendScalpStrategy"),
    (M1_STRATEGIES, "m1_ema_micro", "src.analysis.strategies.m1_ema_micro", "M1EmaMicroStrategy"),
]

for registry, key, mod_path, cls_name in _IMPORTS:
    try:
        registry[key] = getattr(importlib.import_module(mod_path), cls_name)
    except (ImportError, AttributeError) as exc:
        logger.debug("Skipping strategy %s: %s", key, exc)

ALL_STRATEGY_NAMES = sorted(list(M5_STRATEGIES) + list(M1_STRATEGIES))

PERIODS = {
    "9m": ("2025-07-01", "2026-03-27"),
    "15m": ("2025-01-01", "2026-03-27"),
    "24m": ("2024-04-01", "2026-03-27"),
}

_RESULT_FIELDS = [
    "strategy_name", "symbol", "start_date", "end_date", "initial_capital",
    "final_equity", "total_return_pct", "max_drawdown_pct", "win_rate",
    "profit_factor", "total_trades", "avg_trade_pnl", "sharpe_ratio",
    "sortino_ratio", "max_consecutive_losses", "max_consecutive_wins",
    "avg_trade_duration_hours", "best_trade_pnl", "worst_trade_pnl", "expectancy",
]


def _resolve_strategies(strategy_arg: str, timeframe: str) -> dict[str, type]:
    if strategy_arg == "all":
        return dict(M1_STRATEGIES) if timeframe == "M1" else {**M5_STRATEGIES, **M1_STRATEGIES}
    if strategy_arg == "all_m5":
        return dict(M5_STRATEGIES)
    if strategy_arg == "all_m1":
        return dict(M1_STRATEGIES)
    combined = {**M5_STRATEGIES, **M1_STRATEGIES}
    if strategy_arg in combined:
        return {strategy_arg: combined[strategy_arg]}
    logger.error("Unknown strategy: %s", strategy_arg)
    sys.exit(1)


def _filter_by_dates(df: pd.DataFrame, start: str | None, end: str | None) -> pd.DataFrame:
    if start:
        df = df[df["time"] >= pd.Timestamp(start, tz="UTC")]
    if end:
        df = df[df["time"] <= pd.Timestamp(end, tz="UTC")]
    return df.reset_index(drop=True)


def _find_csv(symbol: str, timeframe: str) -> str | None:
    cache_dir = Path("data/backtest_cache")
    if not cache_dir.exists():
        return None
    matches = sorted(cache_dir.glob(f"{symbol}_{timeframe}_*.csv"))
    if matches:
        return str(matches[-1])
    fallback = cache_dir / f"{symbol}_{timeframe}.csv"
    return str(fallback) if fallback.exists() else None


def _build_engine(symbol, timeframe, strategies, capital, max_daily, risk_pct, costs,
                   profit_growth_factor=0.50, max_lot=0.50, use_tiered_caps=False):
    cost_model = CostModel(session_manager=SessionManager()) if costs else None
    return ScalpingBacktestEngine(
        symbol=symbol, primary_timeframe=timeframe, strategies=strategies,
        initial_capital=capital, cost_model=cost_model,
        max_daily_trades=max_daily, risk_pct=risk_pct,
        profit_growth_factor=profit_growth_factor,
        max_lot=max_lot, use_tiered_caps=use_tiered_caps,
    )


def _result_to_dict(result) -> dict:
    d = {f: str(getattr(result, f)) if "date" in f else getattr(result, f) for f in _RESULT_FIELDS}
    d["strategy"] = d.pop("strategy_name")
    d["trades"] = [
        {"ticket": t.ticket, "side": t.side.value, "open_price": t.open_price,
         "close_price": t.close_price, "open_time": str(t.open_time),
         "close_time": str(t.close_time), "pnl": t.pnl, "close_reason": t.close_reason}
        for t in result.trades
    ]
    return d


def _save_results(data: dict, symbol: str, label: str) -> Path:
    out_dir = Path("data/backtest_results")
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"scalp_{symbol}_{label}_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)
    logger.info("Results saved to %s", out_path)
    return out_path


def _print_comparison_table(rows: list[dict]) -> None:
    from rich.console import Console
    from rich.table import Table
    table = Table(title="Scalping Strategy Comparison")
    table.add_column("Strategy", style="cyan")
    table.add_column("Trades", justify="right")
    table.add_column("Win Rate", justify="right")
    table.add_column("PF", justify="right")
    table.add_column("Return %", justify="right")
    table.add_column("Avg PnL", justify="right")
    table.add_column("Max DD %", justify="right")
    table.add_column("Sharpe", justify="right")
    for r in rows:
        table.add_row(
            r.get("strategy", "?"), str(r.get("total_trades", 0)),
            f"{r.get('win_rate', 0):.1f}%", f"{r.get('profit_factor', 0):.2f}",
            f"{r.get('total_return_pct', 0):.2f}%", f"{r.get('avg_trade_pnl', 0):.2f}",
            f"{r.get('max_drawdown_pct', 0):.2f}%", f"{r.get('sharpe_ratio', 0):.2f}",
        )
    Console().print(table)


def _try_generate_report(data: dict, report_dir: str) -> None:
    try:
        from src.backtesting.report_generator import generate_report
        Path(report_dir).mkdir(parents=True, exist_ok=True)
        generate_report(data, output_dir=report_dir)
        logger.info("HTML report generated in %s", report_dir)
    except ImportError:
        logger.warning("report_generator not available; skipping HTML report")
    except Exception as exc:
        logger.warning("Report generation failed: %s", exc)


# -- CLI --------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    valid = ["all", "all_m5", "all_m1"] + ALL_STRATEGY_NAMES
    p = argparse.ArgumentParser(description="Scalping Backtest CLI for M1/M5 strategies")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--timeframe", choices=["M1", "M5"], default="M5")
    p.add_argument("--csv-primary", help="Path to M1 or M5 CSV file")
    p.add_argument("--csv-h1", help="Optional H1 CSV (otherwise resampled)")
    p.add_argument("--strategy", default="all", choices=valid)
    p.add_argument("--start", help="Start date YYYY-MM-DD")
    p.add_argument("--end", help="End date YYYY-MM-DD")
    p.add_argument("--initial-capital", type=float, default=10_000.0)
    p.add_argument("--max-daily-trades", type=int, default=50)
    p.add_argument("--risk-pct", type=float, default=1.0, help="Risk %% per trade")
    p.add_argument("--enable-costs", action="store_true", default=True)
    p.add_argument("--no-costs", action="store_true", help="Disable spread/slippage")
    p.add_argument("--compare", action="store_true", help="Compare strategies individually")
    p.add_argument("--multi-period", action="store_true", help="Run across 9m/15m/24m")
    p.add_argument("--report", action="store_true", help="Generate HTML report")
    p.add_argument("--report-dir", default="reports/")
    p.add_argument("--label", default="", help="Label for output files")
    p.add_argument("--profit-growth", type=float, default=0.50, help="Profit growth factor (0.5 = use 50%% of profits for sizing)")
    p.add_argument("--max-lot", type=float, default=0.50, help="Max lot size per trade")
    p.add_argument("--tiered-caps", action="store_true", help="Enable tiered lot caps based on equity")
    p.add_argument("--trail-giveback", type=float, default=0.10, help="Trailing stop giveback percentage")
    p.add_argument("--trail-maxgive", type=float, default=10.0, help="Max trailing giveback in dollars")
    p.add_argument("--trail-activation", type=float, default=5.0, help="Trailing activation profit in dollars")
    return p


def _run_engine(args, strat_map, primary_data, h1_data, enable_costs, label):
    """Execute single run, compare, or multi-period mode."""
    if args.multi_period:
        all_results = []
        for pname, (ps, pe) in PERIODS.items():
            pdata = _filter_by_dates(primary_data, ps, pe)
            if len(pdata) < 300:
                logger.warning("Period %s: only %d bars, skipping", pname, len(pdata))
                continue
            engine = _build_engine(
                args.symbol, args.timeframe, [c() for c in strat_map.values()],
                args.initial_capital, args.max_daily_trades, args.risk_pct, enable_costs,
                args.profit_growth, args.max_lot, args.tiered_caps,
            )
            logger.info("Running period %s (%s to %s, %d bars)", pname, ps, pe, len(pdata))
            result = engine.run(pdata, h1_data)
            rd = _result_to_dict(result)
            rd["period"], rd["strategy"] = pname, f"{label}_{pname}"
            all_results.append(rd)
            print(result.summary())
        _print_comparison_table(all_results)
        combined = {"mode": "multi_period", "periods": all_results}
        _save_results(combined, args.symbol, f"{label}_multiperiod")
        if args.report:
            _try_generate_report(combined, args.report_dir)
        return

    if args.compare:
        all_results = []
        for name, cls in strat_map.items():
            engine = _build_engine(
                args.symbol, args.timeframe, [cls()],
                args.initial_capital, args.max_daily_trades, args.risk_pct, enable_costs,
                args.profit_growth, args.max_lot, args.tiered_caps,
            )
            logger.info("Running strategy: %s", name)
            result = engine.run(primary_data, h1_data)
            rd = _result_to_dict(result)
            rd["strategy"] = name
            all_results.append(rd)
        _print_comparison_table(all_results)
        combined = {"mode": "compare", "strategies": all_results}
        _save_results(combined, args.symbol, f"{label}_compare")
        if args.report:
            _try_generate_report(combined, args.report_dir)
        return

    # Standard single run
    engine = _build_engine(
        args.symbol, args.timeframe, [c() for c in strat_map.values()],
        args.initial_capital, args.max_daily_trades, args.risk_pct, enable_costs,
        args.profit_growth, args.max_lot, args.tiered_caps,
    )
    result = engine.run(primary_data, h1_data)
    print(result.summary())
    rd = _result_to_dict(result)
    _save_results(rd, args.symbol, label)
    if args.report:
        _try_generate_report(rd, args.report_dir)


def main() -> None:
    args = _build_parser().parse_args()
    enable_costs = not args.no_costs
    label = args.label or args.strategy

    csv_path = args.csv_primary or _find_csv(args.symbol, args.timeframe)
    if csv_path is None:
        sys.exit(
            f"ERROR: No CSV found for {args.symbol} {args.timeframe}. "
            f"Pass --csv-primary or place a file in data/backtest_cache/"
        )
    logger.info("Loading primary %s data from %s", args.timeframe, csv_path)
    primary_data = _filter_by_dates(load_from_csv(csv_path), args.start, args.end)
    logger.info("Primary data: %d bars after date filter", len(primary_data))

    h1_data = load_from_csv(args.csv_h1) if args.csv_h1 else None

    strat_map = _resolve_strategies(args.strategy, args.timeframe)
    if not strat_map:
        sys.exit("ERROR: No strategies available to run")

    _run_engine(args, strat_map, primary_data, h1_data, enable_costs, label)


if __name__ == "__main__":
    main()
