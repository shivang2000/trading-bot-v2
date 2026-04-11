"""HTML Report Generator for backtesting results.

Generates self-contained HTML files viewable in any browser.
Uses Chart.js (CDN) for interactive charts, dark theme styling.
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from src.backtesting.result import BacktestResult

logger = logging.getLogger(__name__)

_CDN = "https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"
_PALETTE = ["#00d4ff", "#00ff88", "#ff6b6b", "#ffd93d", "#c084fc",
            "#fb923c", "#67e8f9", "#a3e635", "#f472b6", "#94a3b8"]

# Shared Chart.js options for line/bar charts (dark theme)
_CHART_OPTS = json.dumps({
    "responsive": True, "maintainAspectRatio": False,
    "interaction": {"intersect": False, "mode": "index"},
    "plugins": {"tooltip": {"enabled": True}, "legend": {"labels": {"color": "#e0e0e0"}}},
    "scales": {
        "x": {"ticks": {"color": "#94a3b8", "maxTicksLimit": 12, "maxRotation": 45},
               "grid": {"color": "#2a2a4e"}},
        "y": {"ticks": {"color": "#94a3b8"}, "grid": {"color": "#2a2a4e"}},
    },
})
_RADAR_OPTS = json.dumps({
    "responsive": True, "maintainAspectRatio": False,
    "plugins": {"legend": {"labels": {"color": "#e0e0e0"}}},
    "scales": {"r": {
        "ticks": {"color": "#94a3b8", "backdropColor": "transparent"},
        "grid": {"color": "#2a2a4e"},
        "pointLabels": {"color": "#e0e0e0", "font": {"size": 12}},
        "suggestedMin": 0, "suggestedMax": 100,
    }},
})


def _fmt(v: float, d: int = 2) -> str:
    return f"{v:,.{d}f}"


def _clr(v: float) -> str:
    return "#00ff88" if v > 0 else ("#ff4444" if v < 0 else "#e0e0e0")


def _pf(v: float) -> str:
    return "999.99" if (math.isinf(v) or v > 999) else _fmt(v)


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _drawdown(eq: list[float]) -> list[float]:
    if not eq:
        return []
    peak, dd = eq[0], []
    for v in eq:
        peak = max(peak, v)
        dd.append(round(((v - peak) / peak) * 100, 4) if peak else 0.0)
    return dd


def _equity_from_trades(trades: list[dict], cap: float) -> tuple[list[str], list[float]]:
    if not trades:
        return [], []
    ts, vals, cum = [], [], cap
    for t in trades:
        cum += t.get("pnl", 0.0)
        ts.append(str(t.get("close_time", "")))
        vals.append(round(cum, 2))
    return ts, vals


def _monthly(trades: list[dict]) -> list[dict]:
    agg: dict[str, dict] = defaultdict(lambda: {"pnl": 0.0, "n": 0, "w": 0})
    for t in trades:
        s = str(t.get("close_time", ""))
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00") if "T" in s else s)
            key = dt.strftime("%Y-%m")
        except (ValueError, TypeError):
            key = s[:7] if len(s) >= 7 else "unknown"
        pnl = t.get("pnl", 0.0)
        agg[key]["pnl"] += pnl
        agg[key]["n"] += 1
        if pnl > 0:
            agg[key]["w"] += 1
    return [{"month": k, "pnl": round(agg[k]["pnl"], 2), "trades": agg[k]["n"],
             "wr": round(agg[k]["w"] / agg[k]["n"] * 100, 1) if agg[k]["n"] else 0}
            for k in sorted(agg)]


def _histogram(pnls: list[float], n: int = 20) -> tuple[list[str], list[int], list[str]]:
    if not pnls:
        return [], [], []
    lo, hi = min(pnls), max(pnls)
    if lo == hi:
        c = "#00ff88" if lo >= 0 else "#ff4444"
        return [_fmt(lo)], [len(pnls)], [c]
    bsz = (hi - lo) / n
    labels, counts, colors = [], [0] * n, []
    for i in range(n):
        mid = lo + (i + 0.5) * bsz
        labels.append(_fmt(mid, 0))
        colors.append("#00ff88" if mid >= 0 else "#ff4444")
    for p in pnls:
        counts[min(int((p - lo) / bsz), n - 1)] += 1
    return labels, counts, colors


_CSS = (
    "* {margin:0;padding:0;box-sizing:border-box}"
    "body{background:#1a1a2e;color:#e0e0e0;"
    "font-family:system-ui,-apple-system,BlinkMacSystemFont,sans-serif;padding:20px;line-height:1.5}"
    ".container{max-width:1400px;margin:0 auto}"
    "h1{color:#00d4ff;font-size:1.8rem;margin-bottom:6px}"
    "h2{color:#00d4ff;font-size:1.3rem;margin:24px 0 12px;border-bottom:1px solid #2a2a4e;padding-bottom:6px}"
    ".sub{color:#94a3b8;font-size:.95rem}"
    ".card{background:#16213e;border:1px solid #2a2a4e;border-radius:10px;padding:20px;margin-bottom:16px}"
    ".hc{display:flex;flex-wrap:wrap;gap:30px;align-items:center}"
    ".bn{font-size:2.2rem;font-weight:700}.bl{font-size:.8rem;color:#94a3b8;text-transform:uppercase;letter-spacing:1px}"
    ".mg{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}"
    ".mc{background:#16213e;border:1px solid #2a2a4e;border-radius:8px;padding:14px;text-align:center}"
    ".mv{font-size:1.5rem;font-weight:600}.ml{font-size:.75rem;color:#94a3b8;text-transform:uppercase;margin-top:4px}"
    ".cc{position:relative;height:350px}"
    "table{width:100%;border-collapse:collapse;font-size:.85rem}"
    "th{background:#0f3460;color:#00d4ff;padding:10px 8px;text-align:left;position:sticky;top:0;cursor:pointer}"
    "th:hover{background:#1a4a7a}td{padding:8px;border-bottom:1px solid #2a2a4e}tr:hover{background:#1e2d4a}"
    ".p{color:#00ff88}.n{color:#ff4444}"
    ".ts{max-height:500px;overflow-y:auto;border-radius:8px}"
    ".rec{background:#0f3460;border-left:4px solid #00d4ff;padding:16px;border-radius:0 8px 8px 0;margin-bottom:10px}"
    ".rb{display:inline-block;background:#00d4ff;color:#1a1a2e;font-weight:700;border-radius:50%;"
    "width:28px;height:28px;line-height:28px;text-align:center;margin-right:8px}"
    ".stamp{color:#64748b;font-size:.75rem;text-align:right;margin-top:20px}"
    "@media(max-width:768px){.mg{grid-template-columns:repeat(2,1fr)}.hc{flex-direction:column;gap:12px}.bn{font-size:1.6rem}}"
)

_SORT_JS = (
    "function S(t,c){var b=t.querySelector('tbody'),r=Array.from(b.querySelectorAll('tr')),"
    "d=t.dataset.sd==='a'?'d':'a';t.dataset.sd=d;r.sort(function(a,b){"
    "var x=a.children[c].textContent.replace(/[$,%]/g,'').trim(),"
    "y=b.children[c].textContent.replace(/[$,%]/g,'').trim(),"
    "m=parseFloat(x),n=parseFloat(y);"
    "if(!isNaN(m)&&!isNaN(n))return d==='a'?m-n:n-m;return d==='a'?x.localeCompare(y):y.localeCompare(x)});"
    "r.forEach(function(e){b.appendChild(e)})}"
    "document.querySelectorAll('table').forEach(function(t){"
    "t.querySelectorAll('th').forEach(function(h,i){h.addEventListener('click',function(){S(t,i)})})})"
)


def _chart_js(canvas_id: str, chart_type: str, labels: list, datasets: list, opts: str = _CHART_OPTS) -> str:
    """Generate a single Chart.js instantiation IIFE."""
    return (
        f"(function(){{var c=document.getElementById('{canvas_id}');if(!c)return;"
        f"new Chart(c,{{type:'{chart_type}',"
        f"data:{{labels:{json.dumps(labels)},datasets:{json.dumps(datasets)}}},"
        f"options:{opts}}})}})()"
    )


class BacktestReportGenerator:
    """Generate beautiful HTML reports from backtest results."""

    def generate_single_report(self, result: BacktestResult, output_path: str) -> str:
        """Generate a single-strategy HTML report. Returns absolute path."""
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        eq_ts, eq_vals = self._equity(result)
        dd_vals = _drawdown(eq_vals)
        trd = self._to_dicts(result)
        pnls = [t["pnl"] for t in trd]
        hl, hc, hclr = _histogram(pnls)
        mo = _monthly(trd)
        rc = _clr(result.total_return_pct)

        # Header
        b = [f'<div class="card hc"><div><h1>{_esc(result.strategy_name)}</h1>'
             f'<div class="sub">{_esc(result.symbol)} &middot; '
             f'{result.start_date:%Y-%m-%d} to {result.end_date:%Y-%m-%d}</div>'
             f'<div class="sub" style="margin-top:4px">Capital: ${_fmt(result.initial_capital)}'
             f' &rarr; ${_fmt(result.final_equity)} &middot; {result.total_trades} trades</div></div>'
             f'<div style="text-align:center"><div class="bn" style="color:{rc}">'
             f'{result.total_return_pct:+.2f}%</div><div class="bl">Total Return</div></div>'
             f'<div style="text-align:center"><div class="bn" style="color:#ff4444">'
             f'{result.max_drawdown_pct:.2f}%</div><div class="bl">Max Drawdown</div></div></div>']

        # Metrics grid
        mx = [("Win Rate", f"{result.win_rate:.1f}%", _clr(result.win_rate - 50)),
              ("Profit Factor", _pf(result.profit_factor), _clr(result.profit_factor - 1)),
              ("Sharpe Ratio", f"{result.sharpe_ratio:.2f}", _clr(result.sharpe_ratio)),
              ("Sortino Ratio", f"{result.sortino_ratio:.2f}", _clr(result.sortino_ratio)),
              ("Expectancy", f"${_fmt(result.expectancy)}", _clr(result.expectancy)),
              ("Avg Trade P&L", f"${_fmt(result.avg_trade_pnl)}", _clr(result.avg_trade_pnl)),
              ("Best Trade", f"${_fmt(result.best_trade_pnl)}", "#00ff88"),
              ("Worst Trade", f"${_fmt(result.worst_trade_pnl)}", "#ff4444")]
        cards = "".join(f'<div class="mc"><div class="mv" style="color:{c}">{v}</div>'
                        f'<div class="ml">{l}</div></div>' for l, v, c in mx)
        b.append(f'<div class="mg">{cards}</div>')

        # Chart canvases
        for cid, label in [("equityChart", "Equity Curve"), ("ddChart", "Drawdown"),
                           ("pnlHist", "Trade P&amp;L Distribution")]:
            b.append(f'<div class="card"><h2>{label}</h2><div class="cc"><canvas id="{cid}"></canvas></div></div>')

        b.append(self._monthly_html(mo))
        b.append(self._trades_html(trd[-100:]))

        # Extra stats row
        b.append(f'<div class="card" style="display:flex;gap:40px;flex-wrap:wrap">'
                 f'<div><span class="bl">Max Consec. Wins</span>'
                 f'<div style="font-size:1.3rem;color:#00ff88">{result.max_consecutive_wins}</div></div>'
                 f'<div><span class="bl">Max Consec. Losses</span>'
                 f'<div style="font-size:1.3rem;color:#ff4444">{result.max_consecutive_losses}</div></div>'
                 f'<div><span class="bl">Avg Duration</span>'
                 f'<div style="font-size:1.3rem">{result.avg_trade_duration_hours:.1f}h</div></div></div>')
        b.append(f'<div class="stamp">Generated {datetime.now():%Y-%m-%d %H:%M:%S}</div>')

        # Charts JS
        js = ";".join([
            _chart_js("equityChart", "line", eq_ts, [
                {"label": "Equity", "data": eq_vals, "borderColor": "#00ff88",
                 "backgroundColor": "rgba(0,255,136,0.08)", "fill": True,
                 "tension": 0.2, "pointRadius": 0, "borderWidth": 2}]),
            _chart_js("ddChart", "line", eq_ts, [
                {"label": "Drawdown %", "data": dd_vals, "borderColor": "#ff4444",
                 "backgroundColor": "rgba(255,68,68,0.15)", "fill": True,
                 "tension": 0.2, "pointRadius": 0, "borderWidth": 2}]),
            _chart_js("pnlHist", "bar", hl, [
                {"label": "Trades", "data": hc, "backgroundColor": hclr,
                 "borderWidth": 0, "borderRadius": 3}]),
        ])

        html = self._html(f"{result.strategy_name} - {result.symbol} Backtest", "\n".join(b), js)
        out.write_text(html, encoding="utf-8")
        logger.info("Single report saved to %s", out)
        return str(out.resolve())

    def generate_comparison_report(self, results: list[BacktestResult], output_path: str) -> str:
        """Generate a multi-strategy comparison HTML report. Returns absolute path."""
        if not results:
            raise ValueError("At least one BacktestResult required")
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        ranked = sorted(results, key=lambda r: r.profit_factor, reverse=True)
        syms = sorted({r.symbol for r in results})

        b = [f'<div class="card hc"><div><h1>Strategy Comparison Report</h1>'
             f'<div class="sub">{", ".join(syms)} &middot; {len(results)} strategies compared</div>'
             f'</div></div>']
        b.append(self._ranking_html(ranked))
        b.append('<div class="card"><h2>Equity Curves Overlay</h2>'
                 '<div class="cc"><canvas id="eqOverlay"></canvas></div></div>')
        b.append('<div class="card"><h2>Strategy Metrics Radar</h2>'
                 '<div class="cc"><canvas id="radar"></canvas></div></div>')
        b.append(self._recs_html(ranked[:3]))
        b.append(f'<div class="stamp">Generated {datetime.now():%Y-%m-%d %H:%M:%S}</div>')

        # Overlay datasets
        eq_ds, longest = [], []
        for i, r in enumerate(ranked):
            ts, vals = self._equity(r)
            if len(ts) > len(longest):
                longest = ts
            eq_ds.append({"label": r.strategy_name, "data": vals,
                          "borderColor": _PALETTE[i % len(_PALETTE)],
                          "backgroundColor": "transparent",
                          "tension": 0.2, "pointRadius": 0, "borderWidth": 2})

        # Radar datasets (normalised 0-100)
        r_labels = ["Win Rate", "Profit Factor", "Sharpe", "Trades/Day", "Avg P&L"]
        r_ds = []
        for i, r in enumerate(ranked):
            days = max((r.end_date - r.start_date).days, 1)
            tpd = r.total_trades / days
            r_ds.append({"label": r.strategy_name,
                         "data": [round(min(r.win_rate, 100), 1),
                                  round(min(r.profit_factor * 20, 100), 1),
                                  round(min(max(r.sharpe_ratio * 20 + 50, 0), 100), 1),
                                  round(min(tpd * 50, 100), 1),
                                  round(min(max(r.avg_trade_pnl + 50, 0), 100), 1)],
                         "borderColor": _PALETTE[i % len(_PALETTE)],
                         "backgroundColor": _PALETTE[i % len(_PALETTE)] + "22",
                         "pointBackgroundColor": _PALETTE[i % len(_PALETTE)], "borderWidth": 2})

        js = ";".join([
            _chart_js("eqOverlay", "line", longest, eq_ds),
            _chart_js("radar", "radar", r_labels, r_ds, _RADAR_OPTS),
        ])

        html = self._html("Strategy Comparison Report", "\n".join(b), js)
        out.write_text(html, encoding="utf-8")
        logger.info("Comparison report saved to %s", out)
        return str(out.resolve())

    def _html(self, title: str, body: str, js: str) -> str:
        return (f'<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">'
                f'<meta name="viewport" content="width=device-width,initial-scale=1.0">'
                f'<title>{_esc(title)}</title><style>{_CSS}</style></head>'
                f'<body><div class="container">{body}</div>'
                f'<script src="{_CDN}"></script><script>{js};{_SORT_JS}</script></body></html>')

    def _equity(self, r: BacktestResult) -> tuple[list[str], list[float]]:
        if r.equity_curve is not None and not r.equity_curve.empty:
            return ([str(t) for t in r.equity_curve.index],
                    [round(float(v), 2) for v in r.equity_curve.values])
        return _equity_from_trades(self._to_dicts(r), r.initial_capital)

    def _to_dicts(self, r: BacktestResult) -> list[dict]:
        return [{"ticket": t.ticket,
                 "side": t.side.value if hasattr(t.side, "value") else str(t.side),
                 "open_price": t.open_price, "close_price": t.close_price,
                 "open_time": str(t.open_time), "close_time": str(t.close_time),
                 "pnl": round(t.pnl, 2), "close_reason": t.close_reason,
                 "duration_h": round(t.duration / 3600, 1) if t.duration else 0}
                for t in r.trades]

    def _monthly_html(self, data: list[dict]) -> str:
        if not data:
            return '<div class="card"><h2>Monthly Returns</h2><p>No trade data.</p></div>'
        rows = "".join(f'<tr><td>{m["month"]}</td><td class="{"p" if m["pnl"]>=0 else "n"}">'
                       f'${_fmt(m["pnl"])}</td><td>{m["trades"]}</td><td>{m["wr"]:.1f}%</td></tr>'
                       for m in data)
        return (f'<div class="card"><h2>Monthly Returns</h2><div class="ts"><table>'
                f'<thead><tr><th>Month</th><th>P&amp;L</th><th>Trades</th><th>Win Rate</th></tr></thead>'
                f'<tbody>{rows}</tbody></table></div></div>')

    def _trades_html(self, trades: list[dict]) -> str:
        if not trades:
            return '<div class="card"><h2>Trade Log</h2><p>No trades.</p></div>'
        rows = "".join(
            f'<tr><td>{t["ticket"]}</td><td>{t["side"]}</td><td>{t["open_price"]}</td>'
            f'<td>{t["close_price"]}</td><td class="{"p" if t["pnl"]>=0 else "n"}">${_fmt(t["pnl"])}</td>'
            f'<td>{t["close_reason"]}</td><td>{t.get("duration_h","")}h</td>'
            f'<td style="font-size:.75rem;color:#64748b">{t["open_time"]}</td>'
            f'<td style="font-size:.75rem;color:#64748b">{t["close_time"]}</td></tr>'
            for t in trades)
        return (f'<div class="card"><h2>Trade Log (last {len(trades)})</h2><div class="ts"><table>'
                f'<thead><tr><th>Ticket</th><th>Side</th><th>Entry</th><th>Exit</th>'
                f'<th>P&amp;L</th><th>Reason</th><th>Duration</th><th>Open</th><th>Close</th>'
                f'</tr></thead><tbody>{rows}</tbody></table></div></div>')

    def _ranking_html(self, ranked: list[BacktestResult]) -> str:
        rows = "".join(
            f'<tr><td><span class="rb">{i+1}</span></td>'
            f'<td style="font-weight:600">{_esc(r.strategy_name)}</td><td>{_esc(r.symbol)}</td>'
            f'<td class="{"p" if r.total_return_pct>=0 else "n"}">{r.total_return_pct:+.2f}%</td>'
            f'<td>{_pf(r.profit_factor)}</td><td>{r.win_rate:.1f}%</td>'
            f'<td>{r.sharpe_ratio:.2f}</td><td>{r.sortino_ratio:.2f}</td>'
            f'<td>{r.max_drawdown_pct:.2f}%</td><td>{r.total_trades}</td>'
            f'<td class="{"p" if r.avg_trade_pnl>=0 else "n"}">${_fmt(r.avg_trade_pnl)}</td>'
            f'<td>${_fmt(r.expectancy)}</td></tr>'
            for i, r in enumerate(ranked))
        return (f'<div class="card"><h2>Strategy Ranking</h2><div class="ts"><table>'
                f'<thead><tr><th>#</th><th>Strategy</th><th>Symbol</th><th>Return</th>'
                f'<th>PF</th><th>Win Rate</th><th>Sharpe</th><th>Sortino</th>'
                f'<th>Max DD</th><th>Trades</th><th>Avg P&amp;L</th><th>Expectancy</th>'
                f'</tr></thead><tbody>{rows}</tbody></table></div></div>')

    def _recs_html(self, top: list[BacktestResult]) -> str:
        if not top:
            return ""
        reasons = ["Highest profit factor with strong risk-adjusted returns.",
                    "Strong runner-up balancing win rate and drawdown control.",
                    "Solid alternative with consistent performance characteristics."]
        items = "".join(
            f'<div class="rec"><span class="rb">{i+1}</span>'
            f'<strong>{_esc(r.strategy_name)}</strong>'
            f'<span class="{"p" if r.total_return_pct>=0 else "n"}"> ({r.total_return_pct:+.2f}%)</span>'
            f'<div style="margin-top:6px;color:#94a3b8;font-size:.9rem">'
            f'PF {_pf(r.profit_factor)} &middot; WR {r.win_rate:.1f}% &middot; '
            f'Sharpe {r.sharpe_ratio:.2f} &middot; DD {r.max_drawdown_pct:.2f}%</div>'
            f'<div style="margin-top:4px;font-size:.85rem">{reasons[i] if i < len(reasons) else ""}</div></div>'
            for i, r in enumerate(top))
        return f'<div class="card"><h2>Top Recommendations</h2>{items}</div>'

    def generate_master_report(self, results: list[BacktestResult], output_path: str) -> str:
        """Generate a master dashboard HTML report from ALL backtest runs.

        Includes an all-runs table, a strategy leaderboard grouped by best PF,
        and a deployment recommendation box.  Returns the absolute path to
        the generated file.
        """
        if not results:
            raise ValueError("At least one BacktestResult required")
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        b: list[str] = []

        # --- Header -----------------------------------------------------------
        syms = sorted({r.symbol for r in results})
        strats = sorted({r.strategy_name for r in results})
        b.append(
            f'<div class="card hc"><div><h1>Master Backtest Dashboard</h1>'
            f'<div class="sub">{len(results)} runs &middot; '
            f'{len(strats)} strategies &middot; {", ".join(syms)}</div></div></div>'
        )

        # --- All Runs Table ----------------------------------------------------
        sorted_runs = sorted(results, key=lambda r: r.profit_factor, reverse=True)
        run_rows = "".join(
            f'<tr>'
            f'<td>{r.start_date:%Y-%m-%d}</td>'
            f'<td style="font-weight:600">{_esc(r.strategy_name)}</td>'
            f'<td>{_esc(r.symbol)}</td>'
            f'<td>{r.total_trades}</td>'
            f'<td>{_pf(r.profit_factor)}</td>'
            f'<td class="{"p" if r.total_return_pct>=0 else "n"}">{r.total_return_pct:+.2f}%</td>'
            f'<td>{r.max_drawdown_pct:.2f}%</td>'
            f'<td>{r.win_rate:.1f}%</td>'
            f'<td>{r.sharpe_ratio:.2f}</td>'
            f'<td>${_fmt(r.initial_capital)}</td>'
            f'</tr>'
            for r in sorted_runs
        )
        b.append(
            f'<div class="card"><h2>All Runs</h2><div class="ts"><table>'
            f'<thead><tr>'
            f'<th>Date</th><th>Strategy</th><th>Symbol</th><th>Trades</th>'
            f'<th>PF</th><th>Return %</th><th>Max DD %</th>'
            f'<th>Win Rate</th><th>Sharpe</th><th>Capital</th>'
            f'</tr></thead><tbody>{run_rows}</tbody></table></div></div>'
        )

        # --- Strategy Leaderboard (best PF per strategy) -----------------------
        best_by_strat: dict[str, BacktestResult] = {}
        for r in results:
            prev = best_by_strat.get(r.strategy_name)
            if prev is None or r.profit_factor > prev.profit_factor:
                best_by_strat[r.strategy_name] = r
        leaderboard = sorted(best_by_strat.values(),
                             key=lambda r: r.profit_factor, reverse=True)

        lb_rows = ""
        for r in leaderboard:
            pf = r.profit_factor
            if math.isinf(pf) or pf > 999:
                pf_val = 999.99
            else:
                pf_val = pf
            if pf_val > 1.1:
                bg = "rgba(0,255,136,0.12)"
            elif pf_val >= 0.95:
                bg = "rgba(255,217,61,0.12)"
            else:
                bg = "rgba(255,68,68,0.12)"
            lb_rows += (
                f'<tr style="background:{bg}">'
                f'<td style="font-weight:600">{_esc(r.strategy_name)}</td>'
                f'<td>{_esc(r.symbol)}</td>'
                f'<td>{_pf(r.profit_factor)}</td>'
                f'<td class="{"p" if r.total_return_pct>=0 else "n"}">'
                f'{r.total_return_pct:+.2f}%</td>'
                f'<td>{r.max_drawdown_pct:.2f}%</td>'
                f'<td>{r.win_rate:.1f}%</td>'
                f'<td>{r.sharpe_ratio:.2f}</td>'
                f'<td>{r.total_trades}</td>'
                f'</tr>'
            )
        b.append(
            f'<div class="card"><h2>Strategy Leaderboard (Best PF per Strategy)</h2>'
            f'<div class="ts"><table>'
            f'<thead><tr>'
            f'<th>Strategy</th><th>Symbol</th><th>PF</th><th>Return %</th>'
            f'<th>Max DD %</th><th>Win Rate</th><th>Sharpe</th><th>Trades</th>'
            f'</tr></thead><tbody>{lb_rows}</tbody></table></div></div>'
        )

        # --- Deployment Recommendation Box -------------------------------------
        deploy = [r for r in leaderboard
                  if not math.isinf(r.profit_factor) and r.profit_factor > 1.1]
        not_rec = [r for r in leaderboard
                   if not math.isinf(r.profit_factor) and r.profit_factor < 1.0]

        deploy_items = ""
        for r in deploy:
            dd_warn = ' <span style="color:#ffd93d">&#9888; High DD</span>' \
                if r.max_drawdown_pct > 50 else ""
            deploy_items += (
                f'<li class="deploy" style="padding:6px 0">'
                f'<strong>{_esc(r.strategy_name)}</strong> &mdash; '
                f'PF {_pf(r.profit_factor)}, DD {r.max_drawdown_pct:.0f}%, '
                f'{r.total_return_pct:+.0f}% return{dd_warn}</li>'
            )
        not_rec_items = ""
        for r in not_rec:
            not_rec_items += (
                f'<li class="skip" style="padding:6px 0">'
                f'<strong>{_esc(r.strategy_name)}</strong> &mdash; '
                f'PF {_pf(r.profit_factor)} '
                f'<span style="color:#ff4444">&#10060;</span></li>'
            )

        rec_html = '<div class="card">'
        rec_html += '<h2>Deployment Recommendation</h2>'
        if deploy_items:
            rec_html += (
                '<div style="border-left:4px solid #00ff88;padding:12px 16px;'
                'margin-bottom:16px;background:rgba(0,255,136,0.06);border-radius:0 8px 8px 0">'
                '<h3 style="color:#00ff88;margin-bottom:8px">Recommended for Deployment</h3>'
                f'<ul style="list-style:none;padding:0">{deploy_items}</ul></div>'
            )
        else:
            rec_html += (
                '<div style="border-left:4px solid #94a3b8;padding:12px 16px;'
                'margin-bottom:16px;background:rgba(148,163,184,0.06);border-radius:0 8px 8px 0">'
                '<h3 style="color:#94a3b8;margin-bottom:8px">Recommended for Deployment</h3>'
                '<p style="color:#94a3b8">No strategies met the PF &gt; 1.1 threshold.</p></div>'
            )
        if not_rec_items:
            rec_html += (
                '<div style="border-left:4px solid #ff4444;padding:12px 16px;'
                'background:rgba(255,68,68,0.06);border-radius:0 8px 8px 0">'
                '<h3 style="color:#ff4444;margin-bottom:8px">Not Recommended</h3>'
                f'<ul style="list-style:none;padding:0">{not_rec_items}</ul></div>'
            )
        rec_html += '</div>'
        b.append(rec_html)

        # --- PF Distribution Chart ---------------------------------------------
        pf_labels = [_esc(r.strategy_name) for r in leaderboard]
        pf_vals = [round(min(r.profit_factor, 5.0), 2) for r in leaderboard]
        pf_colors = [
            "#00ff88" if (not math.isinf(r.profit_factor) and r.profit_factor > 1.1)
            else "#ffd93d" if (not math.isinf(r.profit_factor) and r.profit_factor >= 0.95)
            else "#ff4444"
            for r in leaderboard
        ]
        b.append(
            '<div class="card"><h2>Profit Factor by Strategy</h2>'
            '<div class="cc"><canvas id="pfChart"></canvas></div></div>'
        )

        b.append(f'<div class="stamp">Generated {datetime.now():%Y-%m-%d %H:%M:%S}</div>')

        # --- Chart JS ----------------------------------------------------------
        js = _chart_js("pfChart", "bar", pf_labels, [
            {"label": "Profit Factor", "data": pf_vals,
             "backgroundColor": pf_colors, "borderWidth": 0, "borderRadius": 4}
        ])

        html = self._html("Master Backtest Dashboard", "\n".join(b), js)
        out.write_text(html, encoding="utf-8")
        logger.info("Master report saved to %s", out)
        return str(out.resolve())
