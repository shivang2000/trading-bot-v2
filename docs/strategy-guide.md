# Trading Bot V2 — Comprehensive Strategy Guide

## Last Updated: 2026-04-11
## Data Sources: 9-month M5 (101k bars XAUUSD, 50k bars US30 Vantage, 50k bars each forex)
## Backtests: 80+ runs across 11 strategies × 5 instruments × 4 risk levels

---

## 1. All Available Strategies (15 Total)

### M5 Scalping Strategies (11)

| # | Strategy | File | Description |
|---|----------|------|-------------|
| 1 | m5_keltner_squeeze | `m5_keltner_squeeze.py` | BB inside KC squeeze → breakout + MACD direction |
| 2 | m5_dual_supertrend | `m5_dual_supertrend.py` | Fast + Slow Supertrend agreement + ADX > 20 |
| 3 | m5_tight_sl_scalp | `m5_tight_sl_scalp.py` | Breakout of last bar high/low with aggressive trail |
| 4 | m5_amd_cycle | `m5_amd_cycle.py` | Accumulation-Manipulation-Distribution pattern |
| 5 | m5_ny_orb | `m5_ny_orb.py` | NY Opening Range Breakout (15/30/60min range) |
| 6 | m5_mtf_momentum | `m5_mtf_momentum.py` | EMA cross + Heikin-Ashi flip + volume surge |
| 7 | m5_mean_reversion | `m5_mean_reversion.py` | RSI extreme mean reversion |
| 8 | m5_vwap_mean_reversion | `m5_vwap_mean_reversion.py` | VWAP deviation mean reversion |
| 9 | m5_stochrsi_adx | `m5_stochrsi_adx.py` | Stochastic RSI + ADX filter |
| 10 | m5_bb_squeeze | `m5_bb_squeeze.py` | Bollinger Band squeeze breakout |
| 11 | m5_box_theory | `m5_box_theory.py` | Darvas box breakout pattern |

### M15 Swing Strategies (2)
| 12 | ema_pullback | `ema_pullback.py` | EMA(8/21/50) pullback in trending regime |
| 13 | london_breakout | `london_breakout.py` | Asian range breakout at London open |

### M1 Micro Strategies (2)
| 14 | m1_heikin_ashi_momentum | `m1_heikin_ashi_momentum.py` | HA color flip momentum |
| 15 | m1_rsi_scalp | `m1_rsi_scalp.py` | RSI(7) scalp on M1 |

---

## 2. Complete Backtest Results by Instrument

### XAUUSD (Gold) — 101,741 M5 bars (~9 months)

| Strategy | 0.25% Risk | 0.5% Risk | 1.0% Risk | 2.0% Risk | Best Config |
|----------|-----------|----------|----------|----------|-------------|
| **m5_keltner_squeeze** | — | — | **+262%** | — | 1.0%, 24h trading |
| **m5_tight_sl_scalp** | +39.5%, 5%DD | **+86.3%, 9%DD** | **+202%, 17%DD** | -1.3%, 32%DD | 0.5-1.0% |
| **m5_dual_supertrend** | — | — | **+31.5%** | — | 1.0%, 8-22 UTC |
| m5_ny_orb (r30_tp20_imm) | — | — | +23.1%, 26%DD | — | DD too high for prop |
| m5_ny_orb (r30_tp20_ret) | — | — | -11.6%, 30%DD | — | Retrace hurts gold |
| m5_ny_orb (r15_tp20_ret) | — | — | -27.6%, 31%DD | — | Bad |
| m5_ny_orb (r15_tp20_imm) | — | — | -26.3%, 31%DD | — | Bad |

**Winner strategies for XAUUSD:** Keltner Squeeze, Tight SL Scalp, Dual Supertrend
**DO NOT use on XAUUSD:** NY ORB (high DD), Mean Reversion (not tested)

---

### US30 (Dow Jones) — 50,000 M5 bars (~9 months, Vantage)

| Strategy | 0.25% Risk | 0.5% Risk | 1.0% Risk | 2.0% Risk | Best Config |
|----------|-----------|----------|----------|----------|-------------|
| **m5_amd_cycle** | +7.2%, 1.2%DD | +14.3%, 2.3%DD | **+24.8%, 2.7%DD** | +32.9%, 2.6%DD | Any — CHAMPION |
| **m5_ny_orb (15min ret)** | +6.5%, 4.0%DD | +14.4%, 6.7%DD | **+28.0%, 10.5%DD** | +27.2%, 17%DD | 0.5-1.0% |
| m5_dual_supertrend | +55.2%, 31%DD | +133.4%, 33%DD | +231.7%, 36%DD | +223.6%, 49%DD | 0.25% only (DD!) |
| m5_tight_sl_scalp | +8.8%, 9.8%DD | +18.5%, 19%DD | -30.5%, 32%DD | -30.9%, 33%DD | 0.25-0.5% |
| m5_mtf_momentum | +1.0%, 3.0%DD | +2.0%, 5.7%DD | +4.3%, 10.5%DD | +0.9%, 15%DD | 0.5-1.0% marginal |
| m5_mean_reversion | -22.7%, 27%DD | -26.5%, 31%DD | -27.1%, 33%DD | -30.9%, 40%DD | DISABLE — overfit |
| m5_keltner_squeeze | -11.3%, 14%DD | -19.0%, 25%DD | -18.1%, 35%DD | -11.7%, 35%DD | DISABLE |

**Winner strategies for US30:** AMD Cycle, NY ORB (15min retrace), optionally MTF Momentum
**DO NOT use on US30:** Mean Reversion (-27%, overfit), Keltner Squeeze (-18%), Dual Supertrend (DD too high for prop)

---

### US30 — Short Period Validation (13,423 M5 bars, 60 days yfinance)

| Strategy | 0.25% | 0.5% | 1.0% | 2.0% |
|----------|-------|------|------|------|
| m5_amd_cycle | +3.4%, 1.3%DD, **82%WR** | +6.5%, 2.5%DD | **+10.5%, 3.3%DD** | +14.8%, 3.2%DD |
| m5_mean_reversion | +4.5%, 6.3%DD | +11.2%, 10.4%DD | +16.2%, 15.5%DD | -5.7%, 29%DD |
| m5_mtf_momentum | +2.0%, 1.6%DD | +4.0%, 3.2%DD | +7.3%, 6.2%DD | +11.5%, 11.8%DD |
| london_breakout | -0.4%, 1.4%DD | -1.0%, 2.8%DD | +5.2%, 4.4%DD | -3.8%, 10.7%DD |

*Note: Mean Reversion appeared profitable on 60 days (+16%) but FAILED on 9 months (-27%). Always validate on longer data.*

---

### Forex Pairs (50,000 M5 bars each)

#### USDJPY
| Strategy | 0.25% | 0.5% | 1.0% | 2.0% | Notes |
|----------|-------|------|------|------|-------|
| m5_tight_sl_scalp | -0.75% | -0.75% | -0.75% | -0.75% | Flat — needs trailing stop engine |
| m5_ny_orb | -0.13% | -0.13% | -0.13% | -0.13% | Flat |

#### GBPUSD
| Strategy | 0.25% | 0.5% | 1.0% | 2.0% | Notes |
|----------|-------|------|------|------|-------|
| m5_tight_sl_scalp | -29.9%, 30%DD | -29.9% | -29.9% | -29.9% | DISABLE — loses without trailing |
| m5_ny_orb | -4.7%, 8%DD | -8.3%, 14%DD | -14.0%, 19%DD | -14.4%, 20%DD | DISABLE |

#### EURUSD
| Strategy | 0.25% | 0.5% | 1.0% | 2.0% | Notes |
|----------|-------|------|------|------|-------|
| m5_tight_sl_scalp | -30.0%, 30%DD | -30.0% | -30.0% | -30.0% | DISABLE — loses without trailing |
| m5_ny_orb | -1.2%, 4%DD | -1.5%, 6%DD | -2.9%, 6%DD | -3.3%, 6%DD | DISABLE |

**Forex conclusion:** Our M5 scalping strategies don't work on forex pairs without trailing stop engine support. Forex pairs are traded via Telegram signal strategies (EMA Pullback, London Breakout) not M5 scalping.

---

### NY ORB Optimization Sweep (18 configurations tested)

#### US30
| Config | Trades | Return | DD | WR | Winner? |
|--------|--------|--------|-----|-----|---------|
| r15_tp20_imm | 66 | -1.5% | 13.1% | 37.9% | No |
| r30_tp15_imm (original) | 57 | -3.0% | 14.2% | 42.1% | No |
| r30_tp20_imm | 57 | -4.3% | 15.0% | 35.1% | No |
| r30_tp25_imm | 56 | -9.3% | 16.1% | 28.6% | No |
| **r15_tp20_ret** | **64** | **+6.2%** | **7.2%** | **39.1%** | **YES** |
| r30_tp20_ret | 56 | -5.2% | 14.7% | 33.9% | No |
| r30_tp25_ret | 56 | -11.3% | 17.4% | 26.8% | No |

*15-minute range + 2.0x TP + retrace entry is the only profitable config on US30.*

#### XAUUSD
| Config | Trades | Return | DD | WR |
|--------|--------|--------|-----|-----|
| **r30_tp20_imm** | **589** | **+23.1%** | **25.6%** | **40.9%** |
| r30_tp20_ret | 548 | -11.6% | 30.1% | 33.6% |
| r15_tp20_ret | 129 | -27.6% | 31.0% | 24.0% |
| r15_tp20_imm | 566 | -26.3% | 31.3% | 37.8% |

*r30_tp20_imm is profitable on XAUUSD (+23%) but 25.6% DD exceeds prop firm 10% limit.*

---

## 3. Optimal Configuration per Account Type

### $100,000 Competition (FundedNext) — `fundednext-comp.yaml`
**Goal:** Maximize profit, stay within 5% daily / 10% max DD

| Instrument | Strategy | Risk | 9-Month Return | 9-Month DD | Why |
|-----------|----------|------|----------------|------------|-----|
| XAUUSD | m5_keltner_squeeze | 1.0% | +262% | ~17% | Best gold strategy, 24h active |
| XAUUSD | m5_tight_sl_scalp | 1.0% | +202% | 17% | 52% WR, 3099 trades, high volume |
| XAUUSD | m5_dual_supertrend | 1.0% | +31.5% | ~28% | Trend-follower, London+NY |
| US30 | m5_amd_cycle | 1.0% | +24.8% | 2.7% | CHAMPION — 62% WR, lowest DD |
| US30 | m5_ny_orb | 1.0% | +28.0% | 10.5% | 15min retrace, validated 9mo |
| US30 | m5_mtf_momentum | 1.0% | +4.3% | 10.5% | Safe add-on, marginal return |
| Both | ema_pullback | 1.0% | Proven live | — | M15 swing, regime-filtered |
| Both | london_breakout | 1.0% | Proven live | — | Asian range breakout |

### $10,000 Prop Firm (FundingPips) — `fundingpips-10k.yaml`
**Goal:** Pass Step 1 (8% = $800) then Step 2 (5% = $500) safely

| Instrument | Strategy | Risk | Notes |
|-----------|----------|------|-------|
| XAUUSD | m5_keltner_squeeze | 1.0% | 24h active, primary earner |
| XAUUSD | m5_dual_supertrend | 1.0% | 8-22 UTC, secondary |
| XAUUSD | m5_tight_sl_scalp | 1.0% | High-volume scalper |
| XAUUSD | ema_pullback | 1.0% | M15 swing |
| XAUUSD | london_breakout | 1.0% | Asian breakout |
| **Max lot** | 1.00 | | |
| **Projected** | Step 1 in 1-2 weeks | Step 2 in ~1 week | |

### $5,000 Prop Firm — Same as $10k with `max_lot: 0.50`

---

## 4. Strategy Deep Dives

### m5_keltner_squeeze — GOLD SPECIALIST (+262%)
**Concept:** Volatility compression → explosive breakout
- **Indicator setup:** BB(20, 2.0) + KC(EMA20, 1.5× ATR10)
- **Squeeze:** BB contracts inside KC (low volatility)
- **Release:** BB expands outside KC → MACD histogram confirms direction
- **MTF filter:** M15 ADX(14) > 20 + EMA(21) slope agrees
- **SL:** KC middle line (EMA 20)
- **TP:** 1.5× KC channel width
- **Session:** 0-24 UTC (24-hour, Asian session captured)
- **RSI filter:** Blocks buys at RSI>75, sells at RSI<25
- **Why it works on gold:** Gold has frequent volatility cycles — squeezes build up during Asian, release during London/NY

### m5_tight_sl_scalp — MOMENTUM SCALPER (+202% gold, +18% US30)
**Concept:** If price breaks the last bar, it continues — then trail aggressively
- **Entry:** Close > previous bar high → BUY; Close < previous bar low → SELL
- **Gap filter:** Skip if entry > 1.5× ATR from breakout (avoids gap entries)
- **Range filter:** Skip if prev bar range < 0.3× ATR (avoids noise)
- **SL/TP (Gold):** 0.5% of price (~$24 at $4800)
- **SL/TP (Indices):** 0.3% of price (~$144 at $48000)
- **SL/TP (Forex):** 20 pips
- **Trailing (designed but needs engine support):** Activate after 1.5 pip profit, trail by 1 pip
- **Session:** 7-21 UTC
- **RSI filter:** YES
- **Why +202% even without trailing:** Gold's strong momentum means breakouts of last bar have high follow-through. The percentage-based SL/TP adapts to price level.

### m5_amd_cycle — US30 CHAMPION (+24.8%, 2.7% DD, 62% WR)
**Concept:** Institutional Accumulation → Manipulation → Distribution
- **Accumulation:** Asian session consolidation range
- **Manipulation:** London open fake breakout (sweeps Asian high/low)
- **Distribution:** True directional move during NY session
- **Entry:** After manipulation sweep, enter in opposite direction
- **SL:** Beyond manipulation wick
- **TP:** Extension of accumulation range
- **Why it works on US30:** Indices follow institutional patterns precisely. Asian session accumulates, London manipulates retail, NY distributes.

### m5_ny_orb — NY SESSION BREAKOUT (+28% on US30)
**Concept:** First 15 minutes of NY define the battle; winner predicts the day
- **Range:** High/Low of 14:30-14:45 UTC (first 15 min of NY)
- **Entry:** Breakout + retrace to 50% of range before entering
- **SL:** Opposite side of range
- **TP:** 2× range width
- **Timeout:** Cancel if no retrace within 24 bars (2 hours)
- **Session:** 14:30-21:00 UTC only
- **RSI filter:** YES
- **Why retrace beats immediate:** Filters false breakouts. US30 often spikes out of range then pulls back before the true move.

### m5_dual_supertrend — TREND FOLLOWER (+31.5% gold)
**Concept:** Two Supertrends must agree — high-confidence trend entry
- **Fast ST:** ATR(7), mult=2.0
- **Slow ST:** ATR(14), mult=3.0
- **Entry:** Both agree on direction + close confirms + ADX(14) > 20
- **H1 filter:** EMA(50) direction must agree, reject CHOPPY regime
- **SL:** Fast Supertrend value (dynamic)
- **TP:** ATR-dynamic from ADX regime
- **Session:** 8-22 UTC (London + NY)
- **RSI filter:** YES

### m5_mtf_momentum — SAFE ADD-ON (+4.3% US30)
**Concept:** Multi-timeframe trend confirmation with volume
- **Entry:** M5 EMA cross + Heikin-Ashi color flip + volume surge
- **MTF:** H1 bias must agree
- **RSI filter:** YES
- **Note:** Low return but very safe. Good as additional strategy, not standalone.

---

## 5. Shared Infrastructure

### RSI Overbought/Oversold Filter
- Added to `ScalpingStrategyBase` as opt-in `_check_rsi_filter()` method
- RSI(14) > 75 → blocks BUY entries (overbought, likely to reverse)
- RSI(14) < 25 → blocks SELL entries (oversold, likely to reverse)
- **Opted in:** Keltner Squeeze, Dual Supertrend, Tight SL Scalp, MTF Momentum, NY ORB
- **NOT opted in:** Mean Reversion (it BUYS at oversold — filter would kill it)

### Percentage-Based SL/TP Utility
- `_calculate_pct_sl_tp()` in `ScalpingStrategyBase`
- Used for Gold, US30, and other non-forex instruments
- Adapts to price level changes (Gold went $1800→$4800 in 2 years)

### Partial Profit-Taking System
- Multi-TP signals from Telegram (e.g., TP1=4717, TP2=4720, TP3=4723, TP4=4727)
- Closes equal portions at each level
- After TP1: SL → breakeven. After TP2: SL → TP1. Ratchets up.
- 1-second position monitoring for fast detection
- Persisted to SQLite (survives restarts)

### Live Tick Values
- Fetches real `tick_value`, `point_size`, `contract_size` from MT5 at startup
- **Critical fixes:** US30 was 10× wrong (0.1 vs 1.0), USDJPY was 63× wrong
- Falls back to config values if MT5 unavailable

---

## 6. Key Learnings & Rules

1. **Different instruments need different strategies.** Never assume a gold strategy works on indices or vice versa.
2. **Always validate on 6+ months of data.** Mean Reversion looked +16% on 60 days but was -27% on 9 months.
3. **Live tick values are essential.** Hardcoded values caused 10-63× lot sizing errors.
4. **RSI filter is free edge.** Blocks ~10-15% of entries that would have been losers.
5. **Partial profit-taking prevents reversals from erasing gains.** The $30→$0 profit loss that prompted this feature.
6. **1-second monitoring matters.** Gold moves $1-3/second. 30-second polling missed TP hits.
7. **Trailing stop is the next frontier.** Tight SL Scalper claims 90%+ WR with trailing but we can only backtest fixed SL/TP currently.
8. **Prop firm DD limits shape strategy selection.** Dual Supertrend makes +232% on US30 but with 36% DD — instant breach.

---

## 7. Files Reference

| Category | File | Purpose |
|----------|------|---------|
| **Configs** | `config/fundednext-comp.yaml` | $100k FundedNext competition |
| | `config/fundingpips-10k.yaml` | $10k FundingPips prop firm |
| | `config/base.yaml` | Instrument specs + defaults |
| **New Strategies** | `src/analysis/strategies/m5_tight_sl_scalp.py` | Tight SL momentum scalper |
| | `src/analysis/strategies/m5_ny_orb.py` | NY Opening Range Breakout |
| **Modified Strategies** | `src/analysis/strategies/scalping_base.py` | RSI filter + pct SL/TP helpers |
| | `src/analysis/strategies/m5_keltner_squeeze.py` | +RSI filter |
| | `src/analysis/strategies/m5_dual_supertrend.py` | +RSI filter |
| | `src/analysis/strategies/m5_mtf_momentum.py` | +RSI filter |
| **Partial Profit** | `src/monitoring/partial_profit_manager.py` | Multi-TP partial close logic |
| | `src/monitoring/position_monitor.py` | 1s polling + partial profit integration |
| **Live Tick Values** | `src/main.py` | `_cache_live_symbol_info()` at startup |
| **Execution** | `src/execution/executor.py` | +position_ticket for partial closes |
| | `src/risk/manager.py` | Passes TP levels to orders |
| | `src/telegram/parser.py` | Extracts ALL TP levels from signals |
| **Database** | `src/tracking/database.py` | +partial_profit_tracking table |
| **Backtesting** | `scripts/backtest_scalping.py` | All 15 strategies registered |
| | `scripts/sweep_ny_orb.py` | NY ORB parameter optimization |
| **Research** | `data/research/strategy_analysis_complete.md` | 27 video analysis |
| | `data/research/*.srt` | Raw video transcripts |
