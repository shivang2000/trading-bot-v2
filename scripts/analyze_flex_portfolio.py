#!/usr/bin/env python3
"""Portfolio-level FundingPips Flex pass-probability Monte Carlo.

Merges multiple backtest trade streams (different strategies/symbols) into
one calendar-aligned daily P&L stream — cross-leg correlation is preserved
by summing P&L on the same historical date — then bootstraps whole days.

Each leg: a result JSON from backtest_scalping.py (trades with close_time
and pnl) plus a risk multiplier (pnl scale vs the risk it was backtested at).

Usage:
  python3 scripts/analyze_flex_portfolio.py --portfolio "name=US30amd:file=...json:scale=2,ORBSPEC:scale=2" ...
Simpler: edit PORTFOLIOS below and run with no args.
"""

from __future__ import annotations

import glob
import json
import random
import statistics
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, "scripts")
sys.path.insert(0, ".")

ACCOUNT = 5000.0
SIMS = 3000
SEED = 42

# ── Leg sources: latest breadth JSONs (backtested at 0.5% risk, $10k) ──
# scale=1.0 means "as backtested" = 0.5% of 10k = $50 avg loss ≈ 1% of 5k.
# On the $5k account: scale 0.5 -> 0.5% risk, 1.0 -> 1%, 1.5 -> 1.5%, 2 -> 2%.
def latest(pattern: str) -> str:
    files = sorted(glob.glob(pattern))
    if not files:
        raise SystemExit(f"no match: {pattern}")
    return files[-1]

LEGS = {
    # fresh-window streams (data current to 2026-08-12)
    "us30_amd":   "data/backtest_results/scalp_US30_fresh_m5_amd_cycle_*.json",
    "xau_amd":    "data/backtest_results/scalp_XAUUSD_fresh_m5_amd_cycle_*.json",
    "xau_liq":    "data/backtest_results/scalp_XAUUSD_fresh_m5_liquidity_sweep_*.json",
    "btc_amd":    "data/backtest_results/scalp_BTCUSD_fresh_m5_amd_cycle_*.json",
    "eth_amd":    "data/backtest_results/scalp_ETHUSD_fresh_m5_amd_cycle_*.json",
    "eth_mtf":    "data/backtest_results/scalp_ETHUSD_fresh_m5_mtf_momentum_*.json",
    "eth_keltner": "data/backtest_results/scalp_ETHUSD_fresh_m5_keltner_squeeze_*.json",
}

# ORB spec leg: regenerate trades from the validated spec replay.
def orb_spec_daily() -> dict[str, float]:
    import importlib.util
    import pandas as pd
    spec = importlib.util.spec_from_file_location("orbspec", "scripts/backtest_ny_orb_spec.py")
    orb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(orb)
    df = pd.read_csv("data/backtest_cache/fresh/US30_M5_fresh.csv")
    df["time"] = pd.to_datetime(df["time"], utc=True)
    trades = orb.run_backtest(df, lot=0.10, spread_pts=2.0)
    # normalize: avg loss at lot 0.10 is ~$140 -> rescale so avg loss = $50
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    base = abs(statistics.mean(losses))
    k = 50.0 / base
    out: dict[str, float] = defaultdict(float)
    for t in trades:
        out[str(t.get("date"))[:10]] += t["pnl"] * k
    return dict(out)


def leg_daily(path: str) -> dict[str, float]:
    with open(path) as fh:
        d = json.load(fh)
    out: dict[str, float] = defaultdict(float)
    for t in d.get("trades", []):
        day = (t.get("close_time") or t.get("open_time") or "")[:10]
        out[day] += float(t.get("pnl", 0.0))
    return dict(out)


# ── Portfolios: {leg: scale} where scale 1.0 ≈ 1% risk on $5k ──
PORTFOLIOS_V3 = {
    "LIVE-NOW amd@1+orb@.5+xauamd@.5+liq@.5": {"us30_amd": 1.0, "ORB": 0.5, "xau_amd": 0.5, "xau_liq": 0.5},
    "V3a amd@1+xauamd@.5 (drop orb+liq)":     {"us30_amd": 1.0, "xau_amd": 0.5},
    "V3b V3a + ethmtf@.5":                    {"us30_amd": 1.0, "xau_amd": 0.5, "eth_mtf": 0.5},
    "V3c V3b + btcamd@.5":                    {"us30_amd": 1.0, "xau_amd": 0.5, "eth_mtf": 0.5, "btc_amd": 0.5},
    "V3d V3c + ethamd@.5":                    {"us30_amd": 1.0, "xau_amd": 0.5, "eth_mtf": 0.5, "btc_amd": 0.5, "eth_amd": 0.5},
    "V3e V3d + orb@.5":                       {"us30_amd": 1.0, "xau_amd": 0.5, "eth_mtf": 0.5, "btc_amd": 0.5, "eth_amd": 0.5, "ORB": 0.5},
    "V3f AMD-everywhere @1/.5/.5/.5":         {"us30_amd": 1.0, "xau_amd": 0.5, "btc_amd": 0.5, "eth_amd": 0.5},
    "V3g V3d full-size crypto":               {"us30_amd": 1.0, "xau_amd": 0.5, "eth_mtf": 1.0, "btc_amd": 1.0, "eth_amd": 1.0},
    "V3h V3d + ethkeltner@.25":               {"us30_amd": 1.0, "xau_amd": 0.5, "eth_mtf": 0.5, "btc_amd": 0.5, "eth_amd": 0.5, "eth_keltner": 0.25},
}
PORTFOLIOS_OLD = {
    "BASELINE us30_amd@1 + orb@0.5":          {"us30_amd": 1.0, "ORB": 0.5},
    "A us30_amd@1 + orb@0.5 + keltner@0.5":   {"us30_amd": 1.0, "ORB": 0.5, "xau_keltner": 0.5},
    "B us30_amd@1 + orb@0.5 + keltner@1":     {"us30_amd": 1.0, "ORB": 0.5, "xau_keltner": 1.0},
    "C us30_amd@1 + orb@0.5 + mtf@1":         {"us30_amd": 1.0, "ORB": 0.5, "xau_mtf": 1.0},
    "D us30_amd@1 + orb@0.5 + xau_amd@1":     {"us30_amd": 1.0, "ORB": 0.5, "xau_amd": 1.0},
    "E amd@1 + orb@0.5 + keltner@0.5 + mtf@0.5": {"us30_amd": 1.0, "ORB": 0.5, "xau_keltner": 0.5, "xau_mtf": 0.5},
    "F amd@1.5 + orb@0.5 + keltner@0.5":      {"us30_amd": 1.5, "ORB": 0.5, "xau_keltner": 0.5},
    "G amd@1.5 + orb@1 + mtf@1":              {"us30_amd": 1.5, "ORB": 1.0, "xau_mtf": 1.0},
    "H AGGR amd@2 + orb@1 + keltner@1":       {"us30_amd": 2.0, "ORB": 1.0, "xau_keltner": 1.0},
    "I AGGR amd@2 + orb@1 + keltner@1 + mtf@1": {"us30_amd": 2.0, "ORB": 1.0, "xau_keltner": 1.0, "xau_mtf": 1.0},
    "J gold-only keltner@1 + mtf@1":          {"xau_keltner": 1.0, "xau_mtf": 1.0},
    "K amd@1 + orb@0.5 + liq@1":              {"us30_amd": 1.0, "ORB": 0.5, "xau_liq": 1.0},
    "L amd@1 + orb@0.5 + fvg@1":              {"us30_amd": 1.0, "ORB": 0.5, "xau_fvg": 1.0},
    "M amd@1 + orb@0.5 + keltner@0.5 + liq@0.5": {"us30_amd": 1.0, "ORB": 0.5, "xau_keltner": 0.5, "xau_liq": 0.5},
    "N MAX amd@2 + orb@1.5 + keltner@1.5 + mtf@1.5": {"us30_amd": 2.0, "ORB": 1.5, "xau_keltner": 1.5, "xau_mtf": 1.5},
    "O amd@1 + orb@0.5 + liq@1 + xau_amd@1":  {"us30_amd": 1.0, "ORB": 0.5, "xau_liq": 1.0, "xau_amd": 1.0},
    "P amd@1 + orb@0.5 + liq@0.5 + xau_amd@0.5": {"us30_amd": 1.0, "ORB": 0.5, "xau_liq": 0.5, "xau_amd": 0.5},
    "Q amd@1.5 + orb@0.5 + liq@1 + xau_amd@1": {"us30_amd": 1.5, "ORB": 0.5, "xau_liq": 1.0, "xau_amd": 1.0},
    "R amd@1 + orb@0.5 + liq@1 + m30rsi2@0.5": {"us30_amd": 1.0, "ORB": 0.5, "xau_liq": 1.0, "xau_m30rsi2": 0.5},
}
PORTFOLIOS = PORTFOLIOS_V3


def simulate(day_pool: list[list[tuple[str, float]]], account: float,
             target_pct: float, rng: random.Random,
             daily_pct: float = 4.0, max_dd_pct: float = 12.0,
             max_days: int = 500) -> tuple[str, int]:
    equity = account
    floor = account * (1 - max_dd_pct / 100)
    target = account * (1 + target_pct / 100)
    daily_limit = account * daily_pct / 100
    for day_i in range(max_days):
        day = rng.choice(day_pool)
        day_pnl = 0.0
        for _, pnl in day:
            day_pnl += pnl
            equity += pnl
            if equity <= floor:
                return "breach_dd", day_i + 1
            if day_pnl <= -daily_limit:
                return "breach_daily", day_i + 1
        if equity >= target:
            return "pass", day_i + 1
    return "stalled", max_days


def main() -> None:
    # Load all leg daily maps once
    daily: dict[str, dict[str, float]] = {}
    for k, pat in LEGS.items():
        daily[k] = leg_daily(latest(pat))
    daily["ORB"] = orb_spec_daily()

    hdr = (f"{'PORTFOLIO':<46} {'P(S1)':>6} {'P(both)':>7} {'brDay':>6} "
           f"{'brDD':>6} {'stall':>6} {'d→fund':>7} {'cal':>6} {'tr-days/wk':>10}")
    print(f"Account ${ACCOUNT:.0f} | Flex: S1 +10%, S2 +6%, daily 4%, max 12% | {SIMS} sims")
    print(hdr)
    print("─" * len(hdr))

    rows = []
    for name, legs in PORTFOLIOS.items():
        # calendar-aligned merge over union of dates (overlap window only:
        # require date >= max(first dates) and <= min(last dates) across legs)
        firsts, lasts = [], []
        for leg in legs:
            days = sorted(daily[leg])
            firsts.append(days[0]); lasts.append(days[-1])
        lo, hi = max(firsts), min(lasts)
        merged: dict[str, list[tuple[str, float]]] = defaultdict(list)
        for leg, scale in legs.items():
            for day, pnl in daily[leg].items():
                if lo <= day <= hi:
                    merged[day].append((leg, pnl * scale))
        if not merged:
            print(f"{name:<46} NO OVERLAP ({lo}..{hi})")
            continue
        pool = [merged[d] for d in sorted(merged)]
        span_days = (datetime.fromisoformat(hi) - datetime.fromisoformat(lo)).days or 1
        cal_ratio = span_days / len(pool)

        rng = random.Random(SEED)
        res = {"pass": 0, "breach_daily": 0, "breach_dd": 0, "stalled": 0}
        both = 0
        dt: list[int] = []
        for _ in range(SIMS):
            o1, d1 = simulate(pool, ACCOUNT, 10.0, rng)
            res[o1] += 1
            if o1 == "pass":
                o2, d2 = simulate(pool, ACCOUNT, 6.0, rng)
                if o2 == "pass":
                    both += 1
                    dt.append(d1 + d2)
        md = statistics.median(dt) if dt else None
        cal = round(md * cal_ratio) if md else None
        tdw = len(pool) / (span_days / 7)
        rows.append((name, res["pass"]/SIMS, both/SIMS, res["breach_daily"]/SIMS,
                     res["breach_dd"]/SIMS, res["stalled"]/SIMS, md, cal, tdw))

    rows.sort(key=lambda r: -r[2])
    for r in rows:
        print(f"{r[0]:<46} {r[1]*100:>5.1f}% {r[2]*100:>6.1f}% {r[3]*100:>5.1f}% "
              f"{r[4]*100:>5.1f}% {r[5]*100:>5.1f}% {str(r[6] or '—'):>7} "
              f"{str(r[7] or '—'):>5}d {r[8]:>9.1f}")

    print("\nLegend: scale 1.0 ≈ 1% risk on $5k (streams backtested at 0.5%/$10k). "
          "cal = median calendar days to funded. tr-days/wk = days with ≥1 trade per week.")


if __name__ == "__main__":
    main()
