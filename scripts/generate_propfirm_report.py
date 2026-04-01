#!/usr/bin/env python3
"""Generate comprehensive HTML report for FundingPips prop firm backtesting analysis.

Reads all backtest JSON files and produces an interactive HTML report with:
- Strategy × Risk performance matrices per account size ($5k, $10k, $100k)
- Optimal risk recommendations
- Monthly income projections (80% profit split)
- Scaling roadmap: $5k → $10k → $100k
"""

import json
import glob
import os
import re
from datetime import datetime
from pathlib import Path

RESULT_DIR = "data/backtest_results"
OUTPUT = "reports/propfirm_analysis.html"

ACCOUNTS = [5000, 10000, 100000]
RISKS = [0.25, 0.50, 1.0, 2.0]
STRATEGIES = ["mtf", "keltner", "dst"]
STRATEGY_NAMES = {"mtf": "MTF Momentum", "keltner": "Keltner Squeeze", "dst": "Dual Supertrend"}


def analyze(filepath: str) -> dict | None:
    with open(filepath) as f:
        d = json.load(f)
    trades = d.get("trades", [])
    if not trades:
        return None
    acct = d["initial_capital"]
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] < 0]
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 1
    rr = avg_win / avg_loss if avg_loss else 0

    equity = acct
    min_eq = acct
    max_eq = acct
    monthly = {}
    days_5pct = None
    days_10pct = None

    try:
        start_dt = datetime.fromisoformat(d["start_date"].split("+")[0])
        for t in trades:
            equity += t.get("pnl", 0)
            if equity < min_eq:
                min_eq = equity
            if equity > max_eq:
                max_eq = equity
            try:
                ct = datetime.fromisoformat(t["close_time"].split("+")[0])
                if equity >= acct * 1.05 and days_5pct is None:
                    days_5pct = (ct - start_dt).days
                if equity >= acct * 1.10 and days_10pct is None:
                    days_10pct = (ct - start_dt).days
                mk = ct.strftime("%Y-%m")
                monthly[mk] = monthly.get(mk, 0) + t.get("pnl", 0)
            except Exception:
                pass
    except Exception:
        pass

    avg_mo = sum(monthly.values()) / len(monthly) if monthly else 0
    worst = abs(d.get("worst_trade_pnl", 0))
    dd_floor = acct * 0.90

    return {
        "ret": d["total_return_pct"],
        "dd": d["max_drawdown_pct"],
        "wr": d["win_rate"],
        "trades": len(trades),
        "rr": rr,
        "final_eq": equity,
        "min_eq": min_eq,
        "max_eq": max_eq,
        "avg_mo": avg_mo,
        "split_mo": avg_mo * 0.80,
        "split_yr": avg_mo * 0.80 * 12,
        "days_5pct": days_5pct,
        "days_10pct": days_10pct,
        "worst_pct": worst / acct * 100,
        "floor_ok": min_eq >= dd_floor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
    }


def _build_index() -> dict:
    """Scan all backtest files and build an index by (account, strategy_key)."""
    index = {}
    strat_map = {
        "M5MtfMomentumStrategy": "mtf",
        "M5KeltnerSqueezeStrategy": "keltner",
        "M5DualSupertrendStrategy": "dst",
    }
    for fp in glob.glob(os.path.join(RESULT_DIR, "scalp_XAUUSD_*.json")):
        try:
            with open(fp) as f:
                d = json.load(f)
            acct = d.get("initial_capital", 0)
            strat_raw = d.get("strategy", "")
            # Map strategy name
            skey = None
            for full, short in strat_map.items():
                if full in strat_raw:
                    skey = short
                    break
            if not skey or acct == 0:
                continue
            key = (int(acct), skey, fp)
            index[key] = fp
        except Exception:
            continue
    return index


_INDEX: dict = {}


def find_result(acct: int, risk: float, strat: str) -> dict | None:
    """Find the best matching backtest result file by checking labels and content."""
    global _INDEX
    if not _INDEX:
        _INDEX = _build_index()

    acct_k = f"{acct // 1000}k"
    # Build multiple risk label variants
    risk_labels = []
    if risk == 0.25:
        risk_labels = ["025", "025pct", "0.25"]
    elif risk == 0.5:
        risk_labels = ["050", "05pct", "0.5", "05"]
    elif risk == 1.0:
        risk_labels = ["100", "1pct", "1.0", "r1"]
    elif risk == 2.0:
        risk_labels = ["200", "2pct", "2.0"]

    # Try filename patterns with all risk label variants
    patterns = []
    for rl in risk_labels:
        patterns.append(f"scalp_XAUUSD_matrix_{acct_k}_{rl}_{strat}_*.json")
        patterns.append(f"scalp_XAUUSD_{acct_k}_{rl}_master_{strat}_*.json")
        patterns.append(f"scalp_XAUUSD_{acct_k}_{rl}pct_master_{strat}_*.json")

    # Special patterns for v4 runs ($5k only)
    if acct == 5000:
        strat_file = {"mtf": "m5_mtf_momentum", "keltner": "m5_keltner_squeeze", "dst": "m5_dual_supertrend"}
        sf = strat_file.get(strat, "")
        if risk == 1.0:
            patterns.append(f"scalp_XAUUSD_v4r1_{sf}_*.json")
        elif risk == 2.0:
            patterns.append(f"scalp_XAUUSD_v4_{sf}_*.json")

    # Special patterns for $100k at 2% (no risk prefix)
    if acct == 100000 and risk == 2.0:
        patterns.append(f"scalp_XAUUSD_100k_master_{strat}_*.json")

    for pat in patterns:
        files = sorted(glob.glob(os.path.join(RESULT_DIR, pat)))
        for fp in files:
            try:
                with open(fp) as fh:
                    d = json.load(fh)
                if d.get("initial_capital", 0) == acct:
                    result = analyze(fp)
                    if result:
                        return result
            except Exception:
                continue
    return None


def cell_color(r: dict | None) -> str:
    if r is None:
        return "#333"
    if r["ret"] <= 0:
        return "#4a1a1a"  # dark red
    if not r["floor_ok"]:
        return "#4a1a1a"
    if r["dd"] > 15:
        return "#4a3a1a"  # dark yellow
    return "#1a3a1a"  # dark green


def fmt_money(v: float) -> str:
    if abs(v) >= 1000:
        return f"${v:,.0f}"
    return f"${v:.0f}"


def generate_html(data: dict) -> str:
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>FundingPips Prop Firm — Backtesting Analysis</title>
<style>
  body { background: #0a0a0a; color: #e0e0e0; font-family: 'Segoe UI', system-ui, sans-serif; margin: 20px; }
  h1 { color: #00d4aa; text-align: center; font-size: 28px; }
  h2 { color: #00b4d8; border-bottom: 1px solid #333; padding-bottom: 8px; margin-top: 40px; }
  h3 { color: #90e0ef; }
  table { border-collapse: collapse; width: 100%; margin: 15px 0; }
  th { background: #1a1a2e; color: #00d4aa; padding: 10px; text-align: center; border: 1px solid #333; }
  td { padding: 8px 12px; border: 1px solid #333; text-align: center; font-size: 13px; }
  .green { background: #1a3a1a; }
  .yellow { background: #4a3a1a; }
  .red { background: #4a1a1a; }
  .gray { background: #1a1a1a; color: #666; }
  .best { border: 2px solid #00d4aa !important; }
  .metric { font-size: 11px; color: #888; }
  .highlight { color: #00d4aa; font-weight: bold; }
  .warn { color: #ffaa00; }
  .bad { color: #ff4444; }
  .section { background: #111; border-radius: 8px; padding: 20px; margin: 20px 0; }
  .roadmap { background: #0d1117; border: 1px solid #30363d; border-radius: 8px; padding: 20px; }
  .roadmap-step { padding: 10px 0; border-left: 3px solid #00d4aa; padding-left: 15px; margin: 10px 0; }
  .roadmap-step.future { border-left-color: #333; }
  .roadmap-step.done { border-left-color: #00ff88; }
  .summary-box { display: inline-block; background: #1a1a2e; border-radius: 8px; padding: 15px 25px; margin: 10px; text-align: center; }
  .summary-box .value { font-size: 24px; color: #00d4aa; font-weight: bold; }
  .summary-box .label { font-size: 12px; color: #888; }
</style>
</head>
<body>
<h1>FundingPips Prop Firm — Backtesting Analysis Report</h1>
<p style="text-align:center;color:#888;">Generated: """ + datetime.now().strftime("%Y-%m-%d %H:%M") + """ | Data: Oct 2024 — Apr 2026 (18 months M5 XAUUSD)</p>
"""

    # Summary boxes
    best_5k = data.get("best", {}).get(5000, {})
    best_10k = data.get("best", {}).get(10000, {})
    best_100k = data.get("best", {}).get(100000, {})

    html += '<div style="text-align:center;margin:30px 0;">'
    for acct, best in [(5000, best_5k), (10000, best_10k), (100000, best_100k)]:
        mo = best.get("split_mo", 0)
        yr = best.get("split_yr", 0)
        risk = best.get("risk", "?")
        html += f"""<div class="summary-box">
            <div class="label">${acct:,} Funded (80% split)</div>
            <div class="value">{fmt_money(mo)}/mo</div>
            <div class="metric">{fmt_money(yr)}/yr at {risk}% risk</div>
        </div>"""
    html += "</div>"

    # Per-account matrices
    for acct in ACCOUNTS:
        acct_k = f"${acct:,}"
        floor = acct * 0.90
        max_risk = 2.0 if acct >= 50000 else 3.0
        html += f'<div class="section"><h2>{acct_k} Account — Strategy × Risk Matrix</h2>'
        html += f'<p class="metric">DD Floor: ${floor:,.0f} | Daily Limit: ${acct*0.05:,.0f} | Max Risk/Trade: {max_risk}%</p>'

        html += "<table><tr><th>Strategy</th>"
        for risk in RISKS:
            if risk > max_risk:
                continue
            html += f"<th>{risk}%</th>"
        html += "<th>Best</th></tr>"

        best_for_acct = {"split_mo": -999999}
        for strat in STRATEGIES:
            html += f"<tr><td><strong>{STRATEGY_NAMES[strat]}</strong></td>"
            best_cell = None
            for risk in RISKS:
                if risk > max_risk:
                    continue
                r = data.get("results", {}).get((acct, risk, strat))
                if r is None:
                    html += '<td class="gray">No data</td>'
                    continue

                color_class = "green" if r["ret"] > 0 and r["floor_ok"] and r["dd"] <= 15 else (
                    "yellow" if r["ret"] > 0 and r["floor_ok"] else "red"
                )
                wk5 = f'{r["days_5pct"]/7:.0f}wk' if r["days_5pct"] else "N/A"

                is_best = False
                if r["ret"] > 0 and r["floor_ok"] and (best_cell is None or r["split_mo"] > best_cell["split_mo"]):
                    best_cell = {**r, "risk": risk}
                    is_best = True

                best_cls = " best" if is_best else ""
                html += f'<td class="{color_class}{best_cls}">'
                html += f'<strong>{r["ret"]:.1f}%</strong><br>'
                html += f'<span class="metric">DD: {r["dd"]:.1f}% | WR: {r["wr"]:.1f}%</span><br>'
                html += f'<span class="metric">R:R 1:{r["rr"]:.1f} | {r["trades"]} trades</span><br>'
                html += f'<span class="{"highlight" if r["split_mo"] > 0 else "bad"}">{fmt_money(r["split_mo"])}/mo</span><br>'
                html += f'<span class="metric">5% in {wk5} | Floor: {"OK" if r["floor_ok"] else "BREACH"}</span>'
                html += "</td>"

            # Best column
            if best_cell:
                html += f'<td class="green best"><strong>{best_cell["risk"]}% risk</strong><br>'
                html += f'<span class="highlight">{fmt_money(best_cell["split_mo"])}/mo</span><br>'
                html += f'<span class="metric">{fmt_money(best_cell["split_yr"])}/yr</span></td>'
                if best_cell["split_mo"] > best_for_acct["split_mo"]:
                    best_for_acct = {**best_cell, "risk": best_cell["risk"]}
            else:
                html += '<td class="gray">None profitable</td>'

            html += "</tr>"

        html += "</table>"

        # Recommendation
        if best_for_acct.get("split_mo", 0) > 0:
            html += f'<h3>Recommendation for {acct_k}: {best_for_acct["risk"]}% risk</h3>'
            html += f'<p>Monthly income (80% split): <span class="highlight">{fmt_money(best_for_acct["split_mo"])}/month ({fmt_money(best_for_acct["split_yr"])}/year)</span></p>'
            data.setdefault("best", {})[acct] = best_for_acct

        html += "</div>"

    # EMA Pullback + London Breakout note
    html += """<div class="section">
    <h2>Additional Strategies (Live Trading Only)</h2>
    <table>
    <tr><th>Strategy</th><th>Evidence</th><th>Status</th><th>Income Contribution</th></tr>
    <tr class="green"><td><strong>EMA Pullback</strong> (M15, 4 instruments)</td>
        <td>$30 → $376 live (12.5x in 10 days)</td>
        <td>Deployed, proven</td>
        <td>Additional income stream (not backtestable)</td></tr>
    <tr class="green"><td><strong>London Breakout</strong> (M15, 4 instruments)</td>
        <td>Part of $30 → $376 live results</td>
        <td>Deployed, proven</td>
        <td>Additional income stream (not backtestable)</td></tr>
    </table>
    <p class="metric">These strategies run on all 4 instruments (XAUUSD, XAGUSD, BTCUSD, ETHUSD) and add extra trades beyond the M5 scalping strategies above.</p>
    </div>"""

    # Scaling Roadmap
    b5 = data.get("best", {}).get(5000, {})
    b10 = data.get("best", {}).get(10000, {})
    b100 = data.get("best", {}).get(100000, {})

    html += """<div class="section"><h2>Scaling Roadmap: $5k → $10k → $100k</h2><div class="roadmap">"""
    html += f"""
    <div class="roadmap-step done">
        <strong>Step 1: $5k Challenge — PASSED ✓</strong><br>
        <span class="metric">10% target ($500) reached on Day 1 via manual trading. Waiting 3-day minimum.</span>
    </div>
    <div class="roadmap-step">
        <strong>Step 2: $5k Challenge (5% target = $250)</strong><br>
        <span class="metric">Bot trades at {b5.get('risk','1')}% risk. Projected time: {b5.get('days_5pct',0)//7 if b5.get('days_5pct') else '?'} weeks.</span>
    </div>
    <div class="roadmap-step">
        <strong>$5k Funded — Earning Phase</strong><br>
        <span class="metric">Monthly income: {fmt_money(b5.get('split_mo',0))}/month at 80% split.</span>
    </div>
    <div class="roadmap-step future">
        <strong>Scale to $10k Challenge</strong><br>
        <span class="metric">Buy $10k challenge (~$60) from funded profits. Pass Step 1+2.</span>
    </div>
    <div class="roadmap-step future">
        <strong>$10k Funded — Earning Phase</strong><br>
        <span class="metric">Monthly income: {fmt_money(b10.get('split_mo',0))}/month at 80% split.</span>
    </div>
    <div class="roadmap-step future">
        <strong>Scale to $100k Challenge</strong><br>
        <span class="metric">Buy $100k challenge (~$500) from funded profits. Pass Step 1+2.</span>
    </div>
    <div class="roadmap-step future">
        <strong>$100k Funded — Target Income</strong><br>
        <span class="highlight" style="font-size:18px;">{fmt_money(b100.get('split_mo',0))}/month ({fmt_money(b100.get('split_yr',0))}/year)</span>
    </div>
    """
    html += "</div></div>"

    # Methodology
    html += """<div class="section">
    <h2>Methodology</h2>
    <ul>
    <li><strong>Data:</strong> XAUUSD M5 bars, Oct 2024 — Apr 2026 (18 months, 101k bars)</li>
    <li><strong>Engine:</strong> Prop firm backtest with PropFirmGuard ($7 safety buffers), spread/slippage costs enabled</li>
    <li><strong>Compounding:</strong> 50% profit growth factor (profits partially reinvested into position sizing)</li>
    <li><strong>Income:</strong> 80% profit split (FundingPips funded account terms)</li>
    <li><strong>Floor test:</strong> Equity must never drop below 90% of initial balance</li>
    <li><strong>Limitation:</strong> EMA Pullback and London Breakout not in backtest engine (live results only)</li>
    <li><strong>Limitation:</strong> DST high returns at 1% risk include extreme compounding — real results will be lower</li>
    </ul></div>"""

    html += "</body></html>"
    return html


def main():
    # Collect all results
    data = {"results": {}, "best": {}}

    for acct in ACCOUNTS:
        for risk in RISKS:
            for strat in STRATEGIES:
                r = find_result(acct, risk, strat)
                if r:
                    data["results"][(acct, risk, strat)] = r

    print(f"Found {len(data['results'])} backtest results")

    # Generate HTML
    html = generate_html(data)

    # Write report
    os.makedirs("reports", exist_ok=True)
    with open(OUTPUT, "w") as f:
        f.write(html)

    print(f"Report written to {OUTPUT}")
    print(f"Open: file://{os.path.abspath(OUTPUT)}")


if __name__ == "__main__":
    main()
