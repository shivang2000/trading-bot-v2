# Plan: Tick-Driven HFT Engine for trading-bot-v2

## Context

`trading-bot-v2` already has 6 scalping strategies (M1/M5), a 1-second position monitor, an active trailing-stop manager, and a partial-profit manager — but every entry/exit decision is **bar-driven** (15s scalping loop, 30s position poll). The user wants high-frequency capability: ~100 trades/day at 0.10–0.20 lots on XAUUSD with **tick-level position management**, modeled after a friend's $150/day pattern.

Phase 1 audit found two big surprises:
1. `src/mt5/tick_stream.py` (138 lines, 200ms polling, M5 bar detection) is **already written but never instantiated** in `main.py` or `signal_generator.py`. README/strategy guide says trailing stop is "next frontier" — that is **outdated**: trailing is fully active. Tick streaming is the actual frontier.
2. MT5 RPyC roundtrip floors at ~150–250ms; broker leg adds 50–200ms. Sub-100ms HFT is impossible without an MQL5 EA inside the MT5 container. User chose **Architecture A (Python-only, 200–500ms)** — re-use what exists.

The AI-agents project (`ai-agents-trading-bot-v2/`) is **explicitly deferred** until trading-bot-v2 generates funding profits.

Goal of this work: convert `trading-bot-v2` from a 15-second-loop bar scalper into a **tick-event engine** that keeps the same proven entry signals but manages exits, trailing, and partial closes on every tick. Add one new tick-driven entry strategy as an experiment. Lock down operations so the next prop-firm bust mode (lost EBS, news event drift) cannot recur.

Recommended starting capital and deployment: Vantage demo for 2–4 weeks → $30–100 Vantage live → fresh FundingPips $5k once tick-engine slippage is characterized. Do NOT touch the existing FundingPips $5k Step 2 with the new engine.

---

## Architecture (Python-only, Architecture A)

```
MT5 container (Wine, gmag11/metatrader5_vnc)
   |  RPyC :8001
   v
[AsyncMT5Client]  <-- existing, src/mt5/client.py
   |
   v
[TickStream 200ms]  <-- existing, src/mt5/tick_stream.py (UNWIRED)
   |
   +--> on_tick callback -> [TickPositionManager]   NEW
   |                          |
   |                          +--> TrailingStopManager.update(...) tick-driven
   |                          +--> TrailingStopManager.update_profit_trail(...)
   |                          +--> PartialProfitManager.check_tp_levels(...) tick-driven
   |                          +--> rate_limited modify queue (broker spam guard)
   |
   +--> on_new_bar callback -> [SignalGenerator._scalping_loop]
                                 (existing entry signals stay bar-based)

   [TickVelocityBreakout]  NEW  --> direct on_tick subscription
                                    (separate experimental entry strategy)
```

Key invariant: **bar-driven entry signals are unchanged**. Only exit-side logic and one new experimental strategy listen to ticks.

---

## Phase 0 — Prerequisites & baseline (1 day)

| # | Task | File |
|---|---|---|
| 0.1 | Latency baseline script: 10-min recording of `symbol_info_tick(XAUUSD)` p50/p95/p99 from EC2 → log, set SLO | `scripts/measure_tick_latency.py` (new) |
| 0.2 | Rotate Vantage MT5 password (was leaked in chat earlier this session) | manual + update SSM |
| 0.3 | Confirm MT5 investor-vs-master password split is in place per `docs/post-mortem-5k.md` | runbook check |
| 0.4 | Pin Vantage demo account creds in `.env.vantage-demo`, gitignored | `.env.vantage-demo` |

Pass criterion: tick latency p95 < 350ms over a 10-minute London-session sample. If higher, switch the EC2 region closer to Vantage's MT5 server before continuing.

---

## Phase 1 — Track A: Wire TickStream + tick-driven exit management (3 days)

### 1A.1 Create `src/monitoring/tick_position_manager.py` (NEW, ~250 lines)

Single subscriber to `TickStream.on_tick`. Per tick:

- Look up open bot positions for tick.symbol
- For each position:
  - Call `TrailingStopManager.update(...)` and `update_profit_trail(...)` (already exists, `src/risk/trailing_stop.py`)
  - Call `PartialProfitManager.evaluate_tp_levels(...)` (extend existing — currently poll-driven)
  - If new SL/TP differs from MT5-side, enqueue a modify request

Modify-queue rate limit: max 1 modify per ticket per **2 seconds** (configurable). Broker rejects if too frequent. Drop intermediate updates, keep latest target.

Reuse:
- `src/risk/trailing_stop.py:46` `TrailingStopManager.update`
- `src/risk/trailing_stop.py:116` `update_profit_trail`
- `src/monitoring/partial_profit_manager.py` (extend with `evaluate_on_tick(price)`)
- `src/execution/executor.py:164` order modify via `TRADE_ACTION_SLTP`

### 1A.2 Wire `TickStream` in `src/main.py`

- Instantiate `TickStream(mt5_client, symbols=cfg.tick_symbols, poll_interval_ms=cfg.tick_poll_ms)`
- Register `TickPositionManager.handle_tick` as `on_tick` callback
- Optional: `on_new_bar` → trigger `SignalGenerator.scan_now(symbol)` to remove the dead time between bar close and 15s scan tick

### 1A.3 Demote PositionMonitor's tick-time work, keep health work

`src/monitoring/position_monitor.py` keeps:
- foreign-position scan (`_check_foreign_positions`) — runs every poll cycle
- pre-news flat (`_check_pre_news_flat`)
- close-detection / position reconciliation
- daily-counter reset

Removes:
- per-poll trailing stop update (now tick-driven)
- per-poll partial-profit checks (now tick-driven)

Poll interval can move from 30s → 5s (cheaper now) for faster close-detection of broker-side stop-outs.

### 1A.4 Config wiring (`config/base.yaml`, `config/vantage-50.yaml`)

```yaml
tick_engine:
  enabled: true
  poll_interval_ms: 200
  symbols: [XAUUSD]              # start with one
  modify_rate_limit_seconds: 2.0
  drop_unchanged_modifies: true
```

### 1A.5 Tests

- `tests/unit/test_tick_position_manager.py` — modify-queue dedupe, rate-limit, ratchet correctness
- `tests/unit/test_partial_profit_on_tick.py` — TP levels fire on tick crossing, not after next poll

---

## Phase 1 — Track B: Tick-velocity breakout entry strategy (1–2 weeks)

Experimental new strategy. Lives alongside existing scalpers, off by default.

### 1B.1 `src/analysis/strategies/tick_velocity_breakout.py` (NEW)

Thesis: gold tick velocity > 2σ over rolling 30-second window → momentum follow-through for next 60–120 seconds.

- Maintains rolling window of last 30s of ticks per symbol (deque)
- Computes velocity = (last_tick_price − tick_price_30s_ago) / 30
- Computes σ over last 10 minutes of velocities
- Entry: |velocity| > 2σ AND ADX(M1) > 25 (regime gate using existing pandas_ta) AND not in news blackout
- SL: 0.15% of price (~$5 at $3500), TP: 0.25% (1.67 R:R)
- Lot size: respect `RiskManager.PositionSizer` cap
- Daily limit: max 50 entries from this strategy (separate from base scalper limit)

Subscribed to `TickStream.on_tick` directly (NOT `on_new_bar`).

### 1B.2 Tick-history backtester (`src/backtesting/tick_engine.py` NEW)

Bar-based backtester (`scripts/backtest_scalping.py`) cannot validate tick logic.

- Consumes tick CSV (`<timestamp>,<bid>,<ask>,<volume>`)
- Replays at recorded timestamps
- Same signal interface as live (`TickStream` mock)
- Outputs same metrics format as bar backtester

Need tick history dataset. Two paths: (a) record live demo for 2 weeks, (b) buy 6-month tick CSV from a dataset vendor (Dukascopy free, Tickdata paid). Recommend (a) + (b) in parallel.

### 1B.3 Validation gate

Strategy stays disabled in prod config until:
- 3+ months of replay shows positive expectancy with DD < 8%
- 2 weeks live demo with paper-traded signals matches replay within 20%

---

## Phase 1 — Track C: Operational hardening (2 days)

Per `docs/post-mortem-5k.md`. Lessons from $5k bust must lock in.

| # | Task | Where |
|---|---|---|
| 1C.1 | EBS volume `DeleteOnTermination=false` on prod EC2 | `scripts/bootstrap-ec2.sh` |
| 1C.2 | Nightly `aws s3 sync` of `data/*.db` and `logs/` to S3 with 30-day retention | `scripts/nightly-backup.sh` (new) + cron |
| 1C.3 | Health gate: process exits with CRITICAL if MT5+Telegram both down >5min (matches `docs/PROJECT_KNOWLEDGE.md` design issue) | `src/safety/health_gate.py` |
| 1C.4 | Document MT5 investor-password pattern: master in SSM (bot only), investor for human VNC sessions | `docs/RUNBOOK.md` (new section) |
| 1C.5 | Telegram pre-news alert 30min before high-impact event | `src/monitoring/position_monitor.py:_check_pre_news_flat` extension |

Pass criterion: simulate EC2 termination → verify next launch restores SQLite state from S3 within 5 minutes.

---

## Phase 1 — Track D: MT5 client extensions (1 day)

Fill gaps found in `src/mt5/client.py`. Required for tick backtester (Track B) and faster trailing (Track A).

| # | Function | Why |
|---|---|---|
| 1D.1 | `copy_ticks_from(symbol, flags, fromdt, count)` | Tick history pull for backtester |
| 1D.2 | `copy_ticks_range(symbol, flags, from_dt, to_dt)` | Bulk tick history download |
| 1D.3 | `position_modify(ticket, sl, tp)` named method | Already implemented inline as `TRADE_ACTION_SLTP` (`src/execution/executor.py:164`) — extract to client.py for reuse + cleaner logging |

Tests: `tests/unit/test_mt5_client_ticks.py` against a recorded RPyC fixture.

---

## Phase 2 — Vantage demo validation (2–4 weeks, runs in parallel with Phase 3 prep)

Deploy Phase 1 to a Vantage **demo** account on the same EC2. Collect:

- Tick latency distribution p50/p95/p99 (already from Phase 0 baseline; recheck weekly)
- Modify-rejection rate from broker (should be <1%)
- Slippage per trade (entry, SL hit, TP hit) — log to new `tick_execution_log` SQLite table
- Comparison vs production bar engine: same strategies on a parallel demo with `tick_engine.enabled: false` — measure P&L delta

Pass criteria to promote to live $30 account:
- Slippage p95 ≤ 0.5 pips on XAUUSD
- Zero broker-rejection clusters
- Tick engine demo P&L within ±10% of bar engine demo P&L over 14 days
- Foreign-position monitor never falsely tagged a bot trade
- News blackout caught every event in `config/news_calendar.csv`

---

## Phase 3 — Live promote (after Phase 2 pass)

Tiny live: $30–100 Vantage. `max_lot_per_trade: 0.10`, `max_open_positions: 4`, `tick_engine.enabled: true`.

After 2 weeks profitable: scale to fresh FundingPips $5k. Use **investor password** for any human MT5 session, master only via SSM-injected env into bot container.

---

## Files to create / modify

### New
- `src/monitoring/tick_position_manager.py` — tick subscriber, modify queue
- `src/analysis/strategies/tick_velocity_breakout.py` — experimental tick entry
- `src/backtesting/tick_engine.py` — tick replay backtester
- `scripts/measure_tick_latency.py` — latency baseline
- `scripts/nightly-backup.sh` — S3 sync
- `src/safety/health_gate.py` — exit-on-degraded
- `tests/unit/test_tick_position_manager.py`
- `tests/unit/test_partial_profit_on_tick.py`
- `tests/unit/test_mt5_client_ticks.py`
- `docs/RUNBOOK.md` — investor-password + recovery playbook

### Modify
- `src/main.py` — wire TickStream + TickPositionManager
- `src/mt5/client.py` — add `copy_ticks_from`, `copy_ticks_range`, named `position_modify`
- `src/monitoring/position_monitor.py` — remove per-poll trailing/partial work, keep health
- `src/monitoring/partial_profit_manager.py` — add `evaluate_on_tick(price)` method
- `src/execution/executor.py` — drop hardcoded `1.0s` retry delay, make config-driven
- `config/base.yaml` — `tick_engine` block, demote position-monitor to 5s
- `config/vantage-50.yaml`, `config/vantage-10k-demo.yaml` — enable tick engine
- `scripts/bootstrap-ec2.sh` — DeleteOnTermination=false
- `pyproject.toml` — none expected; verify `pandas-ta` and `aiosqlite` cover tick window aggregation

### Reuse (no edits)
- `src/mt5/tick_stream.py` — already correct
- `src/risk/trailing_stop.py` — already correct, called per-tick now
- `src/risk/manager.py` — guards already cover HFT (max_daily_trades=200)
- `src/risk/prop_firm_guard.py` — buffers already in place
- `src/risk/position_sizer.py` — handles 0.01–0.50 lot range

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Per-trade risk cap ($100 on $5k) reduces 0.20-lot orders to 0.05 with 20-pip SL | Use 10-pip SL on tick scalp OR raise per-trade cap to 2.5% on tick strategies (still inside FundingPips 3% rule) |
| Broker rejects rapid `position_modify` calls | 2-second per-ticket modify rate limit; log + retry-on-throttle |
| Tick poll lag spikes during news → stale SL | Pre-news flat already closes positions 5min before high-impact events; tick engine inherits same gate |
| Vantage RPyC connection drops mid-session | Existing `_call_with_reconnect()` exponential backoff in `AsyncMT5Client` (10 attempts, 60s cap) |
| Tick backtester data quality (Dukascopy gaps) | Use ≥3 months and filter sessions with <80% tick density |
| Operator runs manual trade during tick session (caused $5k bust) | Foreign-position monitor + investor-password runbook (Track C) |

---

## Verification

End-to-end happy path on Vantage demo EC2:

1. Start: `docker compose -f docker-compose.ec2.yml up -d`
2. Tail logs: `docker logs -f trading-bot-v2`. Expect lines:
   - `TickStream started: 1 symbols, 200ms interval`
   - `TickPositionManager: subscribed to tick stream`
3. Open one M5 Tight SL Scalp position manually (force a trade via `scripts/force_test_trade.py` or wait for live signal)
4. Watch logs at next 5 ticks (~1 second of wall time):
   - `Trailing stop moved: ticket=X ...` should fire when price moves favorably
5. Run `scripts/measure_tick_latency.py` for 5 minutes → assert p95 < 350ms
6. Force partial-TP scenario (test signal with TP1/TP2/TP3) → confirm partial close fires within 1 tick of TP1 cross, not at next 30s poll
7. Kill the EC2 instance → relaunch → confirm SQLite restored from S3 and trailing stops resume from `trailing_stops` table
8. Open foreign position via VNC manually → expect Slack/Telegram alert within one poll cycle
9. Run unit tests: `pytest tests/unit/ -k "tick or trail or partial"` → all green
10. After 14 days demo: run report `python3 scripts/generate_propfirm_report.py --tick-engine-compare` (extend existing) → tick-engine vs bar-engine P&L delta within ±10%

Promote to live only if all 10 pass.

---

## Out of scope (explicitly deferred)

- `ai-agents-trading-bot-v2/` orchestration project — funded later from this engine's profits
- MQL5 EA path (Architecture B) — revisit if Phase 2 latency proves insufficient
- Direct broker FIX (Architecture C) — not on the roadmap
- New ML/AI signal models — `ClaudeSignalFilter` stays as-is
- Forex pairs — research already showed M5 scalping doesn't work on EURUSD/GBPUSD/USDJPY without trailing-engine support
