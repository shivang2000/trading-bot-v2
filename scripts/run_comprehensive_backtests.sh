#!/bin/bash
# Comprehensive backtesting: individual strategies + combined + multi-capital
cd /Users/shivang/dev/advanced-trading-bot/trading-bot-v2
RESULTS="data/backtest_results/v3"
mkdir -p "$RESULTS"

BT="python3 scripts/backtest.py --initial-capital 30 --volume 0.01"
SYMBOLS="XAUUSD XAGUSD BTCUSD ETHUSD"
PIDS=()

run() {
  local SYM=$1 STRAT=$2 LBL=$3
  local PS=0.01
  [ "$SYM" = "XAGUSD" ] && PS=0.001
  local M15="data/backtest_cache/${SYM}_M15_20250601_20260301.csv"
  local H1="data/backtest_cache/${SYM}_H1_20250601_20260301.csv"
  [ ! -f "$M15" ] && return
  $BT --symbol "$SYM" --strategy "$STRAT" --point-size "$PS" --label "$LBL" \
    --csv-m15 "$M15" --csv-h1 "$H1" > "$RESULTS/${LBL}.log" 2>&1 &
  PIDS+=($!)
  # Limit parallel to 6
  if [ ${#PIDS[@]} -ge 6 ]; then
    wait "${PIDS[0]}"; PIDS=("${PIDS[@]:1}")
  fi
}

echo "=== Round 1: Individual Strategy Isolation (24 backtests) ==="

for SYM in $SYMBOLS; do
  run $SYM ema_pullback "${SYM}-S-ema_only"
  run $SYM london_breakout "${SYM}-S-london_only"
  run $SYM ny_range_breakout "${SYM}-S-ny_range_only"
  run $SYM ny_momentum "${SYM}-S-ny_mom_only"
  run $SYM all "${SYM}-S-all_combined"
  run $SYM both "${SYM}-S-ema_london_baseline"
done

# Wait for round 1
for PID in "${PIDS[@]}"; do wait "$PID"; done
PIDS=()

echo "=== Round 1 Complete ==="
echo ""
echo "=== Results Summary ==="
echo "LABEL|RETURN|MAX_DD|WR|TRADES"
for LOG in "$RESULTS"/*.log; do
  LBL=$(basename "$LOG" .log)
  LINE=$(grep "Backtest complete" "$LOG" | tail -1)
  if [ -n "$LINE" ]; then
    TRADES=$(echo "$LINE" | grep -oE '[0-9]+ trades' | grep -oE '[0-9]+')
    RET=$(echo "$LINE" | grep -oE '[-0-9.]+% return' | grep -oE '[-0-9.]+')
    DD=$(echo "$LINE" | grep -oE '[0-9.]+% max DD' | grep -oE '[0-9.]+')
    WR=$(echo "$LINE" | grep -oE '[0-9.]+% WR' | grep -oE '[0-9.]+')
    echo "${LBL}|${RET}%|${DD}%|${WR}%|${TRADES}"
  fi
done | sort

echo ""
echo "=== All backtests complete ==="
