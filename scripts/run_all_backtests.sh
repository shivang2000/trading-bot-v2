#!/bin/bash
# Run all 40 backtests in parallel (5 configs × 4 instruments × 2 periods)
cd /Users/shivang/dev/advanced-trading-bot/trading-bot-v2
RESULTS_DIR="data/backtest_results/final"
mkdir -p "$RESULTS_DIR"

BT="python3 scripts/backtest.py --strategy both --initial-capital 30 --volume 0.01"

# Config definitions: "label risk sl tp trail_act trail_atr"
CONFIGS=(
  "A 2.0 2.5 10.0 0.20 1.5"
  "B 3.0 2.5 10.0 0.20 1.5"
  "C 2.0 2.5 10.0 0.15 1.0"
  "D 2.0 3.5 10.0 0.20 1.5"
  "E 5.0 2.0 8.0  0.15 1.0"
)

# Instruments: "symbol point_size"
INSTRUMENTS=(
  "XAUUSD 0.01"
  "XAGUSD 0.001"
  "BTCUSD 0.01"
  "ETHUSD 0.01"
)

# Periods: "label m15_file h1_file"
PERIODS=(
  "9mo 20250601_20260301"
  "15mo 20250101_20260322"
)

PIDS=()
COUNT=0

for INST_DEF in "${INSTRUMENTS[@]}"; do
  read SYM PS <<< "$INST_DEF"
  for PERIOD_DEF in "${PERIODS[@]}"; do
    read PLABEL PDATES <<< "$PERIOD_DEF"
    M15="data/backtest_cache/${SYM}_M15_${PDATES}.csv"
    H1="data/backtest_cache/${SYM}_H1_${PDATES}.csv"

    if [ ! -f "$M15" ] || [ ! -f "$H1" ]; then
      echo "SKIP: $SYM $PLABEL — missing data"
      continue
    fi

    for CFG_DEF in "${CONFIGS[@]}"; do
      read CLABEL RISK SL TP TACT TATR <<< "$CFG_DEF"
      LABEL="${SYM}-${CLABEL}-${PLABEL}"

      $BT --symbol "$SYM" --point-size "$PS" \
        --risk-pct "$RISK" --sl-atr "$SL" --tp-atr "$TP" \
        --trail-act "$TACT" --trail-atr "$TATR" \
        --label "$LABEL" \
        --csv-m15 "$M15" --csv-h1 "$H1" \
        > "$RESULTS_DIR/${LABEL}.log" 2>&1 &

      PIDS+=($!)
      COUNT=$((COUNT + 1))

      # Limit to 8 parallel processes
      if [ ${#PIDS[@]} -ge 8 ]; then
        wait "${PIDS[0]}"
        PIDS=("${PIDS[@]:1}")
      fi
    done
  done
done

# Wait for all remaining
for PID in "${PIDS[@]}"; do
  wait "$PID"
done

echo "All $COUNT backtests complete. Extracting results..."

# Extract summary from all logs
echo ""
echo "INSTRUMENT|CONFIG|PERIOD|RETURN|MAX_DD|WR|TRADES"
for LOG in "$RESULTS_DIR"/*.log; do
  LABEL=$(basename "$LOG" .log)
  LINE=$(grep "Backtest complete" "$LOG" | tail -1)
  if [ -n "$LINE" ]; then
    TRADES=$(echo "$LINE" | grep -o '[0-9]* trades' | grep -o '[0-9]*')
    RETURN=$(echo "$LINE" | grep -o '[0-9.-]*% return' | grep -o '[0-9.-]*')
    DD=$(echo "$LINE" | grep -o '[0-9.-]*% max DD' | grep -o '[0-9.-]*')
    WR=$(echo "$LINE" | grep -o '[0-9.-]*% WR' | grep -o '[0-9.-]*')
    echo "${LABEL}|${RETURN}%|${DD}%|${WR}%|${TRADES}"
  else
    echo "${LABEL}|ERROR|ERROR|ERROR|ERROR"
  fi
done
