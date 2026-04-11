#!/usr/bin/env python3
"""Generate comprehensive HTML report from 160 fullmatrix backtest results.

Matrix: 8 strategies × 5 accounts × 4 risks
Reads: data/backtest_results/scalp_XAUUSD_fullmatrix_{acct}_{risk}_{strategy}_*.json
Output: reports/propfirm_analysis.html
"""

import json
import glob
import os
from datetime import datetime

RESULT_DIR = "data/backtest_results"
OUTPUT = "reports/propfirm_analysis.html"

STRATEGIES = [
    ("m5_mtf_momentum", "MTF Momentum"),
    ("m5_keltner_squeeze", "Keltner Squeeze"),
    ("m5_dual_supertrend", "Dual Supertrend"),
    ("m5_box_theory", "Box Theory"),
    ("m5_amd_cycle", "AMD Cycle"),
    ("m5_stochrsi_adx", "StochRSI ADX"),
    ("ema_pullback", "EMA Pullback"),
    ("london_breakout", "London Breakout"),
]

ACCOUNTS = [
    ("50", 50, 0.50, "$50 Vantage Micro"),
    ("100", 100, 0.50, "$100 Vantage Micro"),
    ("5k", 5000, 0.50, "$5,000 FundingPips"),
    ("10k", 10000, 1.0, "$10,000 FundingPips"),
    ("100k", 100000, 5.0, "$100,000 FundingPips"),
]

RISKS = [
    ("025", 0.25, "0.25%"),
    ("05", 0.5, "0.5%"),
    ("10", 1.0, "1.0%"),
    ("20", 2.0, "2.0%"),
]


def analyze(filepath: str) -> dict | None:
    try:
        with open(filepath) as f:
            d = json.load(f)
    except Exception:
        return None
    trades = d.get("trades", [])
    if not trades:
        return {"ret": 0, "dd": 0, "wr": 0, "trades": 0, "rr": 0, "final_eq": d.get("initial_capital", 0),
                "min_eq": d.get("initial_capital", 0), "avg_mo": 0, "split_mo": 0, "split_yr": 0,
                "days_5": None, "days_10": None, "worst_pct": 0, "floor_ok": True, "avg_win": 0, "avg_loss": 0}

    acct = d["initial_capital"]
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] < 0]
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 1
    rr = avg_win / avg_loss if avg_loss else 0
    worst = abs(d.get("worst_trade_pnl", 0))

    equity = acct
    min_eq = acct
    monthly = {}
    days_5 = days_10 = None
    try:
        start_dt = datetime.fromisoformat(d["start_date"].split("+")[0])
        for t in trades:
            equity += t.get("pnl", 0)
            if equity < min_eq:
                min_eq = equity
            try:
                ct = datetime.fromisoformat(t["close_time"].split("+")[0])
                if equity >= acct * 1.05 and days_5 is None:
                    days_5 = (ct - start_dt).days
                if equity >= acct * 1.10 and days_10 is None:
                    days_10 = (ct - start_dt).days
                mk = ct.strftime("%Y-%m")
                monthly[mk] = monthly.get(mk, 0) + t.get("pnl", 0)
            except Exception:
                pass
    except Exception:
        pass

    avg_mo = sum(monthly.values()) / len(monthly) if monthly else 0
    return {
        "ret": d["total_return_pct"], "dd": d["max_drawdown_pct"], "wr": d["win_rate"],
        "trades": len(trades), "rr": rr, "final_eq": equity, "min_eq": min_eq,
        "avg_mo": avg_mo, "split_mo": avg_mo * 0.80, "split_yr": avg_mo * 0.80 * 12,
        "days_5": days_5, "days_10": days_10, "worst_pct": worst / acct * 100 if acct else 0,
        "floor_ok": min_eq >= acct * 0.90, "avg_win": avg_win, "avg_loss": avg_loss,
    }


def find_result(acct_label: str, risk_label: str, strat_key: str) -> dict | None:
    pattern = f"scalp_XAUUSD_fullmatrix_{acct_label}_{risk_label}_{strat_key}_*.json"
    files = sorted(glob.glob(os.path.join(RESULT_DIR, pattern)))
    if files:
        return analyze(files[-1])
    return None


FOREX_PAIRS = ["USDJPY", "GBPJPY", "NZDUSD", "GBPUSD", "EURUSD"]
FOREX_RISKS = [("025", 0.25, "0.25%"), ("05", 0.5, "0.5%"), ("10", 1.0, "1.0%"), ("20", 2.0, "2.0%")]


def find_forex_result(symbol: str, risk_label: str, strat_key: str) -> dict | None:
    pattern = f"scalp_{symbol}_forex_{symbol}_{risk_label}_{strat_key}_*.json"
    files = sorted(glob.glob(os.path.join(RESULT_DIR, pattern)))
    if files:
        return analyze(files[-1])
    return None


def cell_class(r: dict | None) -> str:
    if r is None or r["trades"] == 0:
        return "gray"
    if r["ret"] <= 0 or not r["floor_ok"]:
        return "red"
    if r["dd"] > 15:
        return "yellow"
    return "green"


def fmt_money(v: float) -> str:
    if abs(v) >= 1000:
        return f"${v:,.0f}"
    if abs(v) >= 1:
        return f"${v:.0f}"
    return f"${v:.2f}"


def generate_html(data: dict) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    total_results = sum(1 for v in data.values() if v is not None)

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<title>FundingPips Prop Firm — Complete Backtesting Report</title>
<style>
body {{ background:#0a0a0a; color:#e0e0e0; font-family:'Segoe UI',system-ui,sans-serif; margin:20px; }}
h1 {{ color:#00d4aa; text-align:center; font-size:26px; }}
h2 {{ color:#00b4d8; border-bottom:1px solid #333; padding-bottom:8px; margin-top:40px; }}
h3 {{ color:#90e0ef; margin-top:25px; }}
table {{ border-collapse:collapse; width:100%; margin:15px 0; font-size:12px; }}
th {{ background:#1a1a2e; color:#00d4aa; padding:8px; text-align:center; border:1px solid #333; }}
td {{ padding:6px 8px; border:1px solid #333; text-align:center; }}
.green {{ background:#1a3a1a; }}
.yellow {{ background:#4a3a1a; }}
.red {{ background:#4a1a1a; }}
.gray {{ background:#1a1a1a; color:#555; }}
.best {{ border:2px solid #00d4aa !important; }}
.metric {{ font-size:10px; color:#888; }}
.highlight {{ color:#00d4aa; font-weight:bold; }}
.bad {{ color:#ff4444; }}
.section {{ background:#111; border-radius:8px; padding:20px; margin:20px 0; }}
.summary-box {{ display:inline-block; background:#1a1a2e; border-radius:8px; padding:15px 25px; margin:8px; text-align:center; min-width:180px; }}
.summary-box .value {{ font-size:22px; color:#00d4aa; font-weight:bold; }}
.summary-box .label {{ font-size:11px; color:#888; }}
.roadmap {{ background:#0d1117; border:1px solid #30363d; border-radius:8px; padding:20px; }}
.step {{ padding:10px 0; border-left:3px solid #00d4aa; padding-left:15px; margin:10px 0; }}
.step.done {{ border-left-color:#00ff88; }}
.step.future {{ border-left-color:#333; }}
</style></head><body>
<h1>FundingPips Prop Firm — Complete Backtesting Report</h1>
<p style="text-align:center;color:#888;">Generated: {now} | {total_results}/160 results | 8 strategies × 5 accounts × 4 risks | Data: Oct 2024 — Apr 2026</p>
"""

    # Per-account matrices
    best_per_account = {}
    for acct_label, acct_val, max_lot, acct_name in ACCOUNTS:
        floor = acct_val * 0.90
        max_risk = 2.0 if acct_val >= 50000 else 3.0

        html += f'<div class="section"><h2>{acct_name}</h2>'
        html += f'<p class="metric">DD Floor: ${floor:,.0f} | Daily Limit: ${acct_val*0.05:,.0f} | Max Lot: {max_lot}</p>'

        html += "<table><tr><th>Strategy</th>"
        for _, _, risk_name in RISKS:
            html += f"<th>{risk_name}</th>"
        html += "<th>Best Config</th></tr>"

        acct_best = {"split_mo": -999999, "risk_name": "?", "strat_name": "?"}

        for strat_key, strat_name in STRATEGIES:
            html += f"<tr><td style='text-align:left'><strong>{strat_name}</strong></td>"
            row_best = None

            for risk_label, risk_val, risk_name in RISKS:
                r = data.get((acct_label, risk_label, strat_key))
                cls = cell_class(r)

                if r is None or r["trades"] == 0:
                    html += f'<td class="gray">No trades</td>'
                else:
                    wk5 = f'{r["days_5"]/7:.0f}wk' if r["days_5"] else "—"
                    is_best = False
                    if r["ret"] > 0 and r["floor_ok"] and (row_best is None or r["split_mo"] > row_best["split_mo"]):
                        row_best = {**r, "risk_name": risk_name, "risk_val": risk_val}
                        is_best = True

                    best_cls = " best" if is_best else ""
                    html += f'<td class="{cls}{best_cls}">'
                    html += f'<strong>{r["ret"]:.1f}%</strong><br>'
                    html += f'<span class="metric">DD:{r["dd"]:.1f}% WR:{r["wr"]:.0f}%</span><br>'
                    html += f'<span class="metric">R:R 1:{r["rr"]:.1f} #{r["trades"]}</span><br>'
                    html += f'<span class="{"highlight" if r["split_mo"]>0 else "bad"}">{fmt_money(r["split_mo"])}/mo</span><br>'
                    html += f'<span class="metric">5%:{wk5} F:{"✓" if r["floor_ok"] else "✗"}</span>'
                    html += "</td>"

            # Best column for this strategy
            if row_best and row_best["split_mo"] > 0:
                html += f'<td class="green best"><strong>{row_best["risk_name"]}</strong><br>'
                html += f'<span class="highlight">{fmt_money(row_best["split_mo"])}/mo</span><br>'
                html += f'<span class="metric">{fmt_money(row_best["split_yr"])}/yr</span></td>'
                if row_best["split_mo"] > acct_best["split_mo"]:
                    acct_best = {**row_best, "strat_name": strat_name}
            else:
                html += '<td class="gray">—</td>'
            html += "</tr>"

        html += "</table>"

        # Account recommendation
        if acct_best["split_mo"] > 0:
            best_per_account[acct_label] = acct_best
            html += f'<h3>Recommended: {acct_best["strat_name"]} at {acct_best["risk_name"]}</h3>'
            html += f'<p>Monthly (80% split): <span class="highlight">{fmt_money(acct_best["split_mo"])}/mo ({fmt_money(acct_best["split_yr"])}/yr)</span></p>'

        html += "</div>"

    # Summary boxes
    html += '<div style="text-align:center;margin:30px 0;"><h2>Optimal Income by Account Size</h2>'
    for acct_label, acct_val, _, acct_name in ACCOUNTS:
        b = best_per_account.get(acct_label, {})
        mo = b.get("split_mo", 0)
        yr = b.get("split_yr", 0)
        rn = b.get("risk_name", "?")
        sn = b.get("strat_name", "?")
        html += f'<div class="summary-box"><div class="label">{acct_name}</div>'
        html += f'<div class="value">{fmt_money(mo)}/mo</div>'
        html += f'<div class="metric">{fmt_money(yr)}/yr | {rn} | {sn}</div></div>'
    html += "</div>"

    # Scaling roadmap
    b5k = best_per_account.get("5k", {})
    b10k = best_per_account.get("10k", {})
    b100k = best_per_account.get("100k", {})
    b50 = best_per_account.get("50", {})
    b100 = best_per_account.get("100", {})

    html += '<div class="section"><h2>Scaling Roadmap</h2><div class="roadmap">'
    html += f'''
    <div class="step done"><strong>$5k Step 1 — PASSED ✓</strong><br><span class="metric">10% target hit Day 1.</span></div>
    <div class="step"><strong>$5k Step 2 (5% target)</strong><br><span class="metric">Bot at {b5k.get("risk_name","1%")} risk. Strategy: {b5k.get("strat_name","MTF")}.</span></div>
    <div class="step"><strong>$5k Funded</strong><br><span class="metric">Monthly: {fmt_money(b5k.get("split_mo",0))}</span></div>
    <div class="step future"><strong>$10k Challenge</strong><br><span class="metric">Monthly: {fmt_money(b10k.get("split_mo",0))}</span></div>
    <div class="step future"><strong>$100k Challenge</strong><br><span class="metric">Monthly: <span class="highlight">{fmt_money(b100k.get("split_mo",0))}</span> ({fmt_money(b100k.get("split_yr",0))}/yr)</span></div>
    <div class="step future"><strong>Vantage $50 (parallel)</strong><br><span class="metric">Monthly: {fmt_money(b50.get("split_mo",0))}</span></div>
    <div class="step future"><strong>Vantage $100 (parallel)</strong><br><span class="metric">Monthly: {fmt_money(b100.get("split_mo",0))}</span></div>
    '''
    html += "</div></div>"

    # Forex Pairs Matrix
    html += '<div class="section"><h2>Forex Pairs — 8 Strategies × 2 Risks ($50 Account)</h2>'
    html += '<p class="metric">Pairs tested: USDJPY, GBPJPY, NZDUSD, GBPUSD, EURUSD | Risks: 0.5%, 1.0%</p>'

    for sym in FOREX_PAIRS:
        html += f'<h3>{sym}</h3><table><tr><th>Strategy</th>'
        for _, _, risk_name in FOREX_RISKS:
            html += f'<th>{risk_name}</th>'
        html += '</tr>'

        for strat_key, strat_name in STRATEGIES:
            html += f"<tr><td style='text-align:left'><strong>{strat_name}</strong></td>"
            for risk_label, _, _ in FOREX_RISKS:
                r = find_forex_result(sym, risk_label, strat_key)
                if r is None or r["trades"] == 0:
                    html += '<td class="gray">No trades</td>'
                else:
                    cls = "green" if r["ret"] > 0 else "red"
                    html += f'<td class="{cls}">'
                    html += f'<strong>{r["ret"]:.1f}%</strong><br>'
                    html += f'<span class="metric">WR:{r["wr"]:.0f}% R:R 1:{r["rr"]:.1f} #{r["trades"]}</span>'
                    html += '</td>'
            html += '</tr>'
        html += '</table>'

    html += '</div>'

    # Methodology
    html += """<div class="section"><h2>Methodology</h2><ul>
    <li><strong>Data:</strong> XAUUSD M5, Oct 2024 — Apr 2026 (18 months)</li>
    <li><strong>Engine:</strong> PropFirmGuard with safety buffers, spread/slippage costs</li>
    <li><strong>Compounding:</strong> 50% profit growth factor</li>
    <li><strong>Income:</strong> 80% profit split (FundingPips funded terms)</li>
    <li><strong>Floor test:</strong> Equity must stay above 90% of initial balance</li>
    <li><strong>Filters:</strong> R:R ≥ 1.5 gate, EMA(200) trend filter, MTF bias alignment</li>
    <li><strong>New strategies:</strong> Box Theory (daily range), AMD Cycle (ICT/SMC)</li>
    </ul></div>"""

    html += """<div class="section"><h2>Legend</h2>
    <table><tr><td class="green" style="width:60px">Green</td><td>Profitable, DD ≤ 15%, floor OK</td>
    <td class="yellow" style="width:60px">Yellow</td><td>Profitable but DD > 15%</td>
    <td class="red" style="width:60px">Red</td><td>Negative return or floor breach</td>
    <td class="gray" style="width:60px">Gray</td><td>No trades generated</td></tr></table></div>"""

    html += "</body></html>"
    return html


def main():
    data = {}
    for acct_label, _, _, _ in ACCOUNTS:
        for risk_label, _, _ in RISKS:
            for strat_key, _ in STRATEGIES:
                r = find_result(acct_label, risk_label, strat_key)
                if r:
                    data[(acct_label, risk_label, strat_key)] = r

    print(f"Found {len(data)}/160 results")

    html = generate_html(data)
    os.makedirs("reports", exist_ok=True)
    with open(OUTPUT, "w") as f:
        f.write(html)

    print(f"Report: {OUTPUT}")
    print(f"Open: file://{os.path.abspath(OUTPUT)}")


if __name__ == "__main__":
    main()
