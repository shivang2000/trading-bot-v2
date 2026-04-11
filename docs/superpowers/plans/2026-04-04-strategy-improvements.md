# Strategy Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement 4 trading bot improvements: AMD Cycle P3 tuning, EURUSD dollar filter, 1H mid-range filter, and currency expansion to 4 new forex pairs.

**Architecture:** Each improvement is independent. Filters are additive in `_publish_signal()`. AMD tuning modifies existing strategy logic. Currency expansion adds data + config only.

**Tech Stack:** Python 3.12, pandas, pandas_ta, asyncio, MT5 RPyC, Docker

---

## File Map

| File | Action | Purpose |
|---|---|---|
| `src/analysis/strategies/m5_amd_cycle.py` | Modify | Add 9am H1 bias, M15 FVG entry, conservative TP |
| `src/analysis/signal_generator.py` | Modify | Add EURUSD DXY filter + 1H mid-range filter in `_publish_signal()` and `_scan_symbol()` |
| `config/base.yaml` | Modify | Add USDJPY, GBPJPY, NZDUSD, GBPUSD instrument configs |
| `config/vantage-50.yaml` | Modify | Add profitable new pairs to signal_generator.instruments |
| `scripts/run_full_matrix.py` | Modify | Add new pairs to matrix runner |

---

### Task 1: AMD Cycle — Add 9am H1 Candle Bias

**Files:**
- Modify: `src/analysis/strategies/m5_amd_cycle.py:144-170`

- [ ] **Step 1: Add `_get_h1_9am_bias()` method**

Add this method to `M5AmdCycleStrategy` class, after `_detect_feg()` (line 142):

```python
def _get_h1_9am_bias(self, h1_bars: pd.DataFrame, as_of: datetime) -> str:
    """Get directional bias from the H1 candle closing at/after 9am UTC.
    
    Returns 'bullish', 'bearish', or '' if no data.
    """
    if h1_bars is None or len(h1_bars) < 5:
        return ""
    
    df = h1_bars.copy()
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], utc=True)
    else:
        return ""
    
    today = as_of.date()
    # Find the H1 candle that closes at 9am or 10am UTC today
    nine_am_candles = df[
        (df["time"].dt.date == today) & 
        (df["time"].dt.hour >= 9) & 
        (df["time"].dt.hour <= 10)
    ]
    if len(nine_am_candles) == 0:
        return ""
    
    candle = nine_am_candles.iloc[0]
    return "bullish" if candle["close"] > candle["open"] else "bearish"
```

- [ ] **Step 2: Wire bias into `scan()` method**

In `scan()`, after the regime check (line 169) and before ATR calculation (line 172), add:

```python
        # Power of Three: 9am H1 candle sets directional bias
        h1_bias = self._get_h1_9am_bias(h1_bars, now) if h1_bars is not None else ""
```

Then in the signal generation section (after line 196), wrap the BUY/SELL blocks:

```python
        if sweep_dir == "below" and h1_bias != "bearish":
            # Sweep below = bullish manipulation → BUY (only if H1 bias not bearish)
```

```python
        elif sweep_dir == "above" and h1_bias != "bullish":
            # Sweep above = bearish manipulation → SELL (only if H1 bias not bullish)
```

- [ ] **Step 3: Commit**

```bash
git add src/analysis/strategies/m5_amd_cycle.py
git commit -m "feat(amd): add 9am H1 candle bias (Power of Three)"
```

---

### Task 2: AMD Cycle — Conservative TP (opposite side of accumulation)

**Files:**
- Modify: `src/analysis/strategies/m5_amd_cycle.py:199-234`

- [ ] **Step 1: Change BUY TP calculation**

Replace line 202:
```python
            tp = zone.high + (zone.high - zone.low)  # TP = range width above zone
```
With:
```python
            tp = zone.high  # TP = opposite side of accumulation (conservative)
```

- [ ] **Step 2: Change SELL TP calculation**

Replace line 222:
```python
            tp = zone.low - (zone.high - zone.low)  # TP = range width below zone
```
With:
```python
            tp = zone.low  # TP = opposite side of accumulation (conservative)
```

- [ ] **Step 3: Lower min R:R since TP is closer**

In `__init__` (line 54), change:
```python
        self._min_rr = cfg.get("min_rr", 2.0)
```
To:
```python
        self._min_rr = cfg.get("min_rr", 1.5)
```

- [ ] **Step 4: Commit**

```bash
git add src/analysis/strategies/m5_amd_cycle.py
git commit -m "feat(amd): conservative TP at opposite side of accumulation"
```

---

### Task 3: EURUSD Dollar Strength Filter

**Files:**
- Modify: `src/analysis/signal_generator.py:270-305` (context building)
- Modify: `src/analysis/signal_generator.py:530-572` (filter in `_publish_signal`)

- [ ] **Step 1: Fetch EURUSD data and compute bias in `_scan_symbol()`**

After line 304 (`"weekly_bias": weekly_bias,`), add to the `_scan_context` dict:

```python
            "dxy_bias": "",  # will be populated below
            "h1_position": 0.5,  # will be populated below
        }

        # ── EURUSD Dollar Strength (for XAUUSD correlation) ──
        if symbol == "XAUUSD":
            try:
                eurusd_bars = await asyncio.wait_for(
                    self._mt5.get_bars("EURUSD", "H1", count=50), timeout=15
                )
                if eurusd_bars is not None and len(eurusd_bars) >= 21:
                    eur_ema21 = eurusd_bars["close"].ewm(span=21, min_periods=10).mean()
                    eur_price = float(eurusd_bars["close"].iloc[-1])
                    eur_ema_val = float(eur_ema21.iloc[-1])
                    # EURUSD up = Dollar weak = Gold bullish
                    dxy_bias = "bearish" if eur_price > eur_ema_val else "bullish"
                    self._scan_context[symbol]["dxy_bias"] = dxy_bias
            except Exception:
                logger.debug("Failed to fetch EURUSD for DXY correlation")
```

**Note:** Remove the duplicate closing `}` — the `_scan_context` dict already closes. This code goes right after the dict assignment.

- [ ] **Step 2: Add DXY filter in `_publish_signal()`**

After the MTF bias filter block (after line 571), add:

```python
        # ── Filter 3: EURUSD Dollar Strength (XAUUSD only) ──
        if sig.symbol == "XAUUSD":
            dxy_bias = ctx.get("dxy_bias", "")
            if dxy_bias:
                if sig.action == "BUY" and dxy_bias == "bullish":
                    logger.info(
                        "DXY filter: BUY XAUUSD rejected (Dollar strong, EURUSD below EMA21)"
                    )
                    return
                if sig.action == "SELL" and dxy_bias == "bearish":
                    logger.info(
                        "DXY filter: SELL XAUUSD rejected (Dollar weak, EURUSD above EMA21)"
                    )
                    return
```

- [ ] **Step 3: Commit**

```bash
git add src/analysis/signal_generator.py
git commit -m "feat: add EURUSD dollar strength filter for XAUUSD signals"
```

---

### Task 4: 1H Mid-Range Avoidance Filter

**Files:**
- Modify: `src/analysis/signal_generator.py:270-305` (context building)
- Modify: `src/analysis/signal_generator.py:530-572` (filter in `_publish_signal`)

- [ ] **Step 1: Calculate H1 position in `_scan_symbol()`**

In the same area where we added DXY bias (after the EURUSD block from Task 3), add:

```python
        # ── 1H Mid-Range Position ──
        if h1_bars is not None and len(h1_bars) >= 1:
            current_h1 = h1_bars.iloc[-1]
            h1_range = float(current_h1["high"]) - float(current_h1["low"])
            if h1_range > 0:
                h1_pos = (current_price - float(current_h1["low"])) / h1_range
                self._scan_context[symbol]["h1_position"] = round(h1_pos, 3)
```

- [ ] **Step 2: Add 1H mid-range filter in `_publish_signal()`**

After the DXY filter block (from Task 3), add:

```python
        # ── Filter 4: 1H Mid-Range Avoidance ──
        h1_pos = ctx.get("h1_position", 0.5)
        if 0.3 < h1_pos < 0.7:
            logger.info(
                "1H mid-range filter: %s %s rejected (h1_pos=%.2f, manipulation zone)",
                sig.action, sig.symbol, h1_pos,
            )
            return
```

- [ ] **Step 3: Commit**

```bash
git add src/analysis/signal_generator.py
git commit -m "feat: add 1H mid-range avoidance filter (skip manipulation zone)"
```

---

### Task 5: Add Forex Instrument Configs

**Files:**
- Modify: `config/base.yaml:18-42`

- [ ] **Step 1: Add 4 forex pairs to instruments list**

After the ETHUSD entry in `config/base.yaml` (line 42), add:

```yaml
  - symbol: USDJPY
    point_size: 0.001      # MT5: point=0.001
    tick_value: 0.01       # Approximate — verify from MT5 symbol_info
    min_lot: 0.01
    max_lot: 100.0
    lot_step: 0.01
  - symbol: GBPJPY
    point_size: 0.001      # MT5: point=0.001
    tick_value: 0.01       # Approximate — verify from MT5 symbol_info
    min_lot: 0.01
    max_lot: 100.0
    lot_step: 0.01
  - symbol: NZDUSD
    point_size: 0.00001    # MT5: point=0.00001 (5-digit broker)
    tick_value: 1.0        # Standard forex: $1 per pip per lot
    min_lot: 0.01
    max_lot: 100.0
    lot_step: 0.01
  - symbol: GBPUSD
    point_size: 0.00001    # MT5: point=0.00001 (5-digit broker)
    tick_value: 1.0        # Standard forex: $1 per pip per lot
    min_lot: 0.01
    max_lot: 100.0
    lot_step: 0.01
```

- [ ] **Step 2: Commit**

```bash
git add config/base.yaml
git commit -m "config: add USDJPY, GBPJPY, NZDUSD, GBPUSD instruments"
```

---

### Task 6: Download Forex M5+H1 Data from Vantage MT5

**Files:**
- No code changes — data download via SSH

- [ ] **Step 1: Download M5 data for all 4 pairs**

```bash
ssh -i south-mumbai-key-pair.pem ec2-user@ec2-13-202-138-193.ap-south-1.compute.amazonaws.com \
  'docker exec trading-bot-vantage python3 -c "
import rpyc, pandas as pd
conn = rpyc.classic.connect(\"metatrader5-vantage\", 8001)
mt5 = conn.modules[\"MetaTrader5\"]
mt5.initialize()
for sym in [\"USDJPY\", \"GBPJPY\", \"NZDUSD\", \"GBPUSD\"]:
    print(f\"Downloading {sym} M5...\")
    rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_M5, 0, 50000)
    if rates is not None:
        df = pd.DataFrame(rpyc.classic.obtain(rates))
        df[\"time\"] = pd.to_datetime(df[\"time\"], unit=\"s\", utc=True)
        df.to_csv(f\"/app/data/backtest_cache/{sym}_M5_all.csv\", index=False)
        print(f\"  {sym}: {len(df)} bars\")
    else:
        print(f\"  {sym}: FAILED\")
print(\"Done\")
"'
```

- [ ] **Step 2: Download H1 data for all 4 pairs**

```bash
ssh -i south-mumbai-key-pair.pem ec2-user@ec2-13-202-138-193.ap-south-1.compute.amazonaws.com \
  'docker exec trading-bot-vantage python3 -c "
import rpyc, pandas as pd
conn = rpyc.classic.connect(\"metatrader5-vantage\", 8001)
mt5 = conn.modules[\"MetaTrader5\"]
mt5.initialize()
for sym in [\"USDJPY\", \"GBPJPY\", \"NZDUSD\", \"GBPUSD\"]:
    print(f\"Downloading {sym} H1...\")
    rates = mt5.copy_rates_from_pos(sym, mt5.TIMEFRAME_H1, 0, 20000)
    if rates is not None:
        df = pd.DataFrame(rpyc.classic.obtain(rates))
        df[\"time\"] = pd.to_datetime(df[\"time\"], unit=\"s\", utc=True)
        df.to_csv(f\"/app/data/backtest_cache/{sym}_H1_all.csv\", index=False)
        print(f\"  {sym}: {len(df)} bars\")
print(\"Done\")
"'
```

- [ ] **Step 3: SCP data files to local**

```bash
for SYM in USDJPY GBPJPY NZDUSD GBPUSD; do
  scp -i south-mumbai-key-pair.pem ec2-user@ec2-13-202-138-193.ap-south-1.compute.amazonaws.com:~/trading-bot-v2/data/backtest_cache/${SYM}_M5_all.csv data/backtest_cache/
  scp -i south-mumbai-key-pair.pem ec2-user@ec2-13-202-138-193.ap-south-1.compute.amazonaws.com:~/trading-bot-v2/data/backtest_cache/${SYM}_H1_all.csv data/backtest_cache/
done
```

---

### Task 7: Run 64-Backtest Matrix on New Pairs

**Files:**
- Modify: `scripts/run_full_matrix.py` — add new pairs

- [ ] **Step 1: Update matrix runner with forex pairs**

In `scripts/run_full_matrix.py`, update the `ACCOUNTS` list to add a forex-specific entry, or simply run manually:

```bash
python3 scripts/run_full_matrix.py  # Existing matrix for XAUUSD

# New forex matrix — run per pair
for SYM in USDJPY GBPJPY NZDUSD GBPUSD; do
  for STRAT in m5_mtf_momentum m5_keltner_squeeze m5_dual_supertrend m5_box_theory m5_amd_cycle m5_stochrsi_adx ema_pullback london_breakout; do
    for RISK in 0.5 1.0; do
      RL=$(echo $RISK | tr -d '.')
      python3 scripts/backtest_scalping.py \
        --symbol $SYM --timeframe M5 --start 2025-01-01 --end 2026-03-27 \
        --prop-firm --phase master --account-size 50 --risk-pct $RISK \
        --max-lot 0.50 --enable-costs \
        --safety-buffer-daily-usd 0.50 --safety-buffer-dd-usd 0.50 \
        --strategy $STRAT --label forex_${SYM}_${RL}_${STRAT} > /dev/null 2>&1 &
    done
  done
  wait  # Wait for each pair's batch before starting next
  echo "$SYM done"
done
```

- [ ] **Step 2: Analyze results**

```bash
python3 -c "
import json, glob
for sym in ['USDJPY','GBPJPY','NZDUSD','GBPUSD']:
    print(f'=== {sym} ===')
    for f in sorted(glob.glob(f'data/backtest_results/scalp_{sym}_forex_*.json')):
        with open(f) as fh:
            d = json.load(fh)
        trades = len(d.get('trades',[]))
        ret = d['total_return_pct']
        if ret > 0:
            label = f.split('forex_')[1].split('_2026')[0]
            print(f'  ✓ {label}: ret={ret:.1f}%, trades={trades}')
"
```

- [ ] **Step 3: Commit results analysis**

```bash
git add scripts/run_full_matrix.py
git commit -m "feat: run 64-backtest matrix on USDJPY/GBPJPY/NZDUSD/GBPUSD"
```

---

### Task 8: Update Configs with Profitable Pairs

**Files:**
- Modify: `config/vantage-50.yaml`

- [ ] **Step 1: Add profitable forex pairs to vantage-50**

Based on backtest results from Task 7, update `signal_generator.instruments` in `config/vantage-50.yaml`:

```yaml
signal_generator:
  instruments: [XAUUSD, BTCUSD, ETHUSD, USDJPY, GBPJPY, NZDUSD, GBPUSD]
  instrument_overrides: {}
```

And add profitable pairs to `strategies.scalping.instruments`:

```yaml
  scalping:
    instruments: [XAUUSD]  # Expand with profitable pairs from backtest
    # e.g., instruments: [XAUUSD, GBPJPY] if GBPJPY shows positive returns
```

- [ ] **Step 2: Commit**

```bash
git add config/vantage-50.yaml
git commit -m "config(vantage-50): add profitable forex pairs from backtest matrix"
```

---

### Task 9: Backtest AMD Cycle Tuned vs Original

**Files:**
- No code changes — verification only

- [ ] **Step 1: Run tuned AMD on XAUUSD**

```bash
python3 scripts/backtest_scalping.py \
  --symbol XAUUSD --timeframe M5 --start 2025-01-01 --end 2026-03-27 \
  --prop-firm --phase master --account-size 5000 --risk-pct 1.0 \
  --max-lot 0.50 --enable-costs \
  --strategy m5_amd_cycle --label amd_tuned_p3
```

- [ ] **Step 2: Compare results**

```bash
python3 -c "
import json, glob
for label in ['test3_amd', 'amd_tuned_p3']:
    files = sorted(glob.glob(f'data/backtest_results/scalp_XAUUSD_{label}_*.json'))
    if files:
        with open(files[-1]) as f:
            d = json.load(f)
        trades = d.get('trades',[])
        wins = [t['pnl'] for t in trades if t['pnl']>0]
        losses = [t['pnl'] for t in trades if t['pnl']<0]
        rr = (sum(wins)/len(wins)) / (abs(sum(losses)/len(losses))) if losses and wins else 0
        print(f'{label}: trades={len(trades)}, ret={d[\"total_return_pct\"]:.1f}%, WR={d[\"win_rate\"]:.1f}%, R:R=1:{rr:.2f}')
"
```

Expected: Tuned version has higher WR (>50%) and R:R (>1.5) than original (33%, 0.63).

---

### Task 10: Deploy to EC2 + Verify

**Files:**
- No code changes — deployment only

- [ ] **Step 1: Build image locally and transfer**

```bash
DOCKER_HOST=unix:///var/run/docker.sock docker buildx build --platform linux/amd64 \
  -t trading-bot-v2-trading-bot --load .
DOCKER_HOST=unix:///var/run/docker.sock docker save trading-bot-v2-trading-bot | gzip > /tmp/trading-bot-v2.tar.gz
scp -i south-mumbai-key-pair.pem /tmp/trading-bot-v2.tar.gz \
  ec2-user@ec2-13-202-138-193.ap-south-1.compute.amazonaws.com:/tmp/
```

- [ ] **Step 2: Load and restart on EC2**

```bash
ssh -i south-mumbai-key-pair.pem ec2-user@ec2-13-202-138-193.ap-south-1.compute.amazonaws.com \
  "gunzip -c /tmp/trading-bot-v2.tar.gz | docker load && \
   docker tag shivang2000/trading-bot-v2:latest trading-bot-v2-trading-bot:latest && \
   cd ~/trading-bot-v2 && \
   docker rm -f trading-bot-v2 trading-bot-vantage && \
   docker-compose -f docker-compose.ec2.yml up -d trading-bot trading-bot-vantage"
```

- [ ] **Step 3: Verify both bots**

```bash
ssh -i south-mumbai-key-pair.pem ec2-user@ec2-13-202-138-193.ap-south-1.compute.amazonaws.com \
  "sleep 30 && docker logs trading-bot-v2 2>&1 | grep -E 'is LIVE|DXY|1H mid' | tail -5 && \
   docker logs trading-bot-vantage 2>&1 | grep -E 'is LIVE|Instruments' | tail -3"
```

- [ ] **Step 4: Final commit and push**

```bash
git push origin research-v2
```
