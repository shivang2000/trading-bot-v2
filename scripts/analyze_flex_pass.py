#!/usr/bin/env python3
"""FundingPips 2-Step Flex pass-probability analysis via Monte Carlo.

Consumes backtest result JSONs (from backtest_scalping.py) and estimates,
by day-level bootstrap of the historical trade stream:

  - P(pass Step 1)  (+10% target, 4% daily loss, 12% max loss, no time limit)
  - P(pass Step 2)  (+6% target, same limits)
  - P(pass both) and expected calendar days to funded
  - Trade cadence (trades/week) and avg $ per winning trade at account scale

Day-level bootstrap preserves intra-day clustering (daily-loss rule operates
on days, not trades). No-time-limit means a sim only ends on target or breach;
we cap at `--max-days` sim days and count overruns as "stalled".

Usage:
  python3 scripts/analyze_flex_pass.py data/backtest_results/scalp_US30_flex1_*.json \
      --account 10000 [--sims 5000] [--json-out out.json]
"""

from __future__ import annotations

import argparse
import glob
import json
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime


def load_day_pnls(path: str) -> tuple[list[list[float]], dict]:
    """Return (list of per-day trade-pnl lists, meta) from a result JSON."""
    with open(path) as fh:
        d = json.load(fh)
    trades = d.get("trades", [])
    by_day: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        ct = t.get("close_time") or t.get("open_time") or ""
        day = ct[:10]
        by_day[day].append(float(t.get("pnl", 0.0)))
    days = [by_day[k] for k in sorted(by_day)]
    # Calendar span for cadence: trading days vs calendar weeks
    span_days = 0
    try:
        t0 = datetime.fromisoformat(d["start_date"].split("+")[0])
        t1 = datetime.fromisoformat(d["end_date"].split("+")[0])
        span_days = max((t1 - t0).days, 1)
    except Exception:
        pass
    meta = {
        "file": path,
        "strategy": d.get("strategy", "?"),
        "symbol": d.get("symbol", "?"),
        "initial_capital": d.get("initial_capital", 0),
        "total_trades": d.get("total_trades", len(trades)),
        "win_rate": d.get("win_rate", 0),
        "profit_factor": d.get("profit_factor", 0),
        "total_return_pct": d.get("total_return_pct", 0),
        "max_drawdown_pct": d.get("max_drawdown_pct", 0),
        "span_days": span_days,
        "n_trading_days": len(days),
        "wins": [p for day in days for p in day if p > 0],
        "losses": [p for day in days for p in day if p <= 0],
    }
    return days, meta


def simulate_phase(
    day_pool: list[list[float]],
    account: float,
    target_pct: float,
    daily_pct: float = 4.0,
    max_dd_pct: float = 12.0,
    max_days: int = 365,
    rng: random.Random | None = None,
    scale: float = 1.0,
) -> tuple[str, int]:
    """One phase sim. Returns (outcome, sim_trading_days).

    outcome: 'pass' | 'breach_daily' | 'breach_dd' | 'stalled'
    scale: multiply historical pnl (risk re-scaling).
    """
    rng = rng or random
    equity = account
    floor = account * (1 - max_dd_pct / 100)
    target = account * (1 + target_pct / 100)
    daily_limit = account * daily_pct / 100

    for day_i in range(max_days):
        day = rng.choice(day_pool)
        day_pnl = 0.0
        for pnl in day:
            pnl *= scale
            day_pnl += pnl
            equity += pnl
            if equity <= floor:
                return "breach_dd", day_i + 1
            if day_pnl <= -daily_limit:
                # PropFirmGuard would stop before the hard limit intraday;
                # counting it as a breach is the conservative assumption.
                return "breach_daily", day_i + 1
        if equity >= target:
            return "pass", day_i + 1
    return "stalled", max_days


def analyze(path: str, account: float, sims: int, seed: int, scale: float,
            max_days: int) -> dict | None:
    days, meta = load_day_pnls(path)
    if len(days) < 20:
        return None
    rng = random.Random(seed)

    results = {"pass": 0, "breach_daily": 0, "breach_dd": 0, "stalled": 0}
    both_pass = 0
    days_to_funded: list[int] = []
    s1_days: list[int] = []

    for _ in range(sims):
        o1, d1 = simulate_phase(days, account, 10.0, rng=rng, scale=scale,
                                max_days=max_days)
        results[o1] += 1
        if o1 == "pass":
            s1_days.append(d1)
            o2, d2 = simulate_phase(days, account, 6.0, rng=rng, scale=scale,
                                    max_days=max_days)
            if o2 == "pass":
                both_pass += 1
                days_to_funded.append(d1 + d2)

    p1 = results["pass"] / sims
    pboth = both_pass / sims
    wins, losses = meta["wins"], meta["losses"]
    # trades/week from historical stream
    tpw = meta["total_trades"] / max(meta["span_days"] / 7.0, 1e-9)

    return {
        **{k: meta[k] for k in ("file", "strategy", "symbol", "win_rate",
                                 "profit_factor", "total_return_pct",
                                 "max_drawdown_pct", "total_trades",
                                 "span_days", "n_trading_days")},
        "scale": scale,
        "p_pass_step1": round(p1, 4),
        "p_pass_both": round(pboth, 4),
        "p_breach_daily": round(results["breach_daily"] / sims, 4),
        "p_breach_dd": round(results["breach_dd"] / sims, 4),
        "p_stalled": round(results["stalled"] / sims, 4),
        "median_days_step1": statistics.median(s1_days) if s1_days else None,
        "median_days_funded": statistics.median(days_to_funded) if days_to_funded else None,
        "trades_per_week": round(tpw, 2),
        "avg_win_usd": round(statistics.mean(wins), 2) if wins else 0.0,
        "avg_loss_usd": round(statistics.mean(losses), 2) if losses else 0.0,
        "expected_attempts_to_fund": round(1 / pboth, 1) if pboth > 0 else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("patterns", nargs="+", help="Result JSON paths or globs")
    ap.add_argument("--account", type=float, default=10000.0)
    ap.add_argument("--sims", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--scale", type=float, default=1.0,
                    help="Multiply historical trade PnL (risk re-scaling)")
    ap.add_argument("--max-days", type=int, default=365)
    ap.add_argument("--json-out", help="Write full results JSON here")
    args = ap.parse_args()

    paths: list[str] = []
    for p in args.patterns:
        paths.extend(sorted(glob.glob(p)))
    if not paths:
        sys.exit("No result files matched")

    rows = []
    for path in paths:
        try:
            r = analyze(path, args.account, args.sims, args.seed, args.scale,
                        args.max_days)
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR {path}: {exc}", file=sys.stderr)
            continue
        if r:
            rows.append(r)

    rows.sort(key=lambda r: -(r["p_pass_both"] or 0))
    hdr = (f"{'STRATEGY':<38} {'SYM':<7} {'P(S1)':>6} {'P(both)':>7} "
           f"{'brDay':>6} {'brDD':>6} {'stall':>6} {'d→fund':>7} "
           f"{'tr/wk':>6} {'avgW$':>8} {'avgL$':>8}")
    print(hdr)
    print("─" * len(hdr))
    for r in rows:
        print(f"{r['strategy'][:38]:<38} {r['symbol']:<7} "
              f"{r['p_pass_step1']*100:>5.1f}% {r['p_pass_both']*100:>6.1f}% "
              f"{r['p_breach_daily']*100:>5.1f}% {r['p_breach_dd']*100:>5.1f}% "
              f"{r['p_stalled']*100:>5.1f}% "
              f"{str(r['median_days_funded'] or '—'):>7} "
              f"{r['trades_per_week']:>6.2f} {r['avg_win_usd']:>8.2f} "
              f"{r['avg_loss_usd']:>8.2f}")

    if args.json_out:
        with open(args.json_out, "w") as fh:
            json.dump(rows, fh, indent=2)
        print(f"\nWrote {args.json_out}")


if __name__ == "__main__":
    main()
