#!/usr/bin/env python3
"""Faithful re-validation of the NY ORB spec (docs/pinescripts/ny_orb.pine).

This is the deploy gate for enabling m5_ny_orb on US30: it implements the
EXACT validated logic — range 14:30-15:00 UTC, entry on the first M5 CLOSE
beyond the range (after 15:00, before 21:00), SL = opposite side of the range,
TP = range_high + 1.5*width (long) / range_low - 1.5*width (short), ONE trade
per day, force-flat at 21:00 UTC. No RSI filter, no retrace, no regime gate.

Conservative fill model: full round-trip spread charged on entry; if a bar
touches both SL and TP, SL wins.

Usage:
    python scripts/backtest_ny_orb_spec.py --csv data/backtest_cache/US30_M5_all.csv \
        --lot 0.10 --spread-points 2.0 --report reports/us30_ny_orb_spec.html
"""
from __future__ import annotations

import argparse
import json
from datetime import time as dtime
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RANGE_START = dtime(14, 30)
RANGE_END = dtime(15, 0)      # exclusive
SESSION_END = dtime(21, 0)    # force flat
DOLLARS_PER_INDEX_POINT_PER_LOT = 10.0   # US30: point_size 0.01, tick_value $0.1 → $10/index-pt/1.0 lot


def run_backtest(df: pd.DataFrame, lot: float, spread_pts: float,
                 usd_per_point_per_lot: float = DOLLARS_PER_INDEX_POINT_PER_LOT) -> list[dict]:
    """Replay the ORB spec day by day. Returns one dict per closed trade."""
    df = df.copy()
    df["date"] = df["time"].dt.date
    df["tod"] = df["time"].dt.time
    usd_per_pt = usd_per_point_per_lot * lot
    cost = spread_pts * usd_per_pt  # full round-trip spread, charged once

    trades: list[dict] = []
    for date, day in df.groupby("date", sort=True):
        rng = day[(day["tod"] >= RANGE_START) & (day["tod"] < RANGE_END)]
        if len(rng) < 4:  # incomplete range window (holiday/short session)
            continue
        rh, rl = float(rng["high"].max()), float(rng["low"].min())
        w = rh - rl
        if w <= 0:
            continue

        sess = day[(day["tod"] >= RANGE_END) & (day["tod"] < SESSION_END)]
        pos = None  # (side, entry, sl, tp, entry_time)
        for row in sess.itertuples():
            c, h, lo = float(row.close), float(row.high), float(row.low)
            if pos is None:
                if c > rh:
                    pos = ("BUY", c, rl, rh + 1.5 * w, row.time)
                elif c < rl:
                    pos = ("SELL", c, rh, rl - 1.5 * w, row.time)
                continue

            side, entry, sl, tp, et = pos
            exit_px = None
            reason = None
            if side == "BUY":
                if lo <= sl:                      # SL first when both touched
                    exit_px, reason = sl, "SL"
                elif h >= tp:
                    exit_px, reason = tp, "TP"
            else:
                if h >= sl:
                    exit_px, reason = sl, "SL"
                elif lo <= tp:
                    exit_px, reason = tp, "TP"
            if exit_px is not None:
                pnl = (exit_px - entry) if side == "BUY" else (entry - exit_px)
                trades.append({
                    "date": str(date), "side": side, "entry": entry,
                    "exit": exit_px, "reason": reason,
                    "pnl": pnl * usd_per_pt - cost,
                    "entry_time": str(et), "exit_time": str(row.time),
                })
                pos = ("DONE",)  # one trade/day — block re-entry
                break

        # EOD flat at 21:00 (position still open at session end)
        if pos is not None and pos[0] in ("BUY", "SELL"):
            side, entry, sl, tp, et = pos
            last = sess.iloc[-1]
            exit_px = float(last["close"])
            pnl = (exit_px - entry) if side == "BUY" else (entry - exit_px)
            trades.append({
                "date": str(date), "side": side, "entry": entry,
                "exit": exit_px, "reason": "EOD",
                "pnl": pnl * usd_per_pt - cost,
                "entry_time": str(et), "exit_time": str(last["time"]),
            })
    return trades


def stats(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_w = sum(wins)
    gross_l = abs(sum(losses))
    eq, peak, maxdd = 0.0, 0.0, 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        maxdd = max(maxdd, peak - eq)
    # daily P&L (one trade/day so ≈ per-trade, but group anyway)
    daily: dict[str, float] = {}
    for t in trades:
        daily[t["date"]] = daily.get(t["date"], 0.0) + t["pnl"]
    dvals = sorted(daily.values())
    return {
        "trades": len(trades),
        "net_pnl": round(sum(pnls), 2),
        "profit_factor": round(gross_w / gross_l, 3) if gross_l else float("inf"),
        "win_rate_pct": round(100 * len(wins) / len(pnls), 1),
        "avg_trade": round(sum(pnls) / len(pnls), 2),
        "max_drawdown": round(maxdd, 2),
        "trading_days": len(daily),
        "avg_daily_pnl": round(sum(dvals) / len(dvals), 2),
        "median_daily_pnl": round(dvals[len(dvals) // 2], 2),
        "best_day": round(dvals[-1], 2),
        "worst_day": round(dvals[0], 2),
        "pct_days_positive": round(100 * sum(1 for v in dvals if v > 0) / len(dvals), 1),
    }


def period_split(trades: list[dict], n: int = 3) -> list[dict]:
    """Equal-count splits for robustness (matches the original 3-period check)."""
    out = []
    k = len(trades) // n
    for i in range(n):
        chunk = trades[i * k: (i + 1) * k if i < n - 1 else len(trades)]
        s = stats(chunk)
        s["period"] = f"P{i + 1} ({chunk[0]['date']} → {chunk[-1]['date']})" if chunk else f"P{i + 1}"
        out.append(s)
    return out


def html_report(symbol: str, lot: float, spread_pts: float, trades: list[dict],
                s: dict, periods: list[dict], spread3x: dict, path: str) -> None:
    eq, labels, series = 0.0, [], []
    for t in trades:
        eq += t["pnl"]
        labels.append(t["date"])
        series.append(round(eq, 2))
    monthly: dict[str, float] = {}
    for t in trades:
        monthly[t["date"][:7]] = monthly.get(t["date"][:7], 0.0) + t["pnl"]
    mrows = "".join(
        f"<tr><td>{m}</td><td style='color:{'#16a34a' if v >= 0 else '#dc2626'}'>{v:+,.2f}</td></tr>"
        for m, v in sorted(monthly.items())
    )
    prow = "".join(
        f"<tr><td>{p.get('period','')}</td><td>{p.get('trades',0)}</td><td>{p.get('profit_factor','-')}</td>"
        f"<td>{p.get('win_rate_pct','-')}%</td><td>{p.get('net_pnl',0):+,.2f}</td></tr>"
        for p in periods
    )
    cards = "".join(
        f"<div class='card'><div class='k'>{k}</div><div class='v'>{v}</div></div>"
        for k, v in [
            ("Trades", s["trades"]), ("Profit Factor", s["profit_factor"]),
            ("Win Rate", f"{s['win_rate_pct']}%"), ("Net P&L", f"${s['net_pnl']:+,.2f}"),
            ("Max DD", f"${s['max_drawdown']:,.2f}"), ("Avg Trade", f"${s['avg_trade']:+,.2f}"),
            ("Avg Daily P&L", f"${s['avg_daily_pnl']:+,.2f}"), ("Days Positive", f"{s['pct_days_positive']}%"),
            ("3x Spread PF", spread3x.get("profit_factor", "-")),
        ]
    )
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>{symbol} NY ORB spec validation</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
body{{font-family:-apple-system,sans-serif;margin:24px;background:#0f172a;color:#e2e8f0}}
h1{{font-size:20px}} .grid{{display:flex;flex-wrap:wrap;gap:12px;margin:16px 0}}
.card{{background:#1e293b;border-radius:10px;padding:12px 18px;min-width:120px}}
.k{{font-size:11px;color:#94a3b8;text-transform:uppercase}} .v{{font-size:20px;font-weight:600}}
table{{border-collapse:collapse;margin:12px 0}} td,th{{padding:6px 14px;border-bottom:1px solid #334155;font-size:13px;text-align:left}}
.wrap{{display:flex;gap:32px;flex-wrap:wrap}}
</style></head><body>
<h1>{symbol} — NY ORB (validated spec) · lot {lot} · spread {spread_pts} pts r/t</h1>
<p>Range 14:30–15:00 UTC · M5 close-beyond entry · SL opposite side · TP 1.5×width · 1 trade/day · flat 21:00 UTC · SL-wins-ties fill model</p>
<div class="grid">{cards}</div>
<canvas id="eq" height="80"></canvas>
<div class="wrap">
<div><h3>3-period robustness</h3><table><tr><th>Period</th><th>Trades</th><th>PF</th><th>WR</th><th>Net</th></tr>{prow}</table></div>
<div><h3>Monthly P&L ($)</h3><table><tr><th>Month</th><th>P&L</th></tr>{mrows}</table></div>
</div>
<script>
new Chart(document.getElementById('eq'),{{type:'line',data:{{labels:{json.dumps(labels)},
datasets:[{{label:'Equity ($, cumulative)',data:{json.dumps(series)},borderColor:'#38bdf8',pointRadius:0,borderWidth:1.5,fill:false}}]}},
options:{{plugins:{{legend:{{labels:{{color:'#e2e8f0'}}}}}},scales:{{x:{{ticks:{{color:'#94a3b8',maxTicksLimit:14}}}},y:{{ticks:{{color:'#94a3b8'}}}}}}}}}});
</script></body></html>"""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(html)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/backtest_cache/US30_M5_all.csv")
    ap.add_argument("--symbol", default="US30")
    ap.add_argument("--lot", type=float, default=0.10)
    ap.add_argument("--spread-points", type=float, default=2.0,
                    help="round-trip spread in PRICE units of the instrument")
    ap.add_argument("--usd-per-point", type=float, default=DOLLARS_PER_INDEX_POINT_PER_LOT,
                    help="$ per 1.0 price-unit move per 1.0 lot (US30=10, XAUUSD=100)")
    ap.add_argument("--report", default="reports/us30_ny_orb_spec.html")
    args = ap.parse_args()

    from src.backtesting.data_loader import load_from_csv
    df = load_from_csv(args.csv)

    trades = run_backtest(df, args.lot, args.spread_points, args.usd_per_point)
    s = stats(trades)
    periods = period_split(trades)
    spread3x = stats(run_backtest(df, args.lot, args.spread_points * 3, args.usd_per_point))

    print(f"\n{args.symbol} NY ORB spec — lot {args.lot}, spread {args.spread_points} pts r/t")
    print(json.dumps(s, indent=2))
    print("\n3-period PF:", [p.get("profit_factor") for p in periods])
    print(f"3x spread ({args.spread_points * 3} pts): PF {spread3x.get('profit_factor')}, "
          f"net ${spread3x.get('net_pnl')}")

    html_report(args.symbol, args.lot, args.spread_points, trades, s, periods, spread3x, args.report)
    print(f"\nHTML report: {args.report}")


if __name__ == "__main__":
    main()
