# Copy-Trader Bot — Design Spec

Date: 2026-05-08

Mirrors trades from Vantage Account A (operated by a human partner) to
Vantage Account B (your other account on the same broker), so the
profitable edge of the source trader is captured on both accounts.

This spec is the formalised version of the brainstorming session and
matches `~/.claude/plans/working-directory-is-advanced-trading-bo-humming-clover.md`. The runbook for operating the deployed system is
`docs/COPY_TRADER_RUNBOOK.md`.

---

## Problem

A human partner operates Account A (a Vantage MT5 account, $200 balance)
and reliably extracted ~$200-300 profit over 2 days trading 0.10-0.30
lots on XAUUSD with mixed hold times (seconds to hours). Profit is split
50/50 with the partner. The user owns a second Vantage account
(Account B, also $200) and wants to mirror Account A's trades onto it
so the same edge generates double profit (and double risk).

Account B has historically been unused. Both accounts are user-owned;
the partner does not have access to B.

---

## Constraints

| Constraint | Why |
|---|---|
| Pure mimicry — no strategy code on A or B | User explicit constraint. A and B are exclusively human-driven (A) and mirror-only (B). The existing trading-bot-v2 strategy stack continues to run on a separate account, untouched. |
| Same Vantage broker for A and B | User confirmed. Eliminates cross-feed slippage. |
| 1:1 lot scaling | Both accounts same size ($200). User explicit decision. |
| Sub-100ms poll, sub-300ms total mirror end-to-end | Trader's hold times include scalp (sub-30s). Slow mirror loses edge. |
| Copy through during news | Trader profitable through news; user accepts news-window risk on B. |
| SL/TP modifications mirrored | If trader trails on A, B must trail too — otherwise B holds losers A already exited. |
| Skip A's positions already open at boot | Avoid mid-trade entry on B at unfavourable prices. |
| Master passwords for A and B; no investor-pwd from third party | User owns both accounts. |

---

## Architecture

```
                Process: copy-trader-bot      (NEW container)
                ├── AsyncMT5Client (source) ──► mt5-source container :8001 → Vantage A
                ├── AsyncMT5Client (dest)   ──► mt5-dest   container :8001 → Vantage B
                ├── CopyTrader.poll_loop()
                │     ├── on_new_position(A)        → open(B)
                │     ├── on_modified_position(A)   → modify(B)
                │     └── on_closed_position(A)     → close(B)
                ├── MirrorJournal (SQLite audit + state recovery)
                └── SlackNotifier (per-event posts)

                Process: existing trading-bot-v2  (UNCHANGED)
                └── runs whatever existing strategies on its own MT5
                    container — does not interact with copy-trader process
```

The two systems are fully isolated:
- Different docker-compose stacks (`docker-compose.copytrader.yml` vs `docker-compose.ec2.yml`)
- Different MT5 containers (`mt5-source` + `mt5-dest` vs the existing `metatrader5`)
- Different SQLite DBs (`mirror_journal.db` vs `trading_bot_v2.db`)
- Different config files (`config/copy_trader.yaml` vs `config/base.yaml`)
- Different process entrypoints (`src.copy_trader.main` vs `src.main`)
- Different Docker images (`Dockerfile.copytrader` is slimmer — no
  pandas/pandas-ta/anthropic deps)

---

## Components

### `src/copy_trader/copy_trader.py` — `CopyTrader`

Hot path. Polls source positions every 100ms, diffs, fires open/modify/close
on dest. Persists every event to journal. Emits Slack alert per event.

Key methods:
- `boot()` — pre-load journal state, snapshot source positions, mark
  pre-existing as ignored
- `run_forever()` — main async loop with `poll_interval_ms` cycle
- `_handle_open`, `_handle_close`, `_handle_modify_if_changed` — per-event
  branches; each writes journal row + posts Slack
- `_compute_dest_volume` — currently `exact` only; structure exposes
  `multiplier` and `proportional` for future use
- `_build_market_request`, `_build_close_request` — raw MT5 dict format
  matching how `src/execution/executor.py` does it

Mockable: takes any object with the `_MT5Like` Protocol (positions_get,
order_send, position_modify, symbol_info_tick).

### `src/copy_trader/mirror_journal.py` — `MirrorJournal`

SQLite, three tables:
- `mirror_events` — every open/modify/close attempt, success/failure,
  latency_ms, raw broker response (for forensics)
- `mirror_map` — current src_ticket → dest_ticket mappings (state
  persistence; restored on restart)
- `ignored_source_tickets` — source positions that exist but should
  never be mirrored (boot snapshot, below-lot-min skips)

### `src/copy_trader/notifier.py` — `SlackNotifier`

Async, non-blocking Slack webhook. Errors logged but never propagated.
Has `open()`, `close()`, `modify()`, `error()` formatters.

### `src/copy_trader/main.py`

Entrypoint. Loads config, connects two AsyncMT5Clients, starts CopyTrader,
handles SIGTERM/SIGINT graceful shutdown.

### `src/config/copy_trader_schema.py`

Pydantic config model. Deliberately not extending the existing
`AppConfig` — clean separation.

---

## Data flow

```
Trader places trade on Account A
      │
      ▼
MT5 server records position (Vantage)
      │
      ▼ (next 100ms)
copy-trader polls source: positions_get()
      │
      ▼ (diff vs last snapshot)
detected: new src_ticket=12345 not in mirror_map
      │
      ▼
build_market_request(symbol, side, volume, sl, tp)
      │
      ▼
dest.order_send(request)
      │
      ▼ (fill response in 50-200ms)
update mirror_map[12345] = dest_ticket=67890
journal.log_event("OPEN", success=True, latency_ms=N)
notifier.post("MIRROR OPEN ...")
```

End-to-end latency budget:
- Trader → Vantage: instant (broker fill time)
- Vantage → source RPyC: ~50ms (Wine + RPyC roundtrip)
- Bot poll cycle wait: avg 50ms (100ms cycle / 2)
- Build request: <1ms
- Dest RPyC + Vantage fill: ~150-300ms
- **Total: 250-500ms typical, 1s worst case during news**

---

## Risks + mitigations

| Risk | Mitigation |
|---|---|
| Bot crash mid-cycle | `mirror_map` persisted to SQLite every event; `restart: unless-stopped` brings bot back in seconds; reconciliation on boot |
| Source MT5 RPyC drops | Existing `AsyncMT5Client._call_with_reconnect` exponential backoff + 30s alert |
| Dest order_send rejection (margin) | Retry once, then alert via Slack + journal entry; do NOT silently skip |
| Trader closes A before B fill arrives | Race detected next cycle; close B in next 100ms |
| Trader trades during news | Documented as accepted risk per design (copy through) |
| 50/50 P&L split bookkeeping | Out of scope for bot — tracked manually via MT5 statements |
| 2× exposure on losing day | Both accounts will lose. 1:1 scaling means symmetric. User accepted. |
| Bot accidentally placed on wrong account | Hard config check at boot: `expected_account_login` mismatch refuses to start |
| Mirror loop accidentally inherits trading-bot strategies | Different package import path; `Dockerfile.copytrader` doesn't even include `src/analysis/` — physically impossible at filesystem layer |

---

## Verification

### Unit tests (19 total, all green)

```
pytest tests/unit/test_mirror_journal.py tests/unit/test_copy_trader.py
```

Coverage:
- Boot ignores existing positions (and skip when disabled)
- New position mirrors with correct symbol/side/volume/SL/TP
- Existing position not re-mirrored on subsequent cycles
- Open failure logs error + alerts
- Volume below lot_min ignored permanently
- Source close triggers dest close
- SL change triggers dest position_modify
- Below-threshold SL noise does NOT trigger modify
- Restart with persisted mappings does not double-mirror
- Journal: events, mirror_map, ignored_set CRUD + roundtrip across reopen

### Demo end-to-end (manual, before live)

1. Start two Vantage demo accounts (free)
2. Set up `config/copy_trader.yaml` pointing at them
3. Start stack: `./scripts/start-copytrader.sh`
4. Open MT5 on demo A via VNC, open a 0.10 lot XAUUSD trade with SL/TP
5. Verify within 500ms:
   - `mirror_events` table has OPEN row with `success=1`
   - Demo B has equivalent position
   - Slack channel got `MIRROR OPEN` notification
6. Modify SL on demo A → verify demo B SL updates within 500ms
7. Close demo A → verify demo B closes within 500ms
8. Kill copy-trader-bot mid-trade → restart → verify reconciliation works

### Acceptance gates before live deploy

- [ ] 14 days demo with zero unhandled `success=0` events
- [ ] Latency p95 < 500ms over 100+ trades
- [ ] Restart-recovery test passes
- [ ] Tiny live ($30 A + $30 B) 7-day soak: P&L delta A vs B within ±$2 per trade

---

## Out of scope (deferred)

- Multi-source mirroring (multiple traders → one dest)
- Multi-dest mirroring (one source → many dest)
- Lot scaling modes other than 1:1
- News filter override per account (currently always off for mirror)
- Cross-broker mirroring
- 50/50 P&L split bookkeeping
- ChartShare visualisation, perf dashboards, web UI
- Alerts beyond Slack (Telegram skipped to avoid phone spam)

---

## Files

### New
- `src/copy_trader/__init__.py`
- `src/copy_trader/copy_trader.py`
- `src/copy_trader/mirror_journal.py`
- `src/copy_trader/notifier.py`
- `src/copy_trader/main.py`
- `src/config/copy_trader_schema.py`
- `config/copy_trader.yaml`
- `tests/unit/test_copy_trader.py`
- `tests/unit/test_mirror_journal.py`
- `Dockerfile.copytrader`
- `docker-compose.copytrader.yml`
- `scripts/start-copytrader.sh`
- `docs/COPY_TRADER_RUNBOOK.md`
- `docs/superpowers/specs/2026-05-08-copy-trader-design.md` (this doc)

### Reuse (read-only)
- `src/mt5/client.py` (AsyncMT5Client)
- `src/core/models.py` (Position, OrderSide enums)

### Untouched (existing trading-bot-v2 stack)
- Everything in `src/main.py`, `src/analysis/`, `src/risk/`, `src/monitoring/`, `src/telegram/`, `config/base.yaml`
