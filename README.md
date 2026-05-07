# Trading Bot V2

Two independent trading systems on a shared codebase, both targeting MT5/Vantage:

1. **`trading-bot-v2`** — automated XAUUSD strategy bot with 6 active strategies, prop-firm safety guard, news filter, and tick-driven exit engine.
2. **`copy-trader-bot`** — pure-mirror system that copies trades from Account A (operated by a human partner) onto Account B in real-time. No strategies, no signals, no automated decisions — pure replication.

Both stacks share the read-only utilities (`src/mt5/client.py`, `src/core/`) but run as separate Docker stacks with no cross-talk.

---

## Quick Index

| Want to… | Go to |
|---|---|
| Deploy the strategy bot to EC2/Hetzner | [`docs/AWS_DEPLOYMENT.md`](docs/AWS_DEPLOYMENT.md) + [`docs/RUNBOOK.md`](docs/RUNBOOK.md) |
| Set up the copy-trader bot (mirror A→B) | [`docs/COPY_TRADER_RUNBOOK.md`](docs/COPY_TRADER_RUNBOOK.md) |
| Understand the design / decisions | [`docs/superpowers/specs/`](docs/superpowers/specs/) |
| Read the post-mortem of the $5k bust | [`docs/post-mortem-5k.md`](docs/post-mortem-5k.md) |
| Run a backtest | `python3 scripts/backtest_scalping.py …` (legacy) or `python3 scripts/backtest_evidence_gated.py …` (new walk-forward) |
| Pull tick history | `python3 scripts/download_dukascopy_xauusd_ticks.py …` |
| Run unit tests | `pytest tests/unit/` (148 tests, ~3s) |

---

## System 1 — Trading Bot V2 (strategy bot)

### Active strategies (live-deployable)

| Strategy | Type | TF | Backtest | Config flag |
|---|---|---|---|---|
| **m5_mtf_momentum** | Scalping | M5 | PF 1.36, DD 10% | `strategies.scalping.strategies_enabled[m5_mtf_momentum]` |
| **m5_keltner_squeeze** | Scalping | M5 | PF 1.20, DD 19% | `strategies.scalping.strategies_enabled[m5_keltner_squeeze]` |
| **m5_dual_supertrend** | Scalping | M5 | PF 1.15 | `strategies.scalping.strategies_enabled[m5_dual_supertrend]` |
| **ema_pullback** | Swing | M15 | (live tested) | `strategies.ema_pullback.enabled` |
| **london_breakout** | Session | M15 | 65-70% directional historically | `strategies.london_breakout.enabled` |
| **smc_confluence** | Booster (not standalone) | M15 | confidence boost +0.10-0.15 | `strategies.smc_confluence.enabled` |

### Tick-driven exit engine (PR #4, off-by-default)

`tick_engine.enabled: true` activates:
- Tick-rate trailing stops (was 30s poll → now per-tick)
- Tick-rate partial profit closes
- Modify-rate-limited (8s/ticket, FTMO 2k req/day cap headroom)
- Promotion gated by [`docs/RUNBOOK.md` §5](docs/RUNBOOK.md)

### Strategies built but archived (DO_NOT_DEPLOY)

| Strategy | Reason |
|---|---|
| `xauusd_ny_orb_tick_breakout` | 8-yr backtest: 831 trades, 36.5% WR, PF 0.34, **-$45,250 P&L**. Documented failure modes in module docstring. |
| `xauusd_pullback_window_state_machine` | Phase machine too restrictive — 1 trade in 8 years. Cannot validate edge or no-edge with N=1. |
| `tick_velocity_breakout` | Scaffold, never validated |

### Disabled scalping strategies (in pool but unset)

`m5_vwap_mean_reversion`, `m5_stochrsi_adx`, `m5_bb_squeeze`, `m5_mean_reversion`, `m1_heikin_ashi_momentum`, `m1_rsi_scalp`, `m1_supertrend_scalp`, `m1_ema_micro` — all backtested negative with cost model.

### Architecture (strategy bot)

```
                Telegram listener (parses signal channels)
                        │
                        ▼
                Signal Generator (dual loop)
                ├── _scan_loop (60s)     → ema_pullback, london_breakout (M15)
                ├── _scalping_loop (15s) → m5_mtf_momentum, m5_keltner_squeeze, m5_dual_supertrend
                ├── Claude AI Filter     → optional pre-trade gate (claude_filter.enabled)
                └── SMC Confluence       → confidence booster
                        │
                        ▼
                RiskManager
                ├── PropFirmGuard        → daily DD, overall DD, profit-target halt, payout reset
                ├── NewsEventFilter      → 142 events, +/- pre-news flat
                ├── PositionSizer        → leverage-aware lot sizing
                └── Directional Cap      → max 2 same-direction
                        │
                        ▼
                OrderExecutor → AsyncMT5Client (RPyC) → MT5 container → Vantage
                        │
                        ▼
                PositionMonitor (30s poll)
                ├── Trailing stops       (off when tick_engine ON)
                ├── Partial-profit ladder (off when tick_engine ON)
                └── Foreign-position alert
                        │
                        ▼
                TickStream (200ms, when tick_engine.enabled)
                └── TickPositionManager → trailing/partial at tick rate
                        │
                        ▼
                StrategyHealthMonitor (always on)
                └── 8 signals: spread regime, slippage drift, modify rejection,
                    ATR expansion, WR degradation, hold-time floor breach,
                    DD proximity, trade frequency spike
```

### Tools

- **Walk-forward validator + DSR** ([`src/backtesting/walk_forward_validator.py`](src/backtesting/walk_forward_validator.py)): rolling train/test windows, Bailey & López de Prado Deflated Sharpe Ratio, ship/no-ship gate
- **Tick replay** ([`src/backtesting/ny_orb_replay_adapter.py`](src/backtesting/ny_orb_replay_adapter.py), [`pullback_window_replay_adapter.py`](src/backtesting/pullback_window_replay_adapter.py)): drives strategy from Dukascopy tick CSVs
- **Dukascopy downloader** ([`scripts/download_dukascopy_xauusd_ticks.py`](scripts/download_dukascopy_xauusd_ticks.py)): rate-limited bi5 fetcher, gap detection, 8-year XAUUSD ≈ 3.6 GB compressed
- **AWS provisioner** ([`scripts/aws-provision.sh`](scripts/aws-provision.sh)): one-shot EC2 + EBS + IAM + SSM + S3 + SG + auto-public-IP

### Quick start (strategy bot on AWS)

```bash
# One-time
aws configure   # IAM user with admin access
[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -t ed25519

# Deploy
export AWS_REGION=ap-south-1
export ACCOUNT_NAME=vantage-50
export REPO_URL=https://github.com/shivang2000/trading-bot-v2.git
./scripts/aws-provision.sh
```

Outputs Elastic-IP-free public IP. Cost ~$42/mo on t3.medium → 2.4 months runway on $100 AWS credit. See [`docs/AWS_DEPLOYMENT.md`](docs/AWS_DEPLOYMENT.md) for full step-by-step + Hetzner migration plan.

### Quick start (strategy bot locally)

```bash
echo "CONFIG_OVERLAY=fundingpips-5k" > .env
echo "ANTHROPIC_API_KEY=your-key" >> .env  # optional
docker compose -f docker-compose.ec2.yml up -d
docker logs -f trading-bot-v2
```

---

## System 2 — Copy-Trader Bot

Mirrors trades from one Vantage account to another. Pure replicator — **no strategies run on either account**.

### Use case

A human partner trades Account A. You own both A and B (same Vantage broker). The copy-trader detects every trade A places and replicates it on B 1:1, with SL/TP modifications synced too. Profit on A is split with the partner; profit on B is fully yours.

### Architecture

```
                Process: copy-trader-bot   (NEW container, no strategy code)
                ├── AsyncMT5Client (source) ──► mt5-source container :8001 ──► Vantage A
                ├── AsyncMT5Client (dest)   ──► mt5-dest   container :8001 ──► Vantage B
                ├── CopyTrader.poll_loop()         (100ms cycle)
                │     ├── on_new_position(A)        → open(B)
                │     ├── on_modified_position(A)   → modify(B)
                │     └── on_closed_position(A)     → close(B)
                ├── MirrorJournal (SQLite, audit + restart-recovery)
                └── SlackNotifier (per-event)
```

### Hard isolation

- **Different docker-compose stack** (`docker-compose.copytrader.yml`)
- **Different Docker image** (`Dockerfile.copytrader`) — `src/analysis/` (where all strategies live) is NOT copied into the image, so strategies physically cannot run
- **Different config** (`config/copy_trader.yaml`) with no `strategies` section
- **Different SQLite** (`data/mirror_journal.db`)

### Mirror guarantees (verified by 19 unit tests)

- Pre-existing positions on A at boot are NOT mirrored
- New positions on A mirror to B within ~300ms
- SL/TP changes on A propagate to B (with min-points threshold to ignore decimal noise)
- A close → immediate B close
- Bot restart restores `mirror_map` from journal — no double-mirroring
- Volume below `lot_min` permanently ignored (alert + add to ignored set)
- All events audited in `mirror_journal.db` with success/failure + raw broker response

### Quick start (copy-trader)

```bash
cat > .env <<EOF
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
MT5_SOURCE_LOGIN=12345678
MT5_DEST_LOGIN=87654321
EOF

./scripts/start-copytrader.sh

# Then once for each MT5:
#   http://<host>:8081 → noVNC for source → log in Account A → AutoTrading
#   http://<host>:8082 → noVNC for dest   → log in Account B → AutoTrading

docker compose -f docker-compose.copytrader.yml restart copy-trader-bot
./scripts/start-copytrader.sh logs
```

See [`docs/COPY_TRADER_RUNBOOK.md`](docs/COPY_TRADER_RUNBOOK.md) for promotion checklist + recovery scenarios.

---

## Key files

### Strategy bot
- [`src/main.py`](src/main.py) — entrypoint
- [`src/analysis/signal_generator.py`](src/analysis/signal_generator.py) — dual-loop scanner
- [`src/analysis/strategies/`](src/analysis/strategies/) — all strategy implementations
- [`src/risk/manager.py`](src/risk/manager.py) — risk gate + central news filter
- [`src/risk/prop_firm_guard.py`](src/risk/prop_firm_guard.py) — FundingPips safety
- [`src/monitoring/tick_position_manager.py`](src/monitoring/tick_position_manager.py) — tick-driven exits
- [`src/monitoring/strategy_health_monitor.py`](src/monitoring/strategy_health_monitor.py) — 8 early-warning signals
- [`src/mt5/client.py`](src/mt5/client.py), [`src/mt5/tick_stream.py`](src/mt5/tick_stream.py) — MT5 connectivity
- [`config/base.yaml`](config/base.yaml) — full strategy config, prop-firm overrides via `fundingpips*.yaml`
- [`docker-compose.ec2.yml`](docker-compose.ec2.yml) — EC2 deployment stack

### Copy-trader bot
- [`src/copy_trader/copy_trader.py`](src/copy_trader/copy_trader.py) — core poll + mirror logic
- [`src/copy_trader/mirror_journal.py`](src/copy_trader/mirror_journal.py) — SQLite audit + state
- [`src/copy_trader/notifier.py`](src/copy_trader/notifier.py) — Slack
- [`src/copy_trader/main.py`](src/copy_trader/main.py) — entrypoint
- [`src/config/copy_trader_schema.py`](src/config/copy_trader_schema.py) — pydantic config
- [`config/copy_trader.yaml`](config/copy_trader.yaml) — sample config
- [`Dockerfile.copytrader`](Dockerfile.copytrader), [`docker-compose.copytrader.yml`](docker-compose.copytrader.yml) — stack
- [`scripts/start-copytrader.sh`](scripts/start-copytrader.sh) — helper

### Backtesting + research
- [`src/backtesting/walk_forward_validator.py`](src/backtesting/walk_forward_validator.py) — rolling train/test + DSR gate
- [`src/backtesting/ny_orb_replay_adapter.py`](src/backtesting/ny_orb_replay_adapter.py), [`pullback_window_replay_adapter.py`](src/backtesting/pullback_window_replay_adapter.py) — tick-replay strategy adapters
- [`scripts/backtest_evidence_gated.py`](scripts/backtest_evidence_gated.py) — CLI walker
- [`scripts/download_dukascopy_xauusd_ticks.py`](scripts/download_dukascopy_xauusd_ticks.py) — bi5 fetcher

### Docs
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — strategy bot ops (MT5 password discipline, EBS recovery, news discipline, tick-engine promotion)
- [`docs/COPY_TRADER_RUNBOOK.md`](docs/COPY_TRADER_RUNBOOK.md) — copy-trader ops
- [`docs/AWS_DEPLOYMENT.md`](docs/AWS_DEPLOYMENT.md) — AWS provisioning + migration to Hetzner
- [`docs/post-mortem-5k.md`](docs/post-mortem-5k.md) — $5k bust analysis + concentration risk addendum
- [`docs/superpowers/specs/`](docs/superpowers/specs/) — design specs

---

## FundingPips Rules (for strategy bot)

| Rule | Value |
|---|---|
| Daily Loss Limit | 5% |
| Overall Drawdown | 10% |
| Max Risk/Trade ($5k-$10k) | 3% (we cap at 2%) |
| Max Risk/Trade ($50k+) | 2% |
| Min Trading Days | 3 |
| HFT, tick scalping, sub-2min holds | **PROHIBITED** — toxic-flow flag triggers retroactive ban |
| Profit Split (Funded) | 80/20 |

Per `docs/post-mortem-5k.md` — strategy bot's tick engine modify rate-limit is 8s/ticket (was 2s) to stay below FTMO's 2,000 server-request/day cap (FundingPips backend likely similar).

---

## Tests

```bash
pytest tests/unit/                  # 148 tests, ~3s
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    python3 -m pytest -p pytest_asyncio.plugin tests/unit/   # if global pytest plugins are broken
```

Modules:
- `test_news_filter.py`, `test_news_filter_enabled_flag.py` — central news gate
- `test_partial_profit_on_tick.py`, `test_tick_position_manager.py` — tick-driven exits
- `test_prop_firm_guard.py` — FundingPips safety
- `test_risk_manager_state_persistence.py` — daily-loss baseline survives restart
- `test_dukascopy_downloader.py` — tick CSV fetcher
- `test_strategy_health_monitor.py` — 8 early-warning signals
- `test_xauusd_ny_orb_tick_breakout.py`, `test_xauusd_pullback_window_state_machine.py` — archived strategies
- `test_walk_forward_validator.py` — DSR + rolling windows
- `test_copy_trader.py`, `test_mirror_journal.py` — copy-trader system

---

## Hard rules carried across the project

1. **Live trading bot NEVER on AWS spot / preemptible** — interruption = loss
2. **Master MT5 password lives only in SSM/SecureString** — humans use the investor (read-only) password for any VNC inspection
3. **EBS DeleteOnTermination=false** on data volume; nightly S3 sync; per `docs/RUNBOOK.md` §2-§3
4. **Tick-engine promotion gated** by RUNBOOK §5 checklist (latency baseline + 14d demo)
5. **Strategies marked DO_NOT_DEPLOY stay off** until walk-forward + DSR > 0.5 gate clears
6. **News window**: bot pre-flats positions ≤5 min before high-impact events; copy-trader does NOT (mirror copies through, accepted risk)
7. **Concentration**: XAUUSD-only is documented vulnerability per April-May 2026 ATH break; XAGUSD/US30 hedge candidates for Phase 2

---

## Status (May 2026)

| System | Live? | Last result |
|---|---|---|
| Strategy bot (existing 6 strategies) | Not currently deployed | $5k Step 1 PASSED prior; $30→$376 prior demo win |
| Copy-trader bot | Code complete, 19/19 tests pass | Awaiting Vantage A + B credentials + first demo run |
| NY ORB tick strategy | Archived | 8yr OOS backtest: -$45,250 / 36.5% WR. DO_NOT_DEPLOY. |
| Pullback window strategy | Archived | 1 trade in 8yr — phase machine too tight. DO_NOT_DEPLOY. |
| Tick infra (TickStream + TickPositionManager) | Code complete (PR #4 merged) | Off-by-default; Phase 1 ops (latency baseline, demo run) on user |
