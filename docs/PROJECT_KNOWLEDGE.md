# Trading Bot V2 — Complete Project Knowledge Base

> Last updated: 2026-03-25
> This file serves as the master reference for all architecture decisions, version history,
> bugs found, fixes applied, backtesting results, and lessons learned.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture](#2-architecture)
3. [Version History](#3-version-history)
4. [Strategies — What Works & What Doesn't](#4-strategies)
5. [Backtesting Results](#5-backtesting-results)
6. [Bugs Found & Fixed](#6-bugs-found--fixed)
7. [Configuration Reference](#7-configuration-reference)
8. [Infrastructure & Deployment](#8-infrastructure--deployment)
9. [Research Findings](#9-research-findings)
10. [Lessons Learned](#10-lessons-learned)
11. [Future Improvements](#11-future-improvements)

---

## 1. Project Overview

**What:** Automated trading bot that executes trades on MT5 via RPyC, using Telegram channel signals + own technical signal generation.

**Instruments:** XAUUSD (Gold), XAGUSD (Silver), BTCUSD (Bitcoin), ETHUSD (Ethereum)

**Broker:** VantageInternational-Demo (MT5), Login 11836008, 1:500 leverage

**EC2:** `ec2-13-202-138-193.ap-south-1.compute.amazonaws.com`
- SSH key: `south-mumbai-key-pair.pem`
- Docker containers: `metatrader5` (MT5+VNC) + `trading-bot-v2` (Python bot)
- VNC/noVNC: port 8080 (password: `botpass`)

**GitHub:** `shivang2000/trading-bot-v2` (private)

**Tech Stack:** Python 3.12, asyncio, RPyC, Telethon, Claude Haiku (signal parsing), pandas_ta, SQLite, Docker

---

## 2. Architecture

### Signal Flow
```
┌─────────────────────────────────────────────────────────┐
│                    SIGNAL SOURCES                         │
├──────────────┬──────────────────────────────────────────┤
│  Telegram    │  Technical Signal Generator               │
│  3 channels  │  EMA Pullback + London Breakout           │
│  Claude +    │  Scans every 60s during London/NY         │
│  regex parse │  60s startup delay (stale signal guard)   │
├──────────────┴──────────────────────────────────────────┤
│              REGIME FILTER (H1 timeframe)                 │
│  TRENDING_UP/DOWN → strategies run                       │
│  VOLATILE_TREND → strategies run (EMA slope for dir)     │
│  CHOPPY → blocked for EMA/London, allowed for NY*        │
│  RANGING → blocked for all                               │
├──────────────────────────────────────────────────────────┤
│              SESSION FILTER                               │
│  London (08:00-13:00), NY (13:00-22:00)                  │
│  London/NY Overlap (13:00-17:00) = BEST                  │
│  Asian (00:00-08:00) = skip for forex                    │
│  Weekend (Fri 22:00 → Sun 22:00) = skip all              │
├──────────────────────────────────────────────────────────┤
│              CONFIDENCE GATE (min 0.45)                    │
├──────────────────────────────────────────────────────────┤
│        RISK MANAGER                                       │
│  Max 2 positions (1 per symbol)                          │
│  2% risk per trade, dynamic lot sizing                   │
│  Free margin check (>20% equity)                         │
│  Daily loss limit (8%), drawdown limit (20%)             │
│  Emergency stop: close all if daily loss > 8%            │
├──────────────────────────────────────────────────────────┤
│        ORDER EXECUTOR → MT5 via RPyC                      │
├──────────────────────────────────────────────────────────┤
│        TRAILING STOP MANAGER                              │
│  ATR-based (1.5x), activates at 20% of TP distance      │
│  Ratchets forward only, never backward                   │
├──────────────────────────────────────────────────────────┤
│        STATE PERSISTENCE (SQLite)                         │
│  bot_positions, trailing_stops, strategy_state,          │
│  daily_state — all survive restarts                      │
└──────────────────────────────────────────────────────────┘
```

### Key Files
| File | Purpose |
|------|---------|
| `src/main.py` | Entry point, component wiring, startup sequence |
| `src/analysis/signal_generator.py` | Timer-based strategy scanner (60s interval) |
| `src/analysis/strategies/ema_pullback.py` | 4-phase state machine strategy |
| `src/analysis/strategies/london_breakout.py` | Asian range breakout strategy |
| `src/analysis/regime.py` | Market regime detection (ADX+EMA+ATR+BB) |
| `src/analysis/sessions.py` | Trading session management |
| `src/risk/manager.py` | Risk validation, confidence gate, position limits |
| `src/risk/trailing_stop.py` | ATR-based trailing stop manager |
| `src/execution/executor.py` | MT5 order execution via RPyC |
| `src/telegram/parser.py` | Claude Haiku + regex fallback signal parsing |
| `src/telegram/listener.py` | Telethon-based channel listener |
| `src/monitoring/position_monitor.py` | 30s poll loop, trailing stops, emergency stop |
| `src/tracking/database.py` | SQLite persistence for all state |
| `src/backtesting/engine.py` | Bar-by-bar replay backtester |
| `src/backtesting/account.py` | Simulated account for backtesting |
| `src/backtesting/result.py` | 15+ performance metrics (Sharpe, PF, WR, etc.) |
| `config/base.yaml` | All configuration parameters |

### Telegram Channels
| Channel | ID | Instruments |
|---------|-----|------------|
| YoForex Gold | -1002130822880 | XAUUSD, XAGUSD |
| TradeLikeMalika | -1002041007049 | Mixed |
| VidollaRugForexTrade | -1001936759409 | Mixed |

---

## 3. Version History

| Version | Date | Description | Key Change |
|---------|------|-------------|-----------|
| v1.0.0-baseline | Mar 22 | First working backtest | +3.95% / 9mo ($10K) |
| v1.1.0-trailing-primary | Mar 22 | Trailing stop as exit | +6.86% / 9mo |
| v1.2.0-compounding | Mar 22 | Dynamic position sizing | +42.59% / 9mo |
| v1.3.0-wide-sl | Mar 22 | Config D winner (SL 3.5x ATR) | +2,449% / 9mo ($30) |
| v1.4.0-pnl-fix | Mar 22 | P&L formula fix (wrong tick_values) | Corrected calculation |
| v1.5.0-correct-specs | Mar 22 | Real MT5 contract specs from broker | XAUUSD tick=1.0, XAG=5.0 |
| v1.6.0-final | Mar 22 | 40-backtest verified config | Comprehensive validation |
| v1.6.1-deadlock-fix | Mar 23 | Fixed RiskManager deadlock | First live trade! |
| v1.7.0-persistence | Mar 23 | State survives restarts (SQLite) | bot_positions, trailing_stops tables |
| v1.7.1-sync-fix | Mar 23 | Pre-sync MT5 positions on boot | No duplicate positions |
| v1.7.2-startup-delay | Mar 23 | 60s delay + reduced limits | max_pos=2, per_sym=1 |
| v1.8.0-stable | Mar 23 | Stale DB cleanup + margin check | Free margin gate |
| v1.9.0-final | Mar 23 | Weekend guard + spread check | All 7 protections |
| v2.0.0 | Mar 23 | Feature complete (all improvements) | Emergency stop, per-instrument config |
| v2.0.1 | Mar 23 | Scan loop fix + VOLATILE_TREND | Scans actually work |
| v2.1.0-ny-strategies | Mar 23 | NY Range Breakout + Momentum | Later disabled by data |
| v3.0.0-final | Mar 24 | Backtested winner: EMA+London only | NY disabled (hurts performance) |
| v3.0.1-bugfix | Mar 25 | EMA re-arm fix + London timestamp | London Breakout works for first time |
| v3.0.2-stale-signals | Mar 25 | No trades on restart from stale conditions | Stale breakout detection |

---

## 4. Strategies

### ACTIVE: EMA Pullback (M15) — PRIMARY
```
State machine: SCANNING → ARMED → WINDOW_OPEN → ENTRY
Entry: EMA(8) crosses EMA(21) + H1 regime trending + pullback + breakout
SL: 3.5x ATR(14), TP: 10x ATR (trailing stop exits)
Backtested: XAUUSD +2,517%, 63.5% WR, 67% DD ($30, 9mo)
```

### ACTIVE: London Breakout (M15)
```
Marks Asian range (00:00-07:00 UTC high/low)
Trades breakout during 07:00-12:00 UTC
SL: opposite side of range, TP: 5x range width
Stale detection: skips if price already outside range on startup
Backtested: complements EMA Pullback, adds ~30% more trades
```

### DISABLED: NY Range Breakout
```
Marks 13:00-14:00 range, trades breakout 14:00-18:00
Result: LOSES money on Gold (-763%), BTC (-345%), ETH marginal
Disabled in v3.0.0 based on 24-backtest evidence
```

### DISABLED: NY Momentum (RSI+MACD)
```
RSI(14) cross 50 + MACD histogram + EMA(21) filter
Result: -140% BTC, -2% ETH, +1,898% Gold standalone but adds drawdown
Disabled in v3.0.0 — overall HURTS combined performance
```

### RESEARCHED & REJECTED: M5/M1 Scalping (research/scalping branch)
```
M5 Mean Reversion RSI: -813% at RSI 20/80, -249% at RSI 10/90. Gold does NOT mean-revert on M5.
M5 BB Squeeze: +145% but 299% DD. Too few trades, unreliable.
M1 EMA Micro: 93% WR with wide SL (useless), 11% WR with tight SL. No edge.
Conclusion: M15 swing trading decisively beats M1/M5 scalping for automated Gold.
```

---

## 5. Backtesting Results

### Winning Config (v3.0.0, $30 account, 9 months, EMA+London)

| Instrument | Return | Max DD | Win Rate | PF | Trades |
|-----------|--------|--------|----------|------|--------|
| XAUUSD | +2,517% | 67% | 63.5% | 1.33 | 104 |
| XAGUSD | +3,267% | 108% | 57.7% | ~1.3 | 111 |
| BTCUSD | +556% | 80% | 57.9% | ~1.3 | 133 |
| ETHUSD | +206% | 15% | 62.9% | ~1.4 | 140 |

### MT5 Contract Specs (verified from broker)

| Symbol | point | tick_value | contract_size | Margin/0.01 lot |
|--------|-------|-----------|--------------|----------------|
| XAUUSD | 0.01 | 1.0 | 100 | ~$8.50 |
| XAGUSD | 0.001 | 5.0 | 5000 | ~$0.60 |
| BTCUSD | 0.01 | 0.01 | 1 | ~$1.40 |
| ETHUSD | 0.01 | 0.01 | 1 | ~$0.04 |

### P&L Formula
```python
pnl = (price_diff / point_size) * tick_value * volume
```

---

## 6. Bugs Found & Fixed

### Critical Bugs (blocked all trading)

| Bug | Root Cause | Fix | Version |
|-----|-----------|-----|---------|
| No trades for a week | Invalid Anthropic API key (401) | Updated .env on EC2 | Session start |
| RiskManager deadlock | `asyncio.run_coroutine_threadsafe` from within event loop | Cached wrappers (non-blocking) | v1.6.1 |
| Duplicate trades on restart | Signal generator fires before PositionMonitor syncs | 60s startup delay + pre-sync | v1.7.1/v1.7.2 |
| Scan loop silent death | Old Docker image running (code SCP'd but not rebuilt) | Always rebuild Docker after deploy | v2.0.1 |
| VOLATILE_TREND blocks strategies | Not mapped to is_up/is_down flags | Use H1 EMA slope for direction | v2.0.1 |
| London Breakout never fires | `time` column is unix seconds, pd.Timestamp parses as nanoseconds | Use DatetimeIndex first, parse int with unit="s" | v3.0.1 |
| EMA Pullback re-arms every 60s | Same crossover detected 15x per M15 candle | Track last_crossover_bar index | v3.0.1 |
| Stale trades on every restart | Strategies detect CONDITIONS not EVENTS | First-scan observe-only + stale breakout detection | v3.0.2 |

### Design Issues

| Issue | Impact | Resolution |
|-------|--------|-----------|
| Bot reports "LIVE" while dead | False confidence, silent failure for a week | Health gate: exit with CRITICAL if MT5+Telegram both down |
| Docker not enabled on boot | EC2 reboot → bot stays down | `systemctl enable docker` |
| No heartbeat monitoring | Zombie bot not detected | EventBus heartbeat file + Docker healthcheck |
| Signal parser blocking on Claude | Claude 401 → no trades | Regex fallback parser |
| Account went to $0 from restarts | Multiple duplicate positions drained margin | Fixed with startup delay + stale detection |

---

## 7. Configuration Reference

### Current Live Config (v3.0.2, main branch)
```yaml
account:
  risk_per_trade_pct: 2.0
  max_lot_per_trade: 0.50

risk:
  max_open_positions: 2
  max_positions_per_symbol: 1
  max_daily_trades: 10
  max_daily_loss_pct: 8.0
  max_drawdown_pct: 20.0

signal_parser:
  model: claude-haiku-4-5-20251001
  min_confidence: 0.45
  atr_sl_multiplier: 3.5
  atr_tp_multiplier: 10.0

trailing_stop:
  atr_multiplier: 1.5
  activation_pct: 0.2

signal_generator:
  scan_interval_seconds: 60
  instruments: [XAUUSD, XAGUSD, BTCUSD, ETHUSD]
  allowed_sessions: [london, new_york, london_ny_overlap, asian_london_overlap]

strategies:
  ema_pullback:
    enabled: true
    fast_ema: 8, slow_ema: 21, trend_ema: 50
    atr_sl_multiplier: 3.5, atr_tp_multiplier: 10.0
    pullback_max_candles: 5, entry_window_candles: 2

  london_breakout:
    enabled: true
    breakout_buffer_pips: 3.0, tp_multiplier: 5.0
    max_trades_per_day: 2

  ny_momentum:
    enabled: false  # backtested: HURTS performance

  smc_confluence:
    enabled: true  # optional — smartmoneyconcepts not installed
```

### Per-Instrument Overrides
```yaml
instrument_overrides:
  ETHUSD:
    risk_per_trade_pct: 3.0
    atr_sl_multiplier: 2.5
  XAGUSD:
    risk_per_trade_pct: 1.0  # extreme tick_value
```

---

## 8. Infrastructure & Deployment

### Deployment Commands
```bash
# SSH into EC2
ssh -i south-mumbai-key-pair.pem ec2-user@ec2-13-202-138-193.ap-south-1.compute.amazonaws.com

# Deploy changes (from local Mac)
scp -i KEY file HOST:~/trading-bot-v2/path
ssh HOST "cd ~/trading-bot-v2 && docker build -t trading-bot-v2-trading-bot . && docker rm -f trading-bot-v2 && docker run -d --name trading-bot-v2 --restart unless-stopped --network trading-bot-v2_default --env-file .env -e MT5_HOST=metatrader5 -e MT5_PORT=8001 -v \$(pwd)/config:/app/config:ro -v \$(pwd)/data:/app/data -v \$(pwd)/logs:/app/logs -v \$(pwd)/scripts/healthcheck.sh:/app/scripts/healthcheck.sh:ro --health-cmd='sh /app/scripts/healthcheck.sh' --health-interval=60s --health-timeout=5s --health-retries=3 --health-start-period=120s --memory=512m trading-bot-v2-trading-bot"

# Check logs
ssh HOST "docker logs trading-bot-v2 --tail 30"

# VNC into MT5
http://ec2-13-202-138-193.ap-south-1.compute.amazonaws.com:8080  (pass: botpass)
```

### Protections in Place
| Protection | What |
|-----------|------|
| 60s startup delay | Signal generator waits for position sync |
| Pre-sync positions | Loads MT5 positions before scanning |
| Stale signal detection | Skips breakouts/crossovers that happened before restart |
| Stale DB cleanup | Marks orphan positions as closed on startup |
| Free margin check | Rejects if free margin < 20% equity |
| Weekend guard | Skip Fri 22:00 → Sun 22:00 UTC |
| Emergency stop | Closes all positions if daily loss > 8% |
| Docker healthcheck | Heartbeat file, auto-restart on zombie |
| Docker on boot | systemctl enabled |
| State persistence | SQLite: positions, trailing stops, daily count |

---

## 9. Research Findings

### Scalping Research (research/scalping branch)

**Sources:** BabyPips, QuantifiedStrategies, TradeLikeMaster, XauBot, Elirox, XS.com, MQL5

**Key Findings:**
1. Traditional RSI 30/70 doesn't work on M1/M5 Gold — too much noise
2. Even extreme RSI (10/90) fails: Gold doesn't mean-revert on short timeframes
3. RSI sweep results: 7-11% WR across ALL thresholds (10/90, 15/85, 20/80, 25/75, 30/70)
4. M5 BB Squeeze: +145% but 299% DD — unreliable
5. M1 EMA Micro: 93% WR with wide SL (no real edge), 11% with tight SL
6. **Conclusion: M15 swing trading decisively beats M1/M5 scalping for automated Gold**

### NY Session Research

**Finding:** NY strategies (Range Breakout + Momentum) backtested WORSE than EMA+London on 3/4 instruments:
- Gold: NY Range Breakout -763%
- BTC: NY strategies -140% / -345%
- ETH: All 4 combined (+189%) WORSE than EMA+London alone (+206%)
- Silver: All 4 combined (+2,148%) WORSE than EMA+London (+3,267%)

### What DOES Work
- EMA(8/21) crossover with H1 regime confirmation on M15
- London session Asian range breakout
- Trailing stop as primary exit (not fixed TP)
- Dynamic position sizing (compounds with equity growth)
- Wide SL (3.5x ATR) — gives Gold room through intrabar noise

---

## 10. Lessons Learned

### Trading
1. **Regime filter is the #1 edge** — preventing trades in CHOPPY markets avoids 30-40% of losing trades
2. **Wide SL > tight SL** for Gold — Gold swings 2-3x ATR within a single M15 candle
3. **Trailing stop as exit > fixed TP** — lets winners run, catches the big moves
4. **M15 > M5 > M1** for automated trading — shorter timeframes = more noise = lower WR
5. **Dynamic sizing is critical** — compounds gains as equity grows
6. **$100 minimum for real money** — $30 too tight for margin with 2 positions

### Engineering
7. **Always rebuild Docker image** — SCP'ing files without rebuilding = running old code
8. **60s startup delay prevents duplicate trades** — scan AFTER position sync, not before
9. **Strategies must detect EVENTS not CONDITIONS** — conditions persist across restarts, events don't
10. **Cache MT5 state locally** — calling MT5 async from sync context = deadlock
11. **Test backtest P&L formula with known values** — tick_value errors compound invisibly
12. **Per-instrument config matters** — Silver's tick_value=5.0 creates extreme P&L swings

### Process
13. **Backtest before deploying** — NY strategies looked good in theory, data said otherwise
14. **Tag every version** — enables instant rollback to any known-good state
15. **Don't restart during open positions** — unless you've verified sync works
16. **Separate research branches** — keep main stable, experiment on branches

---

## 11. Future Improvements

### Short-term (when ready)
- [ ] Verify live win rate matches backtest after 50+ trades
- [ ] Push all commits to GitHub (`git push`)
- [ ] Set up Slack webhook for real notifications
- [ ] Consider $100-200 real money deployment after 2 weeks demo

### Medium-term
- [ ] Per-instrument optimal config (ETH 3% risk, Silver 1% risk)
- [ ] Multi-instrument backtester (shared account simulation)
- [ ] ICT Smart Money Confluence (when smartmoneyconcepts package fixes pandas dep)
- [ ] Adaptive Half-Kelly position sizing (after 50+ trades establish statistics)

### Long-term
- [ ] Web dashboard for monitoring trades/equity
- [ ] Automated daily performance reports
- [ ] Second MT5 container for real + demo simultaneous
- [ ] Additional Telegram signal channels
- [ ] Machine learning on historical trade outcomes

---

## Recommended Starting Capital

| Capital | Max DD (backtest) | Buffer after DD | Margin Call Risk | Verdict |
|---------|------------------|----------------|-----------------|---------|
| $30 | $20 (67%) | $0 | VERY HIGH | Demo only |
| $50 | $33 (67%) | $7 | HIGH | Risky |
| $100 | $67 (67%) | $23 | MEDIUM | Minimum for real |
| $200 | $134 (67%) | $56 | LOW | Recommended |
| $500 | $335 (67%) | $155 | MINIMAL | Comfortable |

**Scaling plan:** Start $100 with max_positions=1. Increase to max_positions=2 at $200 equity. Consider 3% risk at $500+ equity.
