#!/usr/bin/env python3
"""Parameter Grid Search -- find optimal capital x risk x strategy for 10x target.

Runs each strategy independently with different capital and risk combinations.
Uses multiprocessing for parallel execution.

Usage:
    python scripts/optimize_grid.py --symbol XAUUSD --workers 4
    python scripts/optimize_grid.py --symbol XAUUSD --strategies m5_dual_supertrend --workers 8
    python scripts/optimize_grid.py --symbol XAUUSD --capital 50,100,200 --risk 0.5,1.0,2.0
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import product
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-8s] %(message)s")
logger = logging.getLogger("optimize_grid")

# -- Strategy registry (lazy imports inside run_single_backtest) -------------

M5_STRATEGIES = {
    "m5_dual_supertrend": ("src.analysis.strategies.m5_dual_supertrend", "M5DualSupertrendStrategy"),
    "m5_keltner_squeeze": ("src.analysis.strategies.m5_keltner_squeeze", "M5KeltnerSqueezeStrategy"),
    "m5_mtf_momentum": ("src.analysis.strategies.m5_mtf_momentum", "M5MtfMomentumStrategy"),
}

M15_STRATEGIES = {
    "ema_pullback": ("src.analysis.strategies.ema_pullback", "EmaPullbackStrategy"),
    "london_breakout": ("src.analysis.strategies.london_breakout", "LondonBreakoutStrategy"),
}

ALL_STRATEGIES = {**M5_STRATEGIES, **M15_STRATEGIES}

# -- Default grid values ----------------------------------------------------

DEFAULT_CAPITALS = [50, 100, 200, 300, 500, 1000]
DEFAULT_RISKS = [0.3, 0.5, 0.8, 1.0, 1.5, 2.0]
DEFAULT_GROWTHS = [0.30, 0.50, 0.75, 1.00]

CSV_COLUMNS = [
    "strategy", "capital", "risk_pct", "profit_growth", "final_equity",
    "return_pct", "profit_factor", "max_drawdown_pct", "total_trades",
    "win_rate", "sharpe", "avg_pnl", "is_10x", "is_5x_safe", "return_dd_ratio",
]


# -- Core backtest function (top-level for multiprocessing) -----------------

def run_single_backtest(params: dict) -> dict:
    """Run a single backtest with given parameters. Returns result dict."""
    import importlib

    from src.backtesting.cost_model import CostModel
    from src.backtesting.data_loader import load_from_csv
    from src.analysis.sessions import SessionManager

    strategy_name = params["strategy"]
    capital = params["capital"]
    risk_pct = params["risk_pct"]
    profit_growth = params["profit_growth"]
    csv_path = params["csv_path"]
    symbol = params["symbol"]
    enable_costs = params["enable_costs"]

    try:
        data = load_from_csv(csv_path)

        cost_model = None
        if enable_costs:
            cost_model = CostModel(session_manager=SessionManager())

        # Determine strategy type and run
        if strategy_name in M5_STRATEGIES:
            from src.backtesting.scalping_engine import ScalpingBacktestEngine

            mod_path, cls_name = M5_STRATEGIES[strategy_name]
            strategy_cls = getattr(importlib.import_module(mod_path), cls_name)
            strategy = strategy_cls()

            engine = ScalpingBacktestEngine(
                symbol=symbol,
                strategies=[strategy],
                initial_capital=capital,
                risk_pct=risk_pct,
                profit_growth_factor=profit_growth,
                cost_model=cost_model,
                max_daily_trades=50,
            )
            result = engine.run(data)

        elif strategy_name in M15_STRATEGIES:
            from src.backtesting.engine import BacktestEngine
            from src.backtesting.data_loader import resample_m15_to_h1
            from src.config.loader import load_config

            # Resample M5 -> M15 -> H1
            m15_data = data.set_index("time").resample("15min").agg({
                "open": "first", "high": "max", "low": "min", "close": "last",
                "tick_volume": "sum", "real_volume": "sum", "spread": "mean",
            }).dropna(subset=["open"]).reset_index()
            h1_data = resample_m15_to_h1(m15_data)

            config = load_config()
            config.account.risk_per_trade_pct = risk_pct

            engine = BacktestEngine(
                symbol=symbol,
                strategy=strategy_name,
                initial_capital=capital,
                config=config,
            )
            result = engine.run(m15_data, h1_data)
        else:
            return {"error": f"Unknown strategy: {strategy_name}"}

        return {
            "strategy": strategy_name,
            "capital": capital,
            "risk_pct": risk_pct,
            "profit_growth": profit_growth,
            "final_equity": result.final_equity,
            "return_pct": result.total_return_pct,
            "profit_factor": result.profit_factor,
            "max_drawdown_pct": result.max_drawdown_pct,
            "total_trades": result.total_trades,
            "win_rate": result.win_rate,
            "sharpe": result.sharpe_ratio,
            "avg_pnl": result.avg_trade_pnl,
            "is_10x": result.total_return_pct >= 900,
            "is_5x_safe": result.total_return_pct >= 400 and result.max_drawdown_pct < 50,
            "return_dd_ratio": round(
                result.total_return_pct / max(result.max_drawdown_pct, 0.01), 2
            ),
        }
    except Exception as e:
        return {
            "strategy": strategy_name, "capital": capital,
            "risk_pct": risk_pct, "profit_growth": profit_growth,
            "error": str(e),
        }


# -- CSV auto-detection -----------------------------------------------------

def _find_m5_csv(symbol: str) -> str | None:
    cache_dir = Path("data/backtest_cache")
    if not cache_dir.exists():
        return None
    # Prefer *_M5_all.csv, then any M5 csv sorted by name (latest)
    exact = cache_dir / f"{symbol}_M5_all.csv"
    if exact.exists():
        return str(exact)
    matches = sorted(cache_dir.glob(f"{symbol}_M5_*.csv"))
    return str(matches[-1]) if matches else None


# -- Output helpers ---------------------------------------------------------

def _save_csv(results: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    logger.info("Results saved to %s", output_path)


def _print_summary(results: list[dict], elapsed: float) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    valid = [r for r in results if "error" not in r]
    errors = [r for r in results if "error" in r]

    if not valid:
        console.print("[red]No successful results.[/red]")
        if errors:
            for e in errors[:5]:
                console.print(f"  [dim]{e['strategy']} cap={e['capital']} risk={e['risk_pct']}: {e['error']}[/dim]")
        return

    sorted_by_rdd = sorted(valid, key=lambda x: x["return_dd_ratio"], reverse=True)

    # -- Top 10 table --
    table = Table(title="Top 10 Configurations (by Return/DD Ratio)")
    table.add_column("Strategy", style="cyan")
    table.add_column("Capital", justify="right")
    table.add_column("Risk%", justify="right")
    table.add_column("Growth", justify="right")
    table.add_column("Return%", justify="right", style="green")
    table.add_column("Max DD%", justify="right", style="red")
    table.add_column("PF", justify="right")
    table.add_column("Trades", justify="right")
    table.add_column("R/DD", justify="right", style="bold")
    table.add_column("10x?", justify="center")

    for r in sorted_by_rdd[:10]:
        table.add_row(
            r["strategy"], str(r["capital"]),
            f"{r['risk_pct']:.1f}", f"{r['profit_growth']:.2f}",
            f"{r['return_pct']:.1f}", f"{r['max_drawdown_pct']:.1f}",
            f"{r['profit_factor']:.2f}", str(r["total_trades"]),
            f"{r['return_dd_ratio']:.1f}",
            "[green]YES[/green]" if r["is_10x"] else "[dim]no[/dim]",
        )
    console.print(table)

    # -- Best per strategy --
    console.print("\n[bold]Best per Strategy:[/bold]")
    seen: set[str] = set()
    for r in sorted_by_rdd:
        if r["strategy"] not in seen:
            seen.add(r["strategy"])
            console.print(
                f"  {r['strategy']:25s}  cap={r['capital']:>6}  risk={r['risk_pct']:.1f}%  "
                f"growth={r['profit_growth']:.2f}  return={r['return_pct']:>8.1f}%  "
                f"DD={r['max_drawdown_pct']:.1f}%  R/DD={r['return_dd_ratio']:.1f}"
            )

    # -- 10x achievers --
    tens = [r for r in valid if r["is_10x"]]
    if tens:
        console.print(f"\n[bold green]10x Achievers: {len(tens)} configurations[/bold green]")
        for r in sorted(tens, key=lambda x: x["return_dd_ratio"], reverse=True)[:5]:
            console.print(
                f"  {r['strategy']:25s}  cap={r['capital']:>6}  risk={r['risk_pct']:.1f}%  "
                f"return={r['return_pct']:.1f}%  DD={r['max_drawdown_pct']:.1f}%"
            )
    else:
        console.print("\n[dim]No 10x achievers found.[/dim]")

    # -- Safe 5x+ --
    safe = [r for r in valid if r["is_5x_safe"]]
    if safe:
        console.print(f"\n[bold yellow]Safe 5x+ (return>400% AND DD<50%): {len(safe)} configurations[/bold yellow]")
        for r in sorted(safe, key=lambda x: x["return_dd_ratio"], reverse=True)[:5]:
            console.print(
                f"  {r['strategy']:25s}  cap={r['capital']:>6}  risk={r['risk_pct']:.1f}%  "
                f"return={r['return_pct']:.1f}%  DD={r['max_drawdown_pct']:.1f}%"
            )

    # -- Footer --
    console.print(f"\n[dim]Total combos: {len(results)}  |  Success: {len(valid)}  "
                  f"|  Errors: {len(errors)}  |  Elapsed: {elapsed:.1f}s[/dim]")


# -- CLI --------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Parameter grid search for optimal capital x risk x strategy"
    )
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--csv-m5", help="Path to M5 CSV (auto-detects from cache if omitted)")
    p.add_argument("--strategies", default=",".join(ALL_STRATEGIES),
                   help="Comma-separated strategy names (default: all 5)")
    p.add_argument("--capital", default=",".join(str(c) for c in DEFAULT_CAPITALS),
                   help="Comma-separated capital values")
    p.add_argument("--risk", default=",".join(str(r) for r in DEFAULT_RISKS),
                   help="Comma-separated risk %% values")
    p.add_argument("--growth", default=",".join(str(g) for g in DEFAULT_GROWTHS),
                   help="Comma-separated profit growth factors")
    p.add_argument("--workers", type=int, default=4, help="Parallel workers")
    p.add_argument("--enable-costs", action="store_true", default=True)
    p.add_argument("--no-costs", dest="enable_costs", action="store_false",
                   help="Disable spread/slippage costs")
    p.add_argument("--output", default="data/optimization/grid_results.csv",
                   help="CSV output path")
    return p


def main() -> None:
    args = _build_parser().parse_args()

    csv_path = args.csv_m5 or _find_m5_csv(args.symbol)
    if csv_path is None:
        sys.exit(
            f"ERROR: No M5 CSV found for {args.symbol}. "
            f"Pass --csv-m5 or place a file in data/backtest_cache/"
        )
    logger.info("Using M5 data: %s", csv_path)

    strategies = [s.strip() for s in args.strategies.split(",")]
    capitals = [float(c) for c in args.capital.split(",")]
    risks = [float(r) for r in args.risk.split(",")]
    growths = [float(g) for g in args.growth.split(",")]

    unknown = [s for s in strategies if s not in ALL_STRATEGIES]
    if unknown:
        sys.exit(f"ERROR: Unknown strategies: {', '.join(unknown)}")

    # Build cartesian product grid
    grid = [
        {
            "strategy": strat, "capital": cap, "risk_pct": risk,
            "profit_growth": growth, "csv_path": csv_path,
            "symbol": args.symbol, "enable_costs": args.enable_costs,
        }
        for strat, cap, risk, growth in product(strategies, capitals, risks, growths)
    ]

    total = len(grid)
    logger.info(
        "Grid: %d strategies x %d capitals x %d risks x %d growths = %d combos",
        len(strategies), len(capitals), len(risks), len(growths), total,
    )

    # Run with ProcessPoolExecutor
    results: list[dict] = []
    t0 = time.time()

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_single_backtest, p): p for p in grid}
        done = 0
        for future in as_completed(futures):
            done += 1
            try:
                result = future.result()
            except Exception as exc:
                p = futures[future]
                result = {
                    "strategy": p["strategy"], "capital": p["capital"],
                    "risk_pct": p["risk_pct"], "profit_growth": p["profit_growth"],
                    "error": str(exc),
                }
            results.append(result)
            if done % 10 == 0 or done == total:
                logger.info("Progress: %d/%d (%.0f%%)", done, total, done / total * 100)

    elapsed = time.time() - t0

    # Sort valid results by return_dd_ratio descending
    valid = [r for r in results if "error" not in r]
    valid.sort(key=lambda x: x["return_dd_ratio"], reverse=True)
    errors = [r for r in results if "error" in r]
    results = valid + errors

    # Save CSV
    _save_csv(results, Path(args.output))

    # Print summary
    _print_summary(results, elapsed)


if __name__ == "__main__":
    main()
