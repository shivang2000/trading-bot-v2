#!/usr/bin/env python3
"""Generate HTML reports from backtest results.

Usage:
    python scripts/generate_report.py --results "data/backtest_results/XAUUSD_*.json" --type comparison --open
    python scripts/generate_report.py --results data/backtest_results/specific.json --type single --open
    python scripts/generate_report.py --results "data/backtest_results/*.json" --output reports/
    python scripts/generate_report.py --type master --open
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtesting.report_generator import BacktestReportGenerator
from src.backtesting.result import BacktestResult
from src.core.enums import OrderSide
from src.core.models import Trade

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)-30s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("generate_report")


def _parse_datetime(value: str) -> datetime:
    """Parse a datetime string from JSON, handling multiple formats."""
    for fmt in (
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S.%f%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    # Last resort: fromisoformat
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _load_result(filepath: Path) -> BacktestResult:
    """Load a BacktestResult from a saved JSON file.

    Reconstructs Trade objects from the JSON trade dicts produced by
    scripts/backtest.py.
    """
    with open(filepath, encoding="utf-8") as f:
        data = json.load(f)

    start_date = _parse_datetime(data["start_date"])
    end_date = _parse_datetime(data["end_date"])
    initial_capital = data.get("initial_capital", 10000.0)
    final_equity = data.get("final_equity", initial_capital)

    # Reconstruct trades
    trades: list[Trade] = []
    for td in data.get("trades", []):
        side_str = td.get("side", "BUY")
        try:
            side = OrderSide(side_str)
        except ValueError:
            side = OrderSide.BUY

        pnl_val = td.get("pnl", 0.0)

        trade = Trade(
            ticket=td.get("ticket", 0),
            symbol=data.get("symbol", "UNKNOWN"),
            side=side,
            volume=td.get("volume", 0.01),
            open_price=td.get("open_price", 0.0),
            close_price=td.get("close_price", 0.0),
            open_time=_parse_datetime(str(td.get("open_time", str(start_date)))),
            close_time=_parse_datetime(str(td.get("close_time", str(end_date)))),
            profit=pnl_val,
            commission=0.0,
            swap=0.0,
            close_reason=td.get("close_reason", ""),
        )
        trades.append(trade)

    # Compute final equity from trades if not in JSON
    if "final_equity" not in data and trades:
        final_equity = initial_capital + sum(t.pnl for t in trades)

    result = BacktestResult(
        strategy_name=data.get("strategy", filepath.stem),
        symbol=data.get("symbol", "UNKNOWN"),
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        final_equity=final_equity,
        trades=trades,
        total_return_pct=data.get("total_return_pct", 0.0),
        max_drawdown_pct=data.get("max_drawdown_pct", 0.0),
        win_rate=data.get("win_rate", 0.0),
        profit_factor=data.get("profit_factor", 0.0),
        total_trades=data.get("total_trades", len(trades)),
        avg_trade_pnl=data.get("avg_trade_pnl", 0.0),
        sharpe_ratio=data.get("sharpe_ratio", 0.0),
        sortino_ratio=data.get("sortino_ratio", 0.0),
        max_consecutive_losses=data.get("max_consecutive_losses", 0),
        max_consecutive_wins=data.get("max_consecutive_wins", 0),
        avg_trade_duration_hours=data.get("avg_trade_duration_hours", 0.0),
        best_trade_pnl=data.get("best_trade_pnl", 0.0),
        worst_trade_pnl=data.get("worst_trade_pnl", 0.0),
        expectancy=data.get("expectancy", 0.0),
    )

    return result


def _open_in_browser(filepath: str) -> None:
    """Open a file in the default browser."""
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["open", filepath], check=True)
        elif system == "Linux":
            subprocess.run(["xdg-open", filepath], check=True)
        elif system == "Windows":
            subprocess.run(["start", filepath], shell=True, check=True)
        else:
            logger.warning("Cannot auto-open browser on %s", system)
    except (subprocess.SubprocessError, FileNotFoundError):
        logger.warning("Failed to open browser. Open manually: %s", filepath)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate HTML reports from backtest result JSON files."
    )
    parser.add_argument(
        "--results",
        default=None,
        help="Glob pattern for JSON result files (e.g. 'data/backtest_results/XAUUSD_*.json'). "
             "Not required when --type master (auto-globs data/backtest_results/).",
    )
    parser.add_argument(
        "--type",
        choices=["single", "comparison", "master"],
        default="comparison",
        help="Report type: single (one per file), comparison (all in one), or master (all-runs dashboard). Default: comparison",
    )
    parser.add_argument(
        "--output",
        default="reports",
        help="Output directory for HTML reports. Default: reports/",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        dest="open_browser",
        help="Auto-open the generated report in the default browser.",
    )
    args = parser.parse_args()

    # --results is required for single/comparison modes
    if args.type != "master" and not args.results:
        parser.error("--results is required for single and comparison report types")

    # For master reports, glob ALL JSON files in data/backtest_results/
    if args.type == "master":
        master_dir = Path("data/backtest_results")
        files = sorted(str(p) for p in master_dir.glob("*.json")) if master_dir.is_dir() else []
        if not files:
            logger.error("No JSON files found in %s", master_dir)
            sys.exit(1)
    else:
        files = sorted(glob.glob(args.results))
        if not files:
            logger.error("No files matched pattern: %s", args.results)
            sys.exit(1)

    logger.info("Found %d result file(s): %s", len(files), ", ".join(files))

    # Load all results — handle both single-run and comparison JSON formats
    results: list[BacktestResult] = []
    for fp in files:
        try:
            with open(fp, encoding="utf-8") as f:
                raw = json.load(f)

            # Comparison format: {"mode": "compare", "strategies": [...]}
            if isinstance(raw, dict) and raw.get("mode") == "compare":
                for entry in raw.get("strategies", []):
                    # Write each strategy entry to a temp-like dict for _load_result
                    import tempfile, os
                    tmp_path = Path(tempfile.mktemp(suffix=".json"))
                    with open(tmp_path, "w") as tf:
                        json.dump(entry, tf)
                    try:
                        result = _load_result(tmp_path)
                        results.append(result)
                        logger.info(
                            "  Loaded: %s (%s, %d trades, PF %.2f)",
                            result.strategy_name, result.symbol,
                            result.total_trades, result.profit_factor,
                        )
                    finally:
                        tmp_path.unlink(missing_ok=True)
            else:
                result = _load_result(Path(fp))
                results.append(result)
                logger.info(
                    "  Loaded: %s (%s, %d trades, PF %.2f)",
                    result.strategy_name, result.symbol,
                    result.total_trades, result.profit_factor,
                )
        except Exception as exc:
            logger.error("  Failed to load %s: %s", fp, exc)

    if not results:
        logger.error("No valid results loaded. Exiting.")
        sys.exit(1)

    generator = BacktestReportGenerator()
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    generated_paths: list[str] = []

    if args.type == "single":
        for r in results:
            safe_name = r.strategy_name.replace(" ", "_").replace("/", "_")
            out_path = output_dir / f"{r.symbol}_{safe_name}_{timestamp}.html"
            path = generator.generate_single_report(r, str(out_path))
            generated_paths.append(path)
            logger.info("Generated: %s", path)
    elif args.type == "master":
        out_path = output_dir / f"master_dashboard_{timestamp}.html"
        path = generator.generate_master_report(results, str(out_path))
        generated_paths.append(path)
        logger.info("Generated: %s", path)
    else:
        out_path = output_dir / f"comparison_{timestamp}.html"
        path = generator.generate_comparison_report(results, str(out_path))
        generated_paths.append(path)
        logger.info("Generated: %s", path)

    # Auto-open last generated report
    if args.open_browser and generated_paths:
        _open_in_browser(generated_paths[-1])


if __name__ == "__main__":
    main()
