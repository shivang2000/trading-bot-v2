#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# FundingPips 2-Step FLEX matrix (2026-08-12)
# Flex rules: step1 target 10%, step2 6%, daily loss 4%, max DD 12%
#
# Pass 1 (breadth): every wired strategy, per symbol, raw edge
#   $10k, risk 0.5%, costs on, NO prop-firm guard — ranking only
# Pass 2 (flex): candidate strategies under PropFirmGuard flex limits
#
# Usage: ./scripts/run_flex_matrix.sh [--breadth|--flex|--all]
# ═══════════════════════════════════════════════════════════════════════
set -uo pipefail
cd "$(dirname "$0")/.."

LOG_DIR="logs/flex_matrix"
mkdir -p "$LOG_DIR"
MAX_PARALLEL=7
declare -a PIDS=()

throttle() {
    while [ "$(jobs -rp | wc -l)" -ge "$MAX_PARALLEL" ]; do sleep 5; done
}

bt() { # label, then args...
    local label="$1"; shift
    throttle
    echo "[+] $label"
    python3 scripts/backtest_scalping.py "$@" --label "$label" \
        >"$LOG_DIR/${label}.log" 2>&1 &
}

MODE="${1:---all}"

# ── Pass 1: breadth ─────────────────────────────────────────────────
if [ "$MODE" = "--breadth" ] || [ "$MODE" = "--all" ]; then

XAU_M5=data/backtest_cache/XAUUSD_M5_all.csv
US30_M5=data/backtest_cache/US30_M5_all.csv
XAU_M1=data/backtest_cache/XAUUSD_M1_all.csv

XAU_STRATS="m5_vwap_mean_reversion m5_dual_supertrend m5_keltner_squeeze m5_stochrsi_adx m5_mtf_momentum m5_bb_squeeze m5_mean_reversion m5_box_theory m5_amd_cycle m5_ny_orb m5_tight_sl_scalp m5_180_reversal m5_ema_833 m5_liquidity_sweep m30_fvg_ema m30_rsi2_mean_reversion ema_pullback london_breakout"
US30_STRATS="m5_dual_supertrend m5_keltner_squeeze m5_mtf_momentum m5_box_theory m5_amd_cycle m5_ny_orb m5_tight_sl_scalp m5_180_reversal m5_liquidity_sweep m30_rsi2_mean_reversion ema_pullback london_breakout"
M1_STRATS="m1_heikin_ashi_momentum m1_rsi_scalp m1_supertrend_scalp m1_ema_micro"

for S in $XAU_STRATS; do
    bt "br_XAUUSD_${S}" --symbol XAUUSD --timeframe M5 --csv-primary "$XAU_M5" \
       --strategy "$S" --initial-capital 10000 --risk-pct 0.5 --max-lot 1.0 --enable-costs
done
for S in $US30_STRATS; do
    bt "br_US30_${S}" --symbol US30 --timeframe M5 --csv-primary "$US30_M5" \
       --strategy "$S" --initial-capital 10000 --risk-pct 0.5 --max-lot 1.0 --enable-costs
done
for S in $M1_STRATS; do
    bt "br_XAUUSD_${S}" --symbol XAUUSD --timeframe M1 --csv-primary "$XAU_M1" \
       --strategy "$S" --initial-capital 10000 --risk-pct 0.5 --max-lot 1.0 --enable-costs
done
for SYM in USDJPY GBPJPY; do
    for S in m5_mtf_momentum m5_stochrsi_adx; do
        bt "br_${SYM}_${S}" --symbol "$SYM" --timeframe M5 \
           --csv-primary "data/backtest_cache/${SYM}_M5_all.csv" \
           --strategy "$S" --initial-capital 10000 --risk-pct 0.5 --max-lot 1.0 --enable-costs
    done
done
fi

# ── Pass 2: Flex step1 candidates ───────────────────────────────────
if [ "$MODE" = "--flex" ] || [ "$MODE" = "--all" ]; then

FLEX_ARGS=(--prop-firm --account-size 10000 --phase step1
           --daily-loss-pct 4.0 --max-dd-pct 12.0 --profit-target-pct 10.0
           --safety-buffer-daily-usd 40 --safety-buffer-dd-usd 40
           --max-lot 2.0 --enable-costs)

for R in 0.5 1.0 2.0; do
    bt "flex1_US30_ny_orb_r${R}" --symbol US30 --timeframe M5 \
       --csv-primary data/backtest_cache/US30_M5_all.csv \
       --strategy m5_ny_orb --risk-pct "$R" "${FLEX_ARGS[@]}"
    bt "flex1_US30_amd_r${R}" --symbol US30 --timeframe M5 \
       --csv-primary data/backtest_cache/US30_M5_all.csv \
       --strategy m5_amd_cycle --risk-pct "$R" "${FLEX_ARGS[@]}"
    bt "flex1_XAUUSD_m30rsi2_r${R}" --symbol XAUUSD --timeframe M5 \
       --csv-primary data/backtest_cache/XAUUSD_M5_all.csv \
       --strategy m30_rsi2_mean_reversion --risk-pct "$R" "${FLEX_ARGS[@]}"
done
for R in 1.0 2.0; do
    bt "flex1_US30_orb_amd_r${R}" --symbol US30 --timeframe M5 \
       --csv-primary data/backtest_cache/US30_M5_all.csv \
       --strategy m5_ny_orb,m5_amd_cycle --risk-pct "$R" "${FLEX_ARGS[@]}"
done
bt "flex1_XAUUSD_m30_keltner_r1.0" --symbol XAUUSD --timeframe M5 \
   --csv-primary data/backtest_cache/XAUUSD_M5_all.csv \
   --strategy m30_rsi2_mean_reversion,m5_keltner_squeeze --risk-pct 1.0 "${FLEX_ARGS[@]}"
bt "flex1_XAUUSD_dual_st_r1.0" --symbol XAUUSD --timeframe M5 \
   --csv-primary data/backtest_cache/XAUUSD_M5_all.csv \
   --strategy m5_dual_supertrend --risk-pct 1.0 "${FLEX_ARGS[@]}"
fi

wait
echo "DONE $(date '+%Y-%m-%d %H:%M:%S')"
