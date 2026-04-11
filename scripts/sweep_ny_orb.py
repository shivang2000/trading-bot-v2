#!/usr/bin/env python3
"""Parameter sweep for NY Opening Range Breakout strategy.

Tests 18 configurations (3 range windows × 3 TP multipliers × 2 entry modes)
across specified instruments. Runs 8 parallel processes.

Usage:
    python scripts/sweep_ny_orb.py --symbols XAUUSD,US30 --risk-pct 1.0
    python scripts/sweep_ny_orb.py --symbols XAUUSD --risk-pct 0.5 --parallel 4
"""
from __future__ import annotations

import argparse
import itertools
import logging
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtesting.data_loader import load_from_csv
from src.backtesting.scalping_engine import ScalpingBacktestEngine
from src.backtesting.cost_model import CostModel
from src.analysis.sessions import SessionManager
from src.analysis.strategies.m5_ny_orb import M5NyOrbStrategy

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)-8s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sweep_ny_orb")
logger.setLevel(logging.INFO)

# Parameter grid
RANGE_MINUTES = [15, 30, 60]
TP_MULTIPLIERS = [1.5, 2.0, 2.5]
ENTRY_MODES = [
    {"retrace_entry": False, "label": "imm"},
    {"retrace_entry": True, "label": "ret"},
]


def _find_csv(symbol: str, timeframe: str) -> str | None:
    cache_dir = Path("data/backtest_cache")
    matches = sorted(cache_dir.glob(f"{symbol}_{timeframe}_*.csv"))
    if matches:
        return str(matches[-1])
    fallback = cache_dir / f"{symbol}_{timeframe}.csv"
    return str(fallback) if fallback.exists() else None


def _get_instrument_config(symbol: str) -> tuple[float, float]:
    """Return (point_size, tick_value) for an instrument."""
    from src.config.loader import load_config
    cfg = load_config()
    for inst in cfg.instruments:
        if inst.symbol == symbol:
            return inst.point_size, inst.tick_value
    return 0.01, 1.0


def run_single_config(
    symbol: str, range_min: int, tp_mult: float,
    retrace: bool, entry_label: str,
    capital: float, risk_pct: float, max_lot: float,
    csv_path: str, h1_path: str | None,
    point_size: float, tick_value: float,
) -> dict:
    """Run a single backtest configuration. Called in subprocess."""
    import pandas as pd
    from src.backtesting.data_loader import load_from_csv as _load

    label = f"r{range_min}_tp{tp_mult:.0f}0_{entry_label}"

    strategy = M5NyOrbStrategy(
        range_minutes=range_min,
        tp_multiplier=tp_mult,
        retrace_entry=retrace,
        retrace_pct=0.5,
        retrace_timeout_bars=24,
        use_rsi_filter=True,
    )

    primary_data = _load(csv_path)
    h1_data = _load(h1_path) if h1_path else None

    cost_model = CostModel(session_manager=SessionManager())
    engine = ScalpingBacktestEngine(
        symbol=symbol,
        primary_timeframe="M5",
        strategies=[strategy],
        initial_capital=capital,
        cost_model=cost_model,
        max_daily_trades=50,
        risk_pct=risk_pct,
        profit_growth_factor=0.50,
        max_lot=max_lot,
        point_size=point_size,
        tick_value=tick_value,
    )

    result = engine.run(primary_data, h1_data)

    return {
        "symbol": symbol,
        "label": label,
        "range_min": range_min,
        "tp_mult": tp_mult,
        "entry": entry_label,
        "trades": result.total_trades,
        "return_pct": round(result.total_return_pct, 2),
        "max_dd_pct": round(result.max_drawdown_pct, 2),
        "win_rate": round(result.win_rate, 1),
        "profit_factor": round(result.profit_factor, 2),
        "sharpe": round(result.sharpe_ratio, 2),
    }


def main():
    parser = argparse.ArgumentParser(description="NY ORB Parameter Sweep")
    parser.add_argument("--symbols", default="XAUUSD,US30", help="Comma-separated symbols")
    parser.add_argument("--risk-pct", type=float, default=1.0)
    parser.add_argument("--capital", type=float, default=100000)
    parser.add_argument("--max-lot", type=float, default=3.0)
    parser.add_argument("--parallel", type=int, default=8)
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",")]

    # Build all configs
    configs = []
    for symbol in symbols:
        csv_path = _find_csv(symbol, "M5")
        if not csv_path:
            logger.error("No M5 CSV for %s", symbol)
            continue

        h1_path = _find_csv(symbol, "H1")
        point_size, tick_value = _get_instrument_config(symbol)

        for range_min, tp_mult, entry_mode in itertools.product(
            RANGE_MINUTES, TP_MULTIPLIERS, ENTRY_MODES
        ):
            configs.append({
                "symbol": symbol,
                "range_min": range_min,
                "tp_mult": tp_mult,
                "retrace": entry_mode["retrace_entry"],
                "entry_label": entry_mode["label"],
                "capital": args.capital,
                "risk_pct": args.risk_pct,
                "max_lot": args.max_lot,
                "csv_path": csv_path,
                "h1_path": h1_path,
                "point_size": point_size,
                "tick_value": tick_value,
            })

    logger.info("Running %d configurations (%d parallel)...", len(configs), args.parallel)

    results = []
    with ProcessPoolExecutor(max_workers=args.parallel) as pool:
        futures = {pool.submit(run_single_config, **cfg): cfg for cfg in configs}
        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
                logger.info(
                    "  %s %s: %d trades, %.1f%% return, %.1f%% DD, %.1f%% WR",
                    result["symbol"], result["label"],
                    result["trades"], result["return_pct"],
                    result["max_dd_pct"], result["win_rate"],
                )
            except Exception as e:
                cfg = futures[future]
                logger.error("FAILED: %s %s: %s", cfg["symbol"], cfg.get("range_min"), e)

    # Sort by return descending
    results.sort(key=lambda r: r["return_pct"], reverse=True)

    # Print results table
    print("\n" + "=" * 100)
    print(f"NY ORB Parameter Sweep Results (risk={args.risk_pct}%, capital=${args.capital:,.0f})")
    print("=" * 100)

    for symbol in symbols:
        sym_results = [r for r in results if r["symbol"] == symbol]
        print(f"\n--- {symbol} ---")
        print(f"{'Config':<25} {'Trades':>7} {'Return%':>9} {'MaxDD%':>8} {'WinRate':>8} {'PF':>6} {'Sharpe':>7}")
        print("-" * 75)
        for r in sym_results:
            marker = " ***" if r["return_pct"] > 0 and r["max_dd_pct"] < 15 else ""
            print(
                f"{r['label']:<25} {r['trades']:>7} {r['return_pct']:>8.1f}% "
                f"{r['max_dd_pct']:>7.1f}% {r['win_rate']:>7.1f}% "
                f"{r['profit_factor']:>5.2f} {r['sharpe']:>6.2f}{marker}"
            )

    # Highlight winners
    print("\n" + "=" * 100)
    print("WINNERS (positive return, DD < 15%):")
    winners = [r for r in results if r["return_pct"] > 0 and r["max_dd_pct"] < 15]
    if winners:
        for r in winners:
            print(f"  {r['symbol']} {r['label']}: +{r['return_pct']}%, {r['max_dd_pct']}% DD, {r['win_rate']}% WR")
    else:
        print("  No winning configurations found.")
    print("=" * 100)


if __name__ == "__main__":
    main()
