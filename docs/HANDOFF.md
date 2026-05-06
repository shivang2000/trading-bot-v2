# Handoff — 2026-05-07

Cross-machine pickup notes. Read this first on the other laptop after `git pull`.

---

## Status snapshot

- **PR #4** (merged): `feat(monitoring): tick-driven exit engine + ops hardening`
  https://github.com/shivang2000/trading-bot-v2/pull/4
- **Plan document**: `docs/plans/2026-05-07-hft-tick-engine.md`
  (also at `~/.claude/plans/frolicking-beaming-bird.md` on the originating laptop)
- **Runbook**: `docs/RUNBOOK.md`
- **Active branch**: `main` (after merge); feature branch `feat/tick-engine` can be deleted

---

## What got built (Phase 1 of plan, all in PR #4)

| Track | What | Files |
|---|---|---|
| **D** | MT5 client extensions: `copy_ticks_from`, `copy_ticks_range`, named `position_modify` (sync + async wrappers) | `src/mt5/client.py` |
| **A** | `TickEngineConfig` pydantic block + base.yaml defaults (off-by-default) | `src/config/schema.py`, `config/base.yaml` |
| **A** | `PartialProfitManager.evaluate_on_tick()` (alias of `check`, semantic name; shares `_tracked` state) | `src/monitoring/partial_profit_manager.py` |
| **A** | `TickPositionManager` — TickStream subscriber, ATR cache, modify-rate-limit (default 2s/ticket), drop-unchanged guard, foreign-position skip, POSITION_CLOSED cleanup hook | `src/monitoring/tick_position_manager.py` (NEW, ~310 lines) |
| **A** | Wiring into `main.py`: instantiates TickStream + TickPositionManager when enabled, registers cleanup, lifecycle | `src/main.py` |
| **A** | `PositionMonitor.suppress_position_management` flag — skips poll-driven trailing/partial when tick engine owns them; health work unchanged | `src/monitoring/position_monitor.py` |
| **A** | 13 unit tests (rate-limit, dedupe, ratchet, partial-bypass, ATR cache, foreign skip, cleanup, evaluate_on_tick parity) | `tests/unit/test_tick_position_manager.py`, `test_partial_profit_on_tick.py` |
| **C** | Latency baseline script (Phase 0 SLO check, fails if p95 ≥ 350ms) | `scripts/measure_tick_latency.py` |
| **C** | Nightly SQLite → S3 backup with retention prune (cron-ready) | `scripts/nightly-backup.sh` |
| **C** | Runtime health gate (exits CRITICAL after 5min grace so Docker restarts) | `src/safety/health_gate.py` |
| **C** | Operational runbook: investor/master password pattern, EBS recovery, news discipline, tick-engine promotion checklist | `docs/RUNBOOK.md` |
| **B** | Tick-velocity breakout strategy (rolling 30s velocity z-score, off-by-default) | `src/analysis/strategies/tick_velocity_breakout.py` |
| **B** | CSV tick replay engine for tick-strategy backtesting | `src/backtesting/tick_engine.py` |

**One new strategy this session**: `tick_velocity_breakout` (scaffold, disabled). Existing 15 bar-based scalpers untouched.

---

## What's NOT done (operational, not code)

These cannot run from a Claude Code sandbox. You run them on EC2 / venv.

1. **Rotate Vantage MT5 master password** — was leaked in chat during planning. Account is empty so no funds at risk, but rotate as hygiene before any deploy.
2. `pytest tests/unit/ -k "tick or partial"` — confirm 13 new tests pass on a venv with project deps (Python 3.12, `pytest`, `pytest-asyncio`, `pandas`, `pandas-ta`, `pydantic`, `aiosqlite`, `rpyc`).
3. **Set `BACKUP_S3_BUCKET` env on EC2** + cron `scripts/nightly-backup.sh` (1x daily). Verify with one manual run + S3 listing.
4. **Update `scripts/bootstrap-ec2.sh`** to provision EBS with `DeleteOnTermination=false` (per RUNBOOK §2). Not done yet — the bootstrap script edit is part of Track C plan but couldn't be made without seeing the current script content for the existing infra.
5. **Latency baseline**: `python3 scripts/measure_tick_latency.py --symbol XAUUSD --duration 600` from EC2 during London. Must hit p95 < 350ms before flipping `tick_engine.enabled: true` anywhere.
6. **Demo run**: enable in a Vantage demo overlay (`tick_engine.enabled: true`, `tick_engine.symbols: [XAUUSD]`, demo-only YAML), run 14 days, compare P&L vs parallel bar engine demo. Promotion checklist: RUNBOOK §5.
7. **Tick history dataset for Track B validation**: download Dukascopy XAUUSD ticks for ≥3 months OR record live demo for 2 weeks. Without this, `tick_velocity_breakout` cannot be validated and stays off.
8. **Live promote**: $30-100 Vantage live → fresh FundingPips $5k once demo passes. Investor password pattern mandatory (RUNBOOK §1).

---

## Constraints captured this session (do not re-litigate)

- **Architecture A chosen** (Python-only, 200-500ms decision-to-fill). Rejected MQL5 EA path (sub-50ms but new language) and direct broker FIX (broker rewrite). Revisit only if Phase 2 demo proves Python latency insufficient.
- **MT5 RPyC roundtrip**: ~150-250ms floor. Broker leg adds 50-200ms (Sydney). Sub-100ms HFT not possible in this stack.
- **Broker rejects rapid `position_modify`**. 2-second per-ticket rate limit is mandatory; configurable via `tick_engine.modify_rate_limit_seconds`.
- **Per-trade risk cap on FundingPips**: $100 on $5k. 0.20 lot on XAUUSD with 20-pip SL exceeds the cap → PositionSizer reduces lot. Mitigation: 10-pip SL on tick scalp OR `risk_pct_override: 2.5%` in the tick strategy config.
- **Existing FundingPips $5k Step 2 account stays on bar engine**. Do NOT enable `tick_engine` there before completing the demo-validation checklist (RUNBOOK §5). Bust mode last time was $5k → 0 (post-mortem-5k.md).
- **AI-agents project explicitly deferred** until trading-bot-v2 generates funding profits.
- **Tick streaming, NOT trailing**, is the actual frontier. README/strategy-guide claim that "trailing stop is the next frontier" is **outdated** — trailing-stop manager is fully active and persisted to SQLite.

---

## Auth gotcha (will likely hit again)

- This laptop has `gh` CLI authenticated as `shivang-trestle` (Trestle work account)
- Repo is `shivang2000/trading-bot-v2` (personal account)
- First push attempt failed 403 until `shivang-trestle` was added as collaborator
- On the other laptop: if pushing fails 403, either auth as `shivang2000` or add the other laptop's gh user as collaborator

---

## Pickup checklist (other laptop)

```bash
cd <wherever>/trading-bot-v2
git fetch origin
git checkout main
git pull origin main      # PR #4 should be merged

# Sanity: confirm tick engine is OFF in the live config
grep -A1 "^tick_engine:" config/base.yaml
# enabled should still read: false

# Read the runbook before any deploy
less docs/RUNBOOK.md
```

For the other laptop to resume the live Claude Code conversation thread (cross-machine), you need claude.ai/code (web) — local CLI conversations live in `~/.claude/projects/<dir>/conversations/` and don't sync between machines automatically. Plan + handoff in this repo + commits on PR #4 capture the substantive context regardless.

---

## Session metadata

- Date: 2026-05-07
- Plan source: `~/.claude/plans/frolicking-beaming-bird.md` → copied to `docs/plans/2026-05-07-hft-tick-engine.md`
- Originating laptop: `darwin`, working dir `/Users/shivang/dev/GitHub2/advanced-trading-bot/trading-bot-v2`
- Auto-memory: `~/.claude/projects/-Users-shivang-dev-GitHub2-advanced-trading-bot/memory/` (machine-local; key facts duplicated above)

---

## When you pick this back up, the immediate next move is:

1. Read `docs/RUNBOOK.md` §5 (tick-engine promotion checklist)
2. Run `pytest tests/unit/ -k "tick or partial"` to verify the 13 tests pass in your venv
3. Pick a Vantage **demo** account, create `config/vantage-tickdemo.yaml` overlay with `tick_engine.enabled: true`, deploy, run latency baseline, run for 14 days
4. After demo passes RUNBOOK §5 checklist → live $30 → fresh FundingPips $5k
