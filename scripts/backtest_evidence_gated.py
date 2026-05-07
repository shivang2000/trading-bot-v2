"""CLI wrapper: walk-forward + DSR validation for new XAUUSD strategies.

Currently supports the bar-replay path (TickReplayEngine + a strategy
adapter that emits TradeRecord objects). Tick CSVs are read from
data/ticks/dukascopy/xauusd/{YYYY}/{MM}/*.csv.gz produced by
download_dukascopy_xauusd_ticks.py.

Usage:
    python3 scripts/backtest_evidence_gated.py \
        --strategy ny_orb \
        --years 2018-2025 \
        --tick-root data/ticks/dukascopy/xauusd \
        --report-html reports/ny_orb_walkforward.html

Status: SCAFFOLD with end-to-end tick → strategy → metrics plumbing
deferred to its own task. This entry point validates argument parsing,
discovers tick files, runs walk-forward against any TradeRecord stream
the caller plugs in.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.backtesting.walk_forward_validator import (  # noqa: E402
    TradeRecord,
    WalkForwardValidator,
    deflated_sharpe_ratio,
)

logger = logging.getLogger(__name__)


def discover_tick_files(tick_root: Path, years: tuple[int, int]) -> list[Path]:
    """Return all tick CSVs for [start_year, end_year]."""
    files: list[Path] = []
    for year in range(years[0], years[1] + 1):
        year_dir = tick_root / f"{year:04d}"
        if not year_dir.is_dir():
            continue
        files.extend(sorted(year_dir.rglob("*.csv.gz")))
    return files


def load_trades_from_jsonl(path: Path) -> list[TradeRecord]:
    """Load pre-computed trade records from a JSONL file.

    Each line: {"closed_at": "ISO8601", "pnl": float}

    This decouples the validator entry point from any specific strategy
    implementation — caller runs their strategy, dumps trades to JSONL,
    then runs this script to walk-forward + DSR them.
    """
    out: list[TradeRecord] = []
    with path.open() as fh:
        for line in fh:
            row = json.loads(line)
            ts_raw = row["closed_at"]
            if ts_raw.endswith("Z"):
                ts_raw = ts_raw[:-1] + "+00:00"
            out.append(TradeRecord(closed_at=datetime.fromisoformat(ts_raw), pnl=float(row["pnl"])))
    return out


def render_report_html(
    result,
    title: str,
    out_path: Path,
    gate_passed: bool,
    gate_failures: list[str],
) -> None:
    """Emit a self-contained HTML report (no external deps).

    Uses inline-styled tables. Renders monthly P&L heatmap as a tiny <svg>
    with cells per (year, month). Equity-curve chart kept in scope for a
    follow-up that adds matplotlib pre-rendered PNG embedding; for now we
    print the per-window table which is the most actionable artefact.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows_html = "\n".join(
        f"<tr><td>{w.test_start.date()}</td><td>{w.test_end.date()}</td>"
        f"<td>{w.trade_count}</td>"
        f"<td>{w.win_rate * 100:.1f}%</td>"
        f"<td>{w.profit_factor:.2f}</td>"
        f"<td>{w.sharpe_annual:.2f}</td>"
        f"<td>{w.max_drawdown_pct:.1f}%</td>"
        f"<td>${w.total_pnl:.2f}</td></tr>"
        for w in result.windows
    )
    gate_color = "#0a7d2c" if gate_passed else "#b00020"
    gate_text = "PASS" if gate_passed else "FAIL: " + ", ".join(gate_failures)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; max-width: 1100px; margin: 2rem auto; padding: 0 1rem; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
th, td {{ border: 1px solid #ccc; padding: 0.4rem 0.6rem; text-align: right; }}
th {{ background: #f4f4f4; }}
td:first-child, td:nth-child(2) {{ text-align: left; }}
.summary {{ background: #fafafa; padding: 1rem; border-radius: 4px; margin: 1rem 0; }}
.summary dt {{ font-weight: 600; }}
.summary dl {{ display: grid; grid-template-columns: max-content 1fr; gap: 0.4rem 1.5rem; }}
.gate {{ font-size: 1.4rem; font-weight: 700; padding: 0.6rem 1rem; border-radius: 4px; color: white; background: {gate_color}; display: inline-block; }}
</style></head>
<body>
<h1>{title}</h1>
<p class="gate">Gate: {gate_text}</p>
<div class="summary"><dl>
  <dt>Aggregate trades</dt><dd>{result.aggregate_trade_count} {'✓ ≥ 385' if result.sample_sufficient else '✗ < 385 (warning)'}</dd>
  <dt>Aggregate win rate</dt><dd>{result.aggregate_win_rate * 100:.2f}%</dd>
  <dt>Aggregate profit factor</dt><dd>{result.aggregate_profit_factor:.3f}</dd>
  <dt>Aggregate Sharpe (per-trade)</dt><dd>{result.aggregate_sharpe:.3f}</dd>
  <dt>Aggregate Sharpe (annualised)</dt><dd>{result.aggregate_sharpe_annual:.3f}</dd>
  <dt>Aggregate max drawdown</dt><dd>{result.aggregate_max_dd_pct:.2f}%</dd>
  <dt>Aggregate total P&amp;L</dt><dd>${result.aggregate_total_pnl:.2f}</dd>
  <dt>Deflated Sharpe Ratio</dt><dd><strong>{result.deflated_sharpe_ratio:.4f}</strong> (probability the edge is real)</dd>
</dl></div>
<h2>Per-window test metrics</h2>
<table>
<thead><tr><th>Test start</th><th>Test end</th><th>Trades</th><th>WR</th><th>PF</th><th>Sharpe (ann.)</th><th>Max DD</th><th>P&amp;L</th></tr></thead>
<tbody>{rows_html}</tbody>
</table>
</body></html>
"""
    out_path.write_text(html, encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Walk-forward validator + DSR for new XAUUSD strategies.")
    p.add_argument(
        "--strategy",
        choices=["ny_orb", "pullback_window", "both"],
        default="ny_orb",
        help="Which strategy to validate.",
    )
    p.add_argument("--years", required=True, help="UTC year range, e.g. 2018-2025")
    p.add_argument("--tick-root", default="data/ticks/dukascopy/xauusd")
    p.add_argument(
        "--trades-jsonl",
        default=None,
        help="Optional pre-computed trade JSONL (skip tick replay; use for rerunning DSR after strategy emits trades elsewhere)",
    )
    p.add_argument("--train-months", type=int, default=12)
    p.add_argument("--test-months", type=int, default=3)
    p.add_argument("--num-trials", type=int, default=1, help="Implicit parameter-sweep count for DSR adjustment")
    p.add_argument("--report-html", default=None, help="Output HTML report path")
    p.add_argument("--min-dsr", type=float, default=0.5)
    p.add_argument("--min-trades", type=int, default=385)
    p.add_argument("--min-pf", type=float, default=1.3)
    p.add_argument("--max-dd-pct", type=float, default=8.0)
    p.add_argument("--log-level", default="INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    yrs = args.years.split("-")
    if len(yrs) != 2 or not all(y.isdigit() for y in yrs):
        print("--years must be YYYY-YYYY", file=sys.stderr)
        return 2
    year_range = (int(yrs[0]), int(yrs[1]))

    tick_root = Path(args.tick_root)
    files = discover_tick_files(tick_root, year_range)
    print(f"Discovered {len(files)} tick file(s) under {tick_root} for {year_range[0]}-{year_range[1]}")

    if args.trades_jsonl:
        trades = load_trades_from_jsonl(Path(args.trades_jsonl))
        print(f"Loaded {len(trades)} pre-computed trades from {args.trades_jsonl}")
    elif args.strategy in ("ny_orb", "pullback_window") and files:
        import asyncio as _asyncio

        if args.strategy == "ny_orb":
            from src.analysis.strategies.xauusd_ny_orb_tick_breakout import (  # noqa: E402
                XauusdNyOrbConfig,
            )
            from src.backtesting.ny_orb_replay_adapter import run_ny_orb_replay  # noqa: E402
            cfg = XauusdNyOrbConfig(enabled=True)
            replay_fn = run_ny_orb_replay
        else:
            from src.analysis.strategies.xauusd_pullback_window_state_machine import (  # noqa: E402
                XauusdPullbackWindowConfig,
            )
            from src.backtesting.pullback_window_replay_adapter import run_pullback_replay  # noqa: E402
            cfg = XauusdPullbackWindowConfig(enabled=True)
            replay_fn = run_pullback_replay

        print(f"Running {args.strategy} tick-replay over {len(files)} files...")
        replay = _asyncio.run(replay_fn(files, config=cfg))
        print(
            f"Replay done: {replay.ticks_processed:,} ticks, {replay.orders_received} orders, "
            f"{len(replay.closed_trades)} closed trades, P&L ${replay.final_pnl:.2f}"
        )
        out_path = Path(f"reports/{args.strategy}_{args.years}_trades.jsonl")
        replay.write_jsonl(out_path)
        print(f"Trades JSONL written: {out_path}")
        trades = [
            TradeRecord(closed_at=t.closed_at, pnl=t.pnl) for t in replay.closed_trades
        ]
    else:
        print("No --trades-jsonl and strategy adapter not available for this combo.")
        trades = []

    if not trades:
        print("No trades to validate. Exiting.")
        return 0

    history_start = min(t.closed_at for t in trades)
    history_end = max(t.closed_at for t in trades)

    validator = WalkForwardValidator(
        train_months=args.train_months, test_months=args.test_months,
    )
    result = validator.validate(trades, history_start, history_end, num_trials=args.num_trials)

    passed, failures = result.passes_gate(
        min_dsr=args.min_dsr, min_trades=args.min_trades,
        min_pf=args.min_pf, max_dd_pct=args.max_dd_pct,
    )

    print(f"\nWalk-forward result for strategy={args.strategy} years={args.years}")
    print(f"  windows:        {len(result.windows)}")
    print(f"  trades:         {result.aggregate_trade_count}")
    print(f"  WR:             {result.aggregate_win_rate * 100:.1f}%")
    print(f"  PF:             {result.aggregate_profit_factor:.2f}")
    print(f"  Sharpe (ann):   {result.aggregate_sharpe_annual:.2f}")
    print(f"  Max DD:         {result.aggregate_max_dd_pct:.2f}%")
    print(f"  DSR:            {result.deflated_sharpe_ratio:.4f}  (gate >= {args.min_dsr})")
    print(f"  Gate:           {'PASS' if passed else 'FAIL ' + str(failures)}")

    if args.report_html:
        render_report_html(
            result,
            title=f"Walk-forward report — {args.strategy} {args.years}",
            out_path=Path(args.report_html),
            gate_passed=passed,
            gate_failures=failures,
        )
        print(f"\nReport written: {args.report_html}")

    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
