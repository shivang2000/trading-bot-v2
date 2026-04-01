#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# FundingPips Multi-Strategy Scenario Matrix
# 32 scenarios + 4 baselines, max 6 concurrent
#
# Usage:
#   ./scripts/run_propfirm_matrix.sh          # full matrix
#   ./scripts/run_propfirm_matrix.sh --smoke   # Combo D 10k 1.00 only
#   ./scripts/run_propfirm_matrix.sh --table   # re-print table from existing results
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

cd "$(dirname "$0")/.."

# ── Strategy combos ──────────────────────────────────────────────────
COMBO_A="m5_mtf_momentum"
COMBO_B="m5_mtf_momentum,m5_keltner_squeeze"
COMBO_C="m5_mtf_momentum,m5_stochrsi_adx"
COMBO_D="m5_mtf_momentum,m5_keltner_squeeze,m5_stochrsi_adx"
COMBO_E="m5_mtf_momentum,m5_keltner_squeeze,m5_stochrsi_adx,m5_vwap_mean_reversion"

# ── Shared CLI args ──────────────────────────────────────────────────
COMMON_ARGS=(
    --symbol XAUUSD
    --timeframe M5
    --start 2025-01-01
    --end 2026-03-27
    --enable-costs
    --risk-pct 1.0
    --prop-firm
    --phase step1
    --safety-buffer-daily-usd 7.0
    --safety-buffer-dd-usd 7.0
)

RESULT_DIR="data/backtest_results"
LOG_DIR="logs/matrix"
MAX_PARALLEL=6

declare -a JOB_PIDS=()
declare -a ALL_LABELS=()

mkdir -p "$LOG_DIR" "$RESULT_DIR"

# ── Lot label: 0.50→lot050, 1.00→lot100, 2.00→lot200 ─────────────────
lot_label() {
    case "$1" in
        0.50) echo "lot050" ;;
        1.00) echo "lot100" ;;
        2.00) echo "lot200" ;;
        *)    echo "lot$(printf "%.0f" "$(echo "$1 * 100" | bc)")" ;;
    esac
}

# ── Launch a single backtest in background ────────────────────────────
# Usage: launch <label> <strategy> <account_size> <max_lot> [extra args...]
launch() {
    local label="$1" strategy="$2" acct="$3" lot="$4"
    shift 4
    local short="${strategy%%,*}"
    [[ "$strategy" == *,* ]] && short="${short}+..."
    printf "  [+] %-30s  acct=%-6s  lot=%s\n" "$label" "$acct" "$lot"
    python3 scripts/backtest_scalping.py \
        "${COMMON_ARGS[@]}" \
        --strategy "$strategy" \
        --account-size "$acct" \
        --max-lot "$lot" \
        --label "$label" \
        "$@" \
        >"$LOG_DIR/${label}.log" 2>&1 &
    JOB_PIDS+=($!)
    ALL_LABELS+=("$label")
}

# ── FIFO throttle: wait for oldest when at MAX_PARALLEL ──────────────
throttle() {
    if [ ${#JOB_PIDS[@]} -ge "$MAX_PARALLEL" ]; then
        wait "${JOB_PIDS[0]}" || true
        JOB_PIDS=("${JOB_PIDS[@]:1}")
    fi
}

# ── Summary table (Python) ────────────────────────────────────────────
print_table() {
python3 - <<'PYEOF'
import json, glob, os, re
from datetime import datetime

result_dir = "data/backtest_results"
pat = re.compile(r"scalp_XAUUSD_([A-E]_.+?)_(\d{8}_\d{6})\.json$")

by_label: dict = {}
for path in glob.glob(f"{result_dir}/scalp_XAUUSD_*.json"):
    m = pat.search(os.path.basename(path))
    if m:
        label, ts = m.group(1), m.group(2)
        if label not in by_label or ts > by_label[label][1]:
            by_label[label] = (path, ts)

if not by_label:
    print("No matrix results found in", result_dir)
    raise SystemExit(0)

rows = []
for label in sorted(by_label):
    path, _ = by_label[label]
    try:
        with open(path) as fh:
            d = json.load(fh)
    except Exception as e:
        rows.append((label, None, str(e)))
        continue

    acct       = d.get("initial_capital", 0)
    ret        = d.get("total_return_pct", 0)
    dd         = d.get("max_drawdown_pct", 0)
    wr         = d.get("win_rate", 0)
    n_trades   = d.get("total_trades", 0)
    worst      = d.get("worst_trade_pnl", 0)
    worst_pct  = abs(worst) / acct * 100 if acct else 0

    # Walk equity curve to find day 10% target is reached
    target = acct * 1.10
    days_10 = None
    equity  = acct
    start_dt = None
    try:
        start_dt = datetime.fromisoformat(d["start_date"].split("+")[0])
    except Exception:
        pass
    for t in d.get("trades", []):
        equity += t.get("pnl", 0)
        if equity >= target and days_10 is None:
            try:
                ct = datetime.fromisoformat(t["close_time"].split("+")[0])
                if start_dt:
                    days_10 = (ct - start_dt).days
            except Exception:
                pass
            break

    # Lot from label
    m_lot = re.search(r"_lot(\d+)$", label)
    if m_lot:
        lot_str = f"{int(m_lot.group(1))/100:.2f}"
    elif label.endswith("_tiered"):
        lot_str = "tiered"
    elif label.endswith("_compare"):
        lot_str = "cmp"
    else:
        lot_str = "?"

    wk_str = f"{days_10/7:.1f}wk" if days_10 is not None else "N/A"

    # Status: PASS = guard not triggered AND 10% hit ≤14 days
    # SLOW   = guard not triggered BUT >14 days (or never)
    # FAIL   = DD exceeded 10% hard limit (peak-to-trough proxy)
    dd_ok  = dd <= 10.0
    wk_ok  = days_10 is not None and days_10 <= 14
    if not dd_ok:
        status = "FAIL"
    elif wk_ok:
        status = "PASS"
    else:
        status = "SLOW"

    rows.append((label, {
        "acct": acct, "lot": lot_str, "ret": ret, "dd": dd,
        "dd_ok": dd_ok, "wr": wr, "trades": n_trades,
        "worst_pct": worst_pct, "wk": wk_str, "wk_ok": wk_ok,
        "status": status,
    }, None))

H = f"{'LABEL':<30} {'ACCT':>6} {'LOT':>6} {'RET%':>7} {'DD%':>7} {'WR%':>6} {'TRADES':>7} {'WORST%':>7} {'10%':>7} {'STATUS':>6}"
print()
print(H)
print("─" * len(H))

pass_count = slow_count = fail_count = 0
for label, d, err in rows:
    if err:
        print(f"  ERROR {label}: {err}")
        continue
    dd_flag = "!" if not d["dd_ok"] else " "
    wk_flag = "" if d["wk_ok"] else ""
    print(
        f"{label:<30} {d['acct']:>6.0f} {d['lot']:>6}"
        f" {d['ret']:>7.1f}%"
        f" {d['dd']:>6.1f}{dd_flag}"
        f" {d['wr']:>6.1f}%"
        f" {d['trades']:>7}"
        f" {d['worst_pct']:>6.2f}%"
        f" {d['wk']:>7}"
        f"  {d['status']:>6}"
    )
    if d["status"] == "PASS": pass_count += 1
    elif d["status"] == "SLOW": slow_count += 1
    else: fail_count += 1

print("─" * len(H))
print(f"\nResults: {pass_count} PASS  {slow_count} SLOW  {fail_count} FAIL  (total {len(rows)})")
print("Legend : DD%! = exceeds 10% FundingPips hard limit | PASS = 10% in ≤14d + DD ok | SLOW = >14d | FAIL = DD breach")
print(f"\nResult files: {result_dir}/scalp_XAUUSD_[A-E]_*.json")
PYEOF
}

# ════════════════════════════════════════════════════════════════════
# Main
# ════════════════════════════════════════════════════════════════════
MODE="${1:-}"

if [ "$MODE" = "--table" ]; then
    echo "Re-printing summary table from existing results..."
    print_table
    exit 0
fi

if [ "$MODE" = "--smoke" ]; then
    echo "═══════════════════════════════════════════════════════════════"
    echo "  Smoke test: Combo D, \$10k, lot=1.00"
    echo "═══════════════════════════════════════════════════════════════"
    echo ""
    launch "D_10k_lot100" "$COMBO_D" "10000" "1.00"
    wait
    echo ""
    echo "Smoke test complete. Verifying output..."
    python3 -c "
import json, glob, sys
files = sorted(glob.glob('data/backtest_results/scalp_XAUUSD_D_10k_lot100_*.json'))
if not files:
    print('ERROR: No output file found!')
    sys.exit(1)
with open(files[-1]) as fh:
    d = json.load(fh)
ok = True
if d.get('initial_capital') != 10000.0:
    print(f'  FAIL initial_capital={d.get(\"initial_capital\")} (expected 10000.0)')
    ok = False
else:
    print(f'  OK   initial_capital={d[\"initial_capital\"]}')
strat = d.get('strategy', '')
if 'mtf' in strat.lower() or 'combo' in strat.lower() or d.get('total_trades', 0) > 0:
    print(f'  OK   strategy={strat}  trades={d.get(\"total_trades\")}')
else:
    print(f'  WARN strategy={strat}  trades={d.get(\"total_trades\")}')
print(f'  INFO return={d.get(\"total_return_pct\",0):.2f}%  dd={d.get(\"max_drawdown_pct\",0):.2f}%')
if ok:
    print(\"\\nSmoke test PASSED\")
else:
    print(\"\\nSmoke test FAILED\")
    sys.exit(1)
"
    exit 0
fi

# ── Full matrix ──────────────────────────────────────────────────────
echo "═══════════════════════════════════════════════════════════════"
echo "  FundingPips Multi-Strategy Scenario Matrix"
echo "  Started: $(date '+%Y-%m-%d %H:%M:%S')"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  32 scenarios + 4 baselines, max $MAX_PARALLEL parallel"
echo "  Logs: $LOG_DIR/"
echo ""

echo "── Combo A: MTF baseline (4 runs) ──"
for al in "5k:5000" "10k:10000"; do
    a_lbl="${al%%:*}"; a_val="${al##*:}"
    for lot in 0.50 1.00; do
        throttle
        launch "A_${a_lbl}_$(lot_label "$lot")" "$COMBO_A" "$a_val" "$lot"
    done
done

for cl in "B:$COMBO_B" "C:$COMBO_C" "D:$COMBO_D" "E:$COMBO_E"; do
    c_name="${cl%%:*}"; c_strats="${cl##*:}"
    echo ""
    echo "── Combo $c_name (6 runs) ──"
    for al in "5k:5000" "10k:10000"; do
        a_lbl="${al%%:*}"; a_val="${al##*:}"
        for lot in 0.50 1.00 2.00; do
            throttle
            launch "${c_name}_${a_lbl}_$(lot_label "$lot")" "$c_strats" "$a_val" "$lot"
        done
    done
done

echo ""
echo "── Combo D tiered caps (2 runs) ──"
for al in "5k:5000" "10k:10000"; do
    a_lbl="${al%%:*}"; a_val="${al##*:}"
    throttle
    launch "D_${a_lbl}_tiered" "$COMBO_D" "$a_val" "1.00" "--tiered-caps"
done

echo ""
echo "── Combo D individual baselines (2 runs, --compare) ──"
for al in "5k:5000" "10k:10000"; do
    a_lbl="${al%%:*}"; a_val="${al##*:}"
    throttle
    launch "D_${a_lbl}_compare" "$COMBO_D" "$a_val" "1.00" "--compare"
done

echo ""
echo "All ${#ALL_LABELS[@]} jobs launched. Waiting for completion..."
wait
echo "Finished: $(date '+%Y-%m-%d %H:%M:%S')"
echo ""

print_table
