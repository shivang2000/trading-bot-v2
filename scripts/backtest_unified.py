#!/usr/bin/env python3
"""Unified Backtest CLI -- runs M15 + M5 strategies on ONE shared account.

Simulates real-world trading where all strategies share equity and positions.

Usage:
    python scripts/backtest_unified.py --symbol XAUUSD --initial-capital 100 --enable-costs
    python scripts/backtest_unified.py --symbol XAUUSD --m15-strategies ema_pullback,london_breakout \
        --m5-strategies m5_mtf_momentum,m5_keltner_squeeze,m5_dual_supertrend --report
"""

from __future__ import annotations

import argparse, importlib, json, logging, sys
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtesting.data_loader import load_from_csv, resample_m15_to_h1
from src.backtesting.cost_model import CostModel
from src.backtesting.unified_engine import UnifiedBacktestEngine
from src.analysis.sessions import SessionManager
from src.config.loader import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)-30s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("backtest_unified")

# ---------------------------------------------------------------------------
# Strategy registries (graceful imports)
# ---------------------------------------------------------------------------

M15_REGISTRY: dict[str, tuple[str, str]] = {
    "ema_pullback": ("src.analysis.strategies.ema_pullback", "EmaPullbackStrategy"),
    "london_breakout": ("src.analysis.strategies.london_breakout", "LondonBreakoutStrategy"),
}

M5_REGISTRY: dict[str, tuple[str, str]] = {
    "m5_dual_supertrend": ("src.analysis.strategies.m5_dual_supertrend", "M5DualSupertrendStrategy"),
    "m5_keltner_squeeze": ("src.analysis.strategies.m5_keltner_squeeze", "M5KeltnerSqueezeStrategy"),
    "m5_mtf_momentum": ("src.analysis.strategies.m5_mtf_momentum", "M5MtfMomentumStrategy"),
    "m5_vwap_mean_reversion": ("src.analysis.strategies.m5_vwap_mean_reversion", "M5VwapMeanReversionStrategy"),
    "m5_stochrsi_adx": ("src.analysis.strategies.m5_stochrsi_adx", "M5StochRsiAdxStrategy"),
    "m5_bb_squeeze": ("src.analysis.strategies.m5_bb_squeeze", "M5BbSqueezeStrategy"),
    "m5_mean_reversion": ("src.analysis.strategies.m5_mean_reversion", "M5MeanReversionStrategy"),
}

DEFAULT_M15 = "ema_pullback,london_breakout"
DEFAULT_M5 = "m5_mtf_momentum,m5_keltner_squeeze,m5_dual_supertrend"

_RESULT_FIELDS = [
    "strategy_name", "symbol", "start_date", "end_date", "initial_capital",
    "final_equity", "total_return_pct", "max_drawdown_pct", "win_rate",
    "profit_factor", "total_trades", "avg_trade_pnl", "sharpe_ratio",
    "sortino_ratio", "max_consecutive_losses", "max_consecutive_wins",
    "avg_trade_duration_hours", "best_trade_pnl", "worst_trade_pnl", "expectancy",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _import_class(mod_path: str, cls_name: str):
    """Import a class by module path and name; returns None on failure."""
    try:
        return getattr(importlib.import_module(mod_path), cls_name)
    except (ImportError, AttributeError) as exc:
        logger.debug("Skipping %s.%s: %s", mod_path, cls_name, exc)
        return None


def _find_csv(symbol: str, timeframe: str) -> str | None:
    cache = Path("data/backtest_cache")
    if not cache.exists():
        return None
    for pattern in [f"{symbol}_{timeframe}_all.csv", f"{symbol}_{timeframe}_*.csv"]:
        matches = sorted(cache.glob(pattern))
        if matches:
            return str(matches[-1])
    return None


def _resample_m5_to_m15(m5_df: pd.DataFrame) -> pd.DataFrame:
    df = m5_df.copy().set_index("time")
    m15 = df.resample("15min").agg({
        "open": "first", "high": "max", "low": "min", "close": "last",
        "tick_volume": "sum", "real_volume": "sum", "spread": "mean",
    }).dropna(subset=["open"]).reset_index()
    logger.info("Resampled %d M5 bars -> %d M15 bars", len(m5_df), len(m15))
    return m15


def _instantiate_strategies(names: str, registry: dict, cfg=None, is_m15: bool = False):
    """Build strategy instances from comma-separated names."""
    strategies = []
    for name in names.split(","):
        name = name.strip()
        if not name or name not in registry:
            logger.warning("Unknown strategy: %s -- skipping", name)
            continue
        mod_path, cls_name = registry[name]
        cls = _import_class(mod_path, cls_name)
        if cls is None:
            continue
        if is_m15 and cfg is not None:
            strat_cfg = getattr(cfg.strategies, name, None)
            strategies.append(cls(strat_cfg) if strat_cfg else cls(type(cls.__init__.__annotations__.get("config", object))()))
        else:
            strategies.append(cls())
    return strategies


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
    out_path = out_dir / f"unified_{symbol}_{label}_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)
    logger.info("Results saved to %s", out_path)
    return out_path


def _print_summary_table(result_dict: dict) -> None:
    from rich.console import Console
    from rich.table import Table
    table = Table(title="Unified Backtest Summary")
    table.add_column("Metric", style="cyan", min_width=25)
    table.add_column("Value", justify="right", min_width=15)
    for key in ["total_trades", "win_rate", "profit_factor", "total_return_pct",
                "avg_trade_pnl", "max_drawdown_pct", "sharpe_ratio", "sortino_ratio",
                "max_consecutive_losses", "expectancy"]:
        val = result_dict.get(key, 0)
        if isinstance(val, float):
            table.add_row(key.replace("_", " ").title(), f"{val:.2f}")
        else:
            table.add_row(key.replace("_", " ").title(), str(val))
    Console().print(table)


def _try_generate_report(result, report_dir: str) -> None:
    try:
        from src.backtesting.report_generator import BacktestReportGenerator
        Path(report_dir).mkdir(parents=True, exist_ok=True)
        gen = BacktestReportGenerator()
        out = gen.generate_single_report(result, f"{report_dir}/unified_report.html")
        logger.info("HTML report generated: %s", out)
    except ImportError:
        logger.warning("report_generator not available; skipping HTML report")
    except Exception as exc:
        logger.warning("Report generation failed: %s", exc)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Unified Backtest CLI -- M15 + M5 on shared account")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--initial-capital", type=float, default=100.0)
    p.add_argument("--m15-strategies", default=DEFAULT_M15, help="Comma-separated M15 strategies")
    p.add_argument("--m5-strategies", default=DEFAULT_M5, help="Comma-separated M5 strategies")
    p.add_argument("--csv-m5", help="Path to M5 CSV (auto-detect from cache if omitted)")
    p.add_argument("--csv-m15", help="Path to M15 CSV (resample from M5 if omitted)")
    p.add_argument("--csv-h1", help="Path to H1 CSV (resample from M15 if omitted)")
    p.add_argument("--enable-costs", action="store_true", default=True)
    p.add_argument("--no-costs", action="store_true", help="Disable spread/slippage costs")
    p.add_argument("--profit-growth", type=float, default=0.50, help="Profit growth factor")
    p.add_argument("--max-positions", type=int, default=10, help="Max concurrent positions")
    p.add_argument("--risk-pct", type=float, default=1.0, help="Risk %% per trade")
    p.add_argument("--report", action="store_true", help="Generate HTML report")
    p.add_argument("--report-dir", default="reports/")
    p.add_argument("--label", default="", help="Label for output file naming")
    return p


def main() -> None:
    args = _build_parser().parse_args()
    enable_costs = not args.no_costs
    label = args.label or "unified"

    # -- Load config for M15 strategy params --------------------------------
    try:
        cfg = load_config()
    except Exception as exc:
        logger.warning("Config load failed (%s), using defaults", exc)
        cfg = None

    # -- Instantiate strategies ---------------------------------------------
    m15_strats = _instantiate_strategies(args.m15_strategies, M15_REGISTRY, cfg, is_m15=True)
    m5_strats = _instantiate_strategies(args.m5_strategies, M5_REGISTRY)
    all_strategies = m15_strats + m5_strats
    if not all_strategies:
        sys.exit("ERROR: No strategies could be instantiated")
    logger.info("Loaded %d M15 + %d M5 strategies (%d total)",
                len(m15_strats), len(m5_strats), len(all_strategies))

    # -- Load / auto-detect CSV data ----------------------------------------
    csv_m5 = args.csv_m5 or _find_csv(args.symbol, "M5")
    if csv_m5 is None:
        sys.exit("ERROR: No M5 CSV found. Pass --csv-m5 or place file in data/backtest_cache/")
    logger.info("Loading M5 data from %s", csv_m5)
    m5_data = load_from_csv(csv_m5)

    if args.csv_m15:
        m15_data = load_from_csv(args.csv_m15)
    else:
        found = _find_csv(args.symbol, "M15")
        m15_data = load_from_csv(found) if found else _resample_m5_to_m15(m5_data)

    if args.csv_h1:
        h1_data = load_from_csv(args.csv_h1)
    else:
        found = _find_csv(args.symbol, "H1")
        h1_data = load_from_csv(found) if found else resample_m15_to_h1(m15_data)

    # -- Build engine -------------------------------------------------------
    cost_model = CostModel(session_manager=SessionManager()) if enable_costs else None
    engine = UnifiedBacktestEngine(
        symbol=args.symbol,
        m15_strategies=m15_strats,
        m5_strategies=m5_strats,
        initial_capital=args.initial_capital,
        cost_model=cost_model,
        risk_pct=args.risk_pct,
        max_total_positions=args.max_positions,
        profit_growth_factor=args.profit_growth,
    )

    # -- Run ----------------------------------------------------------------
    result = engine.run(m5_data=m5_data, m15_data=m15_data, h1_data=h1_data)
    print(result.summary())

    # -- Output -------------------------------------------------------------
    rd = _result_to_dict(result)
    rd["m15_strategies"] = args.m15_strategies
    rd["m5_strategies"] = args.m5_strategies
    _print_summary_table(rd)
    _save_results(rd, args.symbol, label)

    if args.report:
        _try_generate_report(result, args.report_dir)


if __name__ == "__main__":
    main()
