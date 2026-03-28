#!/usr/bin/env python3
"""Trade Analysis -- P&L by hour, day of week, session.

Usage:
    python scripts/analyze_trades.py --results "data/backtest_results/scalp_*.json"
    python scripts/analyze_trades.py --results data/backtest_results/specific.json --output analysis.json
"""
from __future__ import annotations

import argparse, glob, json, logging, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)-8s] %(message)s")
logger = logging.getLogger("analyze_trades")

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SESSIONS = {"Asian": (0, 8), "London": (8, 13), "Overlap": (13, 17), "NY": (17, 22)}
_B = lambda: {"count": 0, "pnl": 0.0, "wins": 0}


def load_trades(path: str) -> list[dict]:
    """Load trades from a backtest result JSON (single or comparison format)."""
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, dict) and "strategies" in data:
        trades = []
        for s in data["strategies"]:
            name = s.get("strategy", "unknown")
            for t in s.get("trades", []):
                t["strategy"] = name
                trades.append(t)
        return trades
    if isinstance(data, dict):
        name = data.get("strategy", "unknown")
        for t in data.get("trades", []):
            t.setdefault("strategy", name)
        return data.get("trades", [])
    return []


def _parse_time(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        if "T" in str(raw):
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S%z")
    except (ValueError, TypeError):
        try:
            return datetime.strptime(str(raw), "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return None


def _session(hour: int) -> str:
    for name, (lo, hi) in SESSIONS.items():
        if lo <= hour < hi:
            return name
    return "Off-hours"


def analyze(trades: list[dict]) -> dict:
    """Analyze trades by hour, day, session, and strategy."""
    groups = {k: defaultdict(_B) for k in ("by_hour", "by_day", "by_session", "by_strategy")}
    for t in trades:
        dt = _parse_time(t.get("open_time", ""))
        if dt is None:
            continue
        pnl, win = float(t.get("pnl", 0)), float(t.get("pnl", 0)) > 0
        for gk, key in [("by_hour", dt.hour), ("by_day", DAYS[dt.weekday()]),
                         ("by_session", _session(dt.hour)),
                         ("by_strategy", t.get("strategy", "unknown"))]:
            groups[gk][key]["count"] += 1
            groups[gk][key]["pnl"] += pnl
            if win:
                groups[gk][key]["wins"] += 1
    return {k: dict(v) for k, v in groups.items()}


def print_analysis(a: dict) -> None:
    """Print analysis tables using rich."""
    from rich.console import Console
    from rich.table import Table
    console = Console()

    def _fmt(d: dict) -> tuple[str, str, str, str]:
        wr = (d["wins"] / d["count"] * 100) if d["count"] else 0
        avg = d["pnl"] / d["count"] if d["count"] else 0
        c = "green" if d["pnl"] > 0 else "red"
        return str(d["count"]), f"[{c}]${d['pnl']:+.2f}[/{c}]", f"{wr:.1f}%", f"${avg:+.3f}"

    def _table(title: str, cols: list[str], rows: list[tuple]) -> None:
        t = Table(title=title)
        for col in cols:
            t.add_column(col, justify="left" if col == cols[0] else "right")
        for row in rows:
            t.add_row(*row)
        console.print(t)

    _table("P&L by Hour (UTC)", ["Hour", "Trades", "P&L", "Win Rate", "Avg PnL"],
           [(f"{h:02d}:00", *_fmt(a["by_hour"][h])) for h in sorted(a["by_hour"])])
    _table("P&L by Day of Week", ["Day", "Trades", "P&L", "Win Rate"],
           [(d, *_fmt(a["by_day"].get(d, _B()))[:3]) for d in DAYS[:5]])
    _table("P&L by Session", ["Session", "Trades", "P&L", "Win Rate"],
           [(s, *_fmt(a["by_session"].get(s, _B()))[:3]) for s in [*SESSIONS, "Off-hours"]])
    _table("P&L by Strategy", ["Strategy", "Trades", "P&L", "Win Rate", "Avg PnL"],
           [(n, *_fmt(d)) for n, d in sorted(a["by_strategy"].items(), key=lambda x: x[1]["pnl"], reverse=True)])


def main() -> None:
    ap = argparse.ArgumentParser(description="Trade Analysis by Time")
    ap.add_argument("--results", required=True, help="Glob pattern or path for JSON result files")
    ap.add_argument("--output", help="Save analysis JSON to this path")
    args = ap.parse_args()

    files = sorted(glob.glob(args.results))
    if not files:
        logger.error("No files matched: %s", args.results)
        sys.exit(1)

    all_trades: list[dict] = []
    for fp in files:
        trades = load_trades(fp)
        all_trades.extend(trades)
        logger.info("Loaded %d trades from %s", len(trades), fp)

    logger.info("Total trades: %d", len(all_trades))
    analysis = analyze(all_trades)
    print_analysis(analysis)

    if args.output:
        with open(args.output, "w") as f:
            json.dump(analysis, f, indent=2, default=str)
        logger.info("Saved to %s", args.output)


if __name__ == "__main__":
    main()
