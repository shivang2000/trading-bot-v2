# Strategy Improvements Design Spec
**Date:** 2026-04-04
**Scope:** 4 improvements across strategy tuning, filters, and currency expansion

---

## 1. AMD Cycle Power of Three Tuning

**File:** `src/analysis/strategies/m5_amd_cycle.py`
**Goal:** Boost WR from 33% → 60%+ by adding H1 bias + M15 FVG entry confirmation

### Changes:
- **9am H1 candle bias:** Check if H1 candle closing at/after 09:00 UTC is bullish or bearish. Only allow BUY if bullish, SELL if bearish. Uses `h1_bars` param (currently ignored).
- **M15 FVG inversion entry:** After manipulation sweep detected, resample M5→M15, find a Fair Value Gap. Only enter when price returns to fill the FVG (inversion). Reuse FVG detection logic from `_detect_feg()`.
- **Conservative TP:** Change target from "range width beyond zone" to "opposite side of accumulation range." Higher WR, slightly lower R:R.

### Acceptance:
- Backtest WR > 50% (vs current 33%)
- R:R > 1.5 (vs current 0.63)
- Trades still generated (not over-filtered to 0)

---

## 2. EURUSD Dollar Strength Filter (for XAUUSD)

**File:** `src/analysis/signal_generator.py`
**Goal:** Filter XAUUSD signals using dollar strength via EURUSD inverse proxy

### Changes:
- In `_scan_symbol()`: Fetch EURUSD H1 bars (100 bars). Calculate EMA(21). If EURUSD price > EMA(21) → Dollar weak (bullish gold). If below → Dollar strong (bearish gold). Store as `dxy_bias` in `_scan_context`.
- In `_publish_signal()`: For XAUUSD only — reject BUY when `dxy_bias == "bullish"` (dollar strong), reject SELL when `dxy_bias == "bearish"` (dollar weak).
- **Only applies to XAUUSD** — other instruments unaffected.

### Acceptance:
- Fewer but higher-quality XAUUSD signals in backtest
- No impact on BTCUSD/ETHUSD/forex signals

---

## 3. 1H Mid-Range Avoidance Filter

**File:** `src/analysis/signal_generator.py`
**Goal:** Skip signals when price is in the middle of the current H1 candle (manipulation zone)

### Changes:
- In `_scan_symbol()`: Calculate `h1_position = (price - h1_low) / (h1_high - h1_low)`. Store in `_scan_context`.
- In `_publish_signal()`: Reject signals when `0.3 < h1_position < 0.7`. Only allow entries near H1 extremes (top/bottom 30%).

### Acceptance:
- Reduced losses from mid-range fake signals
- Does not over-filter (still allows trades at H1 extremes)

---

## 4. Currency Expansion (4 new forex pairs × 8 strategies)

**New pairs:** USDJPY, GBPJPY, NZDUSD, GBPUSD

### Steps:
1. **Download data:** M5 + H1 from Vantage MT5 via `docker exec` rpyc (50k bars each)
2. **Add instrument configs** to `config/base.yaml`:
   - USDJPY: point_size=0.001, tick_value=0.01 (per lot, per pip)
   - GBPJPY: point_size=0.001, tick_value=0.01
   - NZDUSD: point_size=0.00001, tick_value=1.0
   - GBPUSD: point_size=0.00001, tick_value=1.0
3. **Backtest matrix:** 4 pairs × 8 strategies × 2 risks (0.5%, 1%) = 64 runs
4. **Update configs:** Add profitable combos to `config/vantage-50.yaml`

### 8 Strategies to test per pair:
1. m5_mtf_momentum
2. m5_keltner_squeeze
3. m5_dual_supertrend
4. m5_box_theory
5. m5_amd_cycle (tuned)
6. m5_stochrsi_adx
7. ema_pullback
8. london_breakout

### Acceptance:
- At least 2 pairs with positive returns on at least 2 strategies
- Profitable combos added to vantage-50 config

---

## Execution Order

| # | Task | Files | Effort |
|---|---|---|---|
| 1 | AMD Cycle P3 tuning | m5_amd_cycle.py | 30 min |
| 2 | EURUSD DXY filter | signal_generator.py | 15 min |
| 3 | 1H mid-range filter | signal_generator.py | 10 min |
| 4 | Download forex data | EC2 rpyc script | 15 min |
| 5 | Add instrument configs | config/base.yaml | 10 min |
| 6 | Run 64 backtests | run_full_matrix.py | 40 min (parallel) |
| 7 | Backtest AMD/DXY/1H | backtest_scalping.py | 10 min |
| 8 | Update configs | vantage-50.yaml | 10 min |
| 9 | Deploy to EC2 | docker build + restart | 15 min |

**Total: ~2.5 hours**

---

## Testing Plan

1. **AMD Cycle:** Backtest tuned vs original on XAUUSD $5k 1% risk
2. **DXY filter:** Backtest XAUUSD with/without filter
3. **1H filter:** Backtest XAUUSD with/without filter
4. **New pairs:** 64-run matrix
5. **Integration:** Deploy to EC2, verify both bots start with new configs
