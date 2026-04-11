# Trading Bot V2 — Strategy Guide & Optimal Configuration

## Last Updated: 2026-04-11
## Validated on: 9 months of M5 data (50k-101k bars per instrument)

---

## Strategy Performance Matrix

### XAUUSD (Gold) — 101,741 M5 bars, 9 months

| Strategy | Optimal Risk | Return | Max DD | Win Rate | Trades | Status |
|----------|-------------|--------|--------|----------|--------|--------|
| m5_keltner_squeeze | 1.0% | +262% | ~17% | 22% | High | DEPLOY (24h) |
| m5_tight_sl_scalp | 0.5-1.0% | +86-202% | 9-17% | 52% | 3099 | DEPLOY |
| m5_dual_supertrend | 1.0% | +31.5% | ~28% | 37% | Med | DEPLOY (8-22 UTC) |
| EMA Pullback | 1.0% | Proven live | — | — | Low | DEPLOY |
| London Breakout | 1.0% | Proven live | — | — | Low | DEPLOY |
| m5_ny_orb | — | +23% but 26% DD | 26% | 41% | 589 | DISABLE (DD too high) |
| m5_mean_reversion | — | — | — | — | — | NOT TESTED on gold |

### US30 (Dow Jones) — 50,000 M5 bars, 9 months (Vantage)

| Strategy | Optimal Risk | Return | Max DD | Win Rate | Trades | Status |
|----------|-------------|--------|--------|----------|--------|--------|
| m5_amd_cycle | 1.0% | +24.8% | 2.7% | 62% | 79 | DEPLOY — CHAMPION |
| m5_ny_orb (15min retrace) | 1.0% | +28.0% | 10.5% | 34% | 286 | DEPLOY |
| m5_mtf_momentum | 0.5-1.0% | +2-4% | 6-10% | 35% | 86 | DEPLOY (marginal) |
| m5_tight_sl_scalp | 0.25-0.5% | +9-18% | 10-19% | 51% | 1832 | OPTIONAL (needs trailing) |
| m5_dual_supertrend | 0.25% | +55% | 31% | 34% | 1988 | DISABLE (DD too high for prop) |
| m5_mean_reversion | — | -27% | 33% | 28% | — | DISABLE (overfit) |
| m5_keltner_squeeze | — | -18% | 35% | 17% | — | DISABLE |

### Forex Pairs (USDJPY, GBPUSD, EURUSD) — 50,000 M5 bars

| Strategy | Status | Notes |
|----------|--------|-------|
| m5_tight_sl_scalp | DISABLE | -30% without trailing stop. Needs engine support. |
| m5_ny_orb | MARGINAL | -1.5% to -14%. Not profitable on forex. |
| All M5 scalping | NOT TESTED | Forex pairs primarily use Telegram signal strategies |

---

## Account Size Configurations

### $100,000 Competition (FundedNext)
**Config:** `fundednext-comp.yaml`
**Rules:** 5% daily loss ($5k), 10% max DD ($10k), no profit target

| Instrument | Strategies | Risk |
|-----------|-----------|------|
| XAUUSD | Keltner Squeeze + Dual Supertrend + Tight SL Scalp | 1.0% |
| US30 | AMD Cycle + NY ORB + MTF Momentum | 1.0% |
| Both | EMA Pullback + London Breakout | 1.0% |
| Max lot | 3.00 | |
| Position monitor | 1 second | |
| Partial profit | Enabled | |
| RSI filter | On all except mean reversion | |

### $10,000 Prop Firm (FundingPips 2-Step Standard)
**Config:** `fundingpips-10k.yaml`
**Rules:** 5% daily loss ($500), 10% max DD ($1k), 8% Step 1 target, 5% Step 2

| Instrument | Strategies | Risk |
|-----------|-----------|------|
| XAUUSD | Keltner Squeeze (24h) + Dual Supertrend (8-22 UTC) | 1.0% |
| XAUUSD | Tight SL Scalp + EMA Pullback + London Breakout | 1.0% |
| Max lot | 1.00 | |
| Position monitor | 1 second | |
| Partial profit | Enabled | |
| **Projected pass time** | Step 1: 1-2 weeks, Step 2: ~1 week | |

### $5,000 Prop Firm (FundingPips 2-Step Standard)
Same as $10k but with `max_lot: 0.50` and `account_size: 5000`.
Expected monthly income when funded: ~$150-250/month at 60% split.

---

## Strategy Details

### m5_keltner_squeeze — Best on XAUUSD
- **Entry:** Bollinger Bands compress inside Keltner Channel (squeeze), then expand (release). MACD histogram confirms direction.
- **MTF filter:** M15 ADX > 20 + EMA slope agreement
- **SL:** KC middle line (EMA 20)
- **TP:** 1.5x KC channel width
- **Session:** 0-24 UTC (24-hour trading)
- **RSI filter:** YES (blocks at overbought/oversold)

### m5_tight_sl_scalp — New, Best on XAUUSD
- **Entry:** Close > previous bar high (BUY) or close < previous bar low (SELL)
- **Gap filter:** Skip if entry > 1.5x ATR from breakout level
- **Range filter:** Skip if previous bar range < 0.3x ATR
- **SL/TP:** 0.5% of price (Gold/Indices), 20 pips (Forex)
- **Trailing:** 1.5 pip trigger, 1 pip distance (needs engine support for backtesting)
- **Session:** 7:00-21:00 UTC
- **RSI filter:** YES

### m5_amd_cycle — Best on US30
- **Entry:** Detects Accumulation (Asian range) → Manipulation (fake breakout) → Distribution (true move)
- **Session:** Best during NY session
- **Win rate:** 62% on 9-month data
- **DD:** Only 2.7% — safest strategy

### m5_ny_orb — Strong on US30
- **Entry:** First 15 minutes of NY session (14:30-14:45 UTC) defines range. Wait for breakout, then retrace to 50% of range before entering.
- **SL:** Opposite side of range
- **TP:** 2x range width
- **Session:** 14:30-21:00 UTC only
- **RSI filter:** YES

### m5_dual_supertrend — Good on XAUUSD
- **Entry:** Fast ST(7, 2.0) and Slow ST(14, 3.0) must agree on direction. ADX > 20.
- **MTF filter:** H1 EMA(50) direction must agree
- **SL:** Fast Supertrend value
- **Session:** 8-22 UTC (London + NY)
- **RSI filter:** YES

---

## Key Learnings

1. **Different instruments need different strategies.** Keltner/DST work on gold, lose on US30. AMD Cycle works on US30, not tested on gold.
2. **Mean reversion overfit to 60-day data.** Looked +16% on short test, was -27% on 9 months. Always validate on longer data.
3. **Live tick values are essential.** Fixed config values were 10-63x wrong for US30/USDJPY. Bot now fetches from MT5 at startup.
4. **RSI filter reduces bad entries.** Blocks buys at overbought (>75), sells at oversold (<25). Opt-in per strategy — not used on mean reversion.
5. **Partial profit-taking protects gains.** Multi-TP signals close portions at each level, SL moves to breakeven after TP1.

---

## Files Reference

| File | Purpose |
|------|---------|
| `config/fundednext-comp.yaml` | $100k FundedNext competition config |
| `config/fundingpips-10k.yaml` | $10k FundingPips prop firm config |
| `config/base.yaml` | Base config with instrument specs |
| `src/analysis/strategies/m5_keltner_squeeze.py` | Keltner squeeze strategy |
| `src/analysis/strategies/m5_dual_supertrend.py` | Dual supertrend strategy |
| `src/analysis/strategies/m5_tight_sl_scalp.py` | Tight SL scalper (NEW) |
| `src/analysis/strategies/m5_ny_orb.py` | NY opening range breakout (NEW) |
| `src/analysis/strategies/m5_amd_cycle.py` | AMD cycle strategy |
| `src/analysis/strategies/scalping_base.py` | Base class with RSI filter + pct SL/TP utilities |
| `src/monitoring/partial_profit_manager.py` | Multi-TP partial close manager (NEW) |
| `src/monitoring/position_monitor.py` | 1-second position monitor with partial profit |
| `scripts/backtest_scalping.py` | Backtest runner (all strategies registered) |
| `scripts/sweep_ny_orb.py` | NY ORB parameter optimization sweep |
