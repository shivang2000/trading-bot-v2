# Trading Bot V2 Improvement Plan

**Created:** 2026-03-30
**Status:** DRAFT - Awaiting user confirmation
**Scope:** 10 improvement items across ~20 files
**Estimated complexity:** HIGH (safety-critical financial system)

---

## RALPLAN-DR Structured Deliberation

### Principles (5)

1. **Capital preservation above all.** The bot manages real money in a prop firm context where a 5% daily loss or 10% overall drawdown means account termination. Every change must be evaluated against "can this lose the account?"
2. **Fail-safe defaults.** When uncertain, the system should refuse to trade rather than trade recklessly. Silent failures must become loud failures.
3. **State must survive restarts.** A Docker restart or EC2 reboot must not cause the bot to forget its peak equity, daily losses, or open positions. In-memory-only state is a liability.
4. **Test before deploy.** Zero test coverage on a live trading system is an existential risk. Critical paths (risk checks, position sizing, prop firm guard) need automated verification before any code change ships.
5. **Minimal viable change.** Prefer targeted fixes over architecture rewrites. The bot is live and generating signals; stability of what works matters more than elegance.

### Decision Drivers (top 3)

1. **Account survival risk** -- Issues 4, 5, 6, 7, 8 can each independently cause the prop firm account to breach limits and be terminated. These are the highest-value fixes.
2. **Confidence to ship changes** -- With zero tests (issue 1), every fix to the risk layer is a gamble. A minimal test harness for the risk module de-risks all subsequent work.
3. **Operational reliability** -- The bot runs 24/5 on EC2 via Docker. State persistence bugs (peak equity, daily count) compound over restarts and can silently degrade risk enforcement.

### Viable Options

#### Option A: "Safety First" (Recommended)

Fix all P0 capital-at-risk bugs first, add test coverage for the risk layer, then move to reliability and quality improvements.

- **Pros:** Directly addresses the highest financial risk. Each P0 fix is small and isolated. Test harness enables safe iteration on subsequent fixes.
- **Cons:** Delays feature work (strategy selection, walk-forward optimization). Less exciting than new capabilities.

#### Option B: "Test Infrastructure First"

Build comprehensive test suite before touching any production code, then fix bugs with test coverage already in place.

- **Pros:** Safest approach to code changes. Catches regressions. Establishes good engineering practice.
- **Cons:** Slow to deliver value. The P0 bugs are actively risking the account while tests are being written. Over-engineering for a single-developer project.

#### Option C: "Features + Fixes in Parallel"

Split work into two tracks: one person fixes bugs, another builds strategy selection and walk-forward optimization.

- **Pros:** Fastest to deliver both safety and features.
- **Cons:** Single-developer project; parallelism is fictional. Risk of merge conflicts in shared modules (signal_generator, risk manager). Feature work on an untested codebase is brittle.

**Invalidation rationale for Option C:** The project context (solo developer, $50 starting capital, prop firm evaluation) means there is no team to parallelize. Option B is valid but the delay is dangerous given active P0 bugs.

---

## ADR: Improvement Track Decision

- **Decision:** Option A -- Safety First
- **Drivers:** Account survival risk, confidence to ship, operational reliability
- **Alternatives considered:** Test-first (Option B), parallel tracks (Option C)
- **Why chosen:** P0 bugs are actively threatening account survival. Targeted risk-layer tests (not full coverage) give enough confidence to ship safely without the delay of Option B.
- **Consequences:** Feature work (strategy selection, walk-forward) deferred to P2/P3. Acceptable because the bot is already live with 2 working strategies.
- **Follow-ups:** After P0/P1 complete, re-evaluate whether to invest in broader test coverage or move to feature work.

---

## Prioritized Improvement Plan

### P0: CRITICAL -- Capital at Risk

#### P0-1: Add periodic equity monitoring to PropFirmGuard

**What:** PropFirmGuard currently only checks equity when a new signal arrives (`can_trade()` in `_on_signal`). If the account is losing money on open positions and no new signals come in, the bot will not detect that it has breached the daily loss limit or DD floor until the next signal -- which could be hours later.

**Why:** A rapid gold move (e.g., $30 in 5 minutes) with an open position could breach the 5% daily loss limit silently. By the time the next signal triggers a check, the account may already be terminated by FundingPips.

**Files:**
- `src/monitoring/position_monitor.py` -- Add PropFirmGuard equity check inside `_check_positions()` (runs every 30s)
- `src/risk/prop_firm_guard.py` -- No changes needed, `can_trade()` already works
- `src/main.py` -- Pass PropFirmGuard instance to PositionMonitor constructor

**Acceptance criteria:**
- PositionMonitor checks `prop_firm_guard.can_trade(equity, now)` every poll cycle
- When daily loss or DD floor is breached, all open positions are closed immediately via EMERGENCY close orders
- Slack + Telegram notification sent on breach detection
- Breach detection works even when no new signals are being generated

**Effort:** S (small -- ~30 lines of new code, pattern already exists in PositionMonitor's emergency stop logic at line 154)

---

#### P0-2: Persist peak equity across restarts

**What:** `RiskManager._peak_equity` is initialized from `config.account.initial_balance` (line 89) and updated from MT5 on `initialize()` (line 100). But if the bot restarts after the account has grown (e.g., equity went from $5000 to $5400 and then dropped to $5200), the new `_peak_equity` will be set to $5200, not $5400. The drawdown calculation will undercount the actual drawdown.

**Why:** Prop firm DD is calculated from the highest equity point ever reached. If peak equity is lost on restart, the bot thinks drawdown is smaller than it actually is and may allow trades that push past the real DD floor.

**Files:**
- `src/tracking/database.py` -- Add `save_peak_equity(value)` and `get_peak_equity()` methods
- `src/risk/manager.py` -- On init, load persisted peak equity; on each update, persist if new peak
- `src/main.py` -- Wire the persistence in the startup sequence (after DB connect, before risk manager init)

**Acceptance criteria:**
- Peak equity survives bot restarts and Docker container recreation
- On startup, peak equity is set to `max(persisted_peak, current_mt5_equity)`
- Peak equity is written to DB whenever a new peak is reached (inside `_validate_risk_limits`)

**Effort:** S

---

#### P0-3: Friday auto-close must actively close open positions

**What:** `PropFirmGuard.should_friday_close()` returns `True` on Friday after 21:00 UTC, but this only blocks NEW trades (via `can_trade()` returning False). No code in the live bot actually closes existing open positions on Friday evening. The scalping backtester does this correctly (line 202 of `scalping_engine.py`), but the live bot does not.

**Why:** Holding positions over the weekend on a prop firm account is extremely risky -- gaps on Sunday open can blow through stop losses and breach DD limits. FundingPips may also have rules requiring flat books over weekends.

**Files:**
- `src/monitoring/position_monitor.py` -- In `_check_positions()`, check `PropFirmGuard.should_friday_close(now)`. If true and positions are open, generate close orders for all positions.
- `src/main.py` -- Pass PropFirmGuard to PositionMonitor (same wiring as P0-1)

**Acceptance criteria:**
- All open positions are closed via market orders when Friday 21:00 UTC is reached
- Close orders use "FRIDAY_CLOSE" comment for audit trail
- Slack + Telegram notification sent: "Friday auto-close: closed N positions"
- No new trades are opened after close (already handled by `can_trade()`)

**Effort:** S

---

#### P0-4: Reset daily trade count at midnight

**What:** `RiskManager._daily_trade_count` is initialized to 0 at startup (line 87) and restored from DB (line 344-345 of `main.py`). The DB query `get_daily_trade_count()` correctly filters by today's date. However, the in-memory `_daily_trade_count` in RiskManager is never reset at midnight -- it just accumulates. If the bot runs across midnight, yesterday's trades count toward today's limit.

**Why:** With `max_daily_trades: 10`, if the bot placed 8 trades yesterday and runs through midnight without restart, it will think it has already placed 8 trades today and only allow 2 more.

**Files:**
- `src/risk/manager.py` -- Add a `_current_date` tracker. In `_validate_risk_limits()`, check if the date has changed and reset `_daily_trade_count` to 0 (or re-read from DB for today).

**Acceptance criteria:**
- `_daily_trade_count` resets to 0 when the date rolls over (UTC midnight)
- The reset is logged: "Daily trade count reset (new day: YYYY-MM-DD)"
- No restart needed for the reset to occur

**Effort:** S

---

### P1: HIGH -- Reliability & Performance

#### P1-1: Add real-time PnL tracking for open positions in PropFirmGuard

**What:** PropFirmGuard's `can_trade()` receives equity from MT5, but the guard itself has no visibility into individual position PnL. The PositionMonitor already has per-position data (current_price, open_price, profit). This data should be logged and made available for smarter risk decisions (e.g., "close the worst-performing position if approaching DD floor" rather than closing everything).

**Why:** The current approach is binary: either allow trading or block everything. With per-position PnL awareness, the guard could selectively close losing positions before hitting the hard limit, preserving winning positions.

**Files:**
- `src/risk/prop_firm_guard.py` -- Add `update_positions(positions: list[Position])` method that tracks per-position unrealized PnL
- `src/monitoring/position_monitor.py` -- Call `prop_firm_guard.update_positions()` in `_check_positions()`
- `src/monitoring/slack.py` -- Add periodic PnL summary (e.g., every 5 minutes when positions are open)

**Acceptance criteria:**
- PropFirmGuard tracks unrealized PnL per open position
- When approaching DD floor (within 2%), the guard can identify and close the worst-performing position
- Slack receives periodic PnL updates when positions are open
- Metric logged: "Open PnL: $X.XX across N positions (worst: ticket #Y at -$Z.ZZ)"

**Effort:** M

---

#### P1-2: Add core test suite for risk layer

**What:** The `tests/` directory contains only empty `__init__.py` stubs. There are zero actual test implementations. The risk layer (PropFirmGuard, RiskManager, PositionSizer, TrailingStopManager) is the most critical code in the system and must have tests.

**Why:** Every P0 fix above modifies the risk layer. Without tests, there is no way to verify that fixes work correctly or that they don't break existing behavior. The prop firm guard math (buffer calculations, tier multipliers, directional exposure) is exactly the kind of logic that benefits most from unit tests.

**Files:**
- `tests/unit/test_prop_firm_guard.py` -- Test all PropFirmGuard methods: `can_trade()` edge cases (at buffer boundary, at exact limit), `get_risk_multiplier()` tier transitions, `should_friday_close()`, `check_directional_exposure()`, `check_trade_idea()` window logic
- `tests/unit/test_risk_manager.py` -- Test signal validation: max positions, daily trade count, daily loss %, drawdown %, spread check, free margin check
- `tests/unit/test_trailing_stop.py` -- Test ratchet behavior (SL only moves favorably), activation threshold, profit trail with giveback
- `tests/unit/test_position_sizer.py` -- Test lot calculation, lot capping, minimum lot enforcement

**Acceptance criteria:**
- `pytest tests/unit/` passes with 0 failures
- PropFirmGuard has tests for: daily loss limit hit, DD floor hit, profit target reached, Friday close, buffer calculations, risk multiplier tiers
- TrailingStopManager has tests for: ratchet-only movement, activation threshold, profit trail giveback cap
- Tests use mocks (London School TDD per project convention) -- no MT5 connection needed

**Effort:** M

---

### P2: MEDIUM -- Quality & Maintainability

#### P2-1: Fix EmergencyStop inconsistency with PropFirmGuard

**What:** There are two independent safety systems with different thresholds: `EmergencyStop` in `position_monitor.py` (line 54-57: 8% daily loss, 20% drawdown of initial_balance) and `PropFirmGuard` (5% daily loss, 10% DD). The EmergencyStop thresholds are looser than PropFirmGuard, meaning the account would be breached by FundingPips before EmergencyStop ever triggers. Additionally, EmergencyStop uses `_initial_equity` (set on first check) while PropFirmGuard uses `account_size` from config -- these can diverge.

**Why:** Redundant safety systems with inconsistent thresholds create confusion and a false sense of security. The EmergencyStop should either be removed (if PropFirmGuard handles everything via P0-1) or aligned with PropFirmGuard's limits.

**Files:**
- `src/monitoring/position_monitor.py` -- Remove or reconfigure EmergencyStop to use PropFirmGuard limits
- `src/safety/emergency.py` -- Consider deprecating or making it a fallback-only layer

**Acceptance criteria:**
- Single source of truth for loss limits (PropFirmGuard)
- EmergencyStop either removed or configured as a last-resort fallback with limits tighter than PropFirmGuard
- No scenario where FundingPips breaches the account before the bot's safety system reacts

**Effort:** S

---

#### P2-2: Add structured error handling and alerting for silent failures

**What:** Multiple critical code paths swallow exceptions with bare `except Exception: pass` or `except Exception: logger.debug(...)`. Examples:
- `main.py` line 215: `_refresh_account_cache` -- silently ignores MT5 failures
- `position_monitor.py` line 170-171: emergency check errors suppressed with `logger.debug`
- `position_monitor.py` line 297-300: trailing stop failures logged as warnings but never escalated

**Why:** In a live trading system, silent failures can cascade. If account state refresh fails repeatedly, the cached state becomes stale, and risk decisions are made on outdated equity values. The operator (you) gets no alert that this is happening.

**Files:**
- `src/main.py` -- Add consecutive failure counting to `_refresh_account_cache`. Alert on Slack after 3 consecutive failures.
- `src/monitoring/position_monitor.py` -- Escalate emergency check errors from debug to warning. Add failure counter for trailing stop updates.
- Add a health check endpoint or periodic health log line: "Health: MT5=OK, account_cache_age=15s, trailing_stops=OK"

**Acceptance criteria:**
- No bare `except Exception: pass` in critical paths (risk, execution, position monitoring)
- Consecutive MT5 failures trigger Slack alert after 3 failures
- Periodic health status logged every 5 minutes with component states
- Stale account cache (>2 minutes old) triggers a warning

**Effort:** M

---

### P3: LOW -- Nice-to-Have / Future

#### P3-1: Automated strategy selection based on market regime

**What:** 15+ strategies exist in the codebase but only 2 are enabled (`m5_keltner_squeeze`, `m5_mtf_momentum`). The regime detector already classifies markets as TRENDING_UP/DOWN, RANGING, CHOPPY, VOLATILE_TREND. Strategy selection is currently manual via YAML config.

**Why:** Different strategies perform better in different regimes. Automating selection could improve overall performance. However, this requires validated backtest results per strategy per regime, which don't exist yet.

**Files:**
- `src/analysis/signal_generator.py` -- Add regime-to-strategy mapping
- `config/strategies.yaml` -- Add per-strategy regime preferences
- `src/backtesting/walk_forward.py` -- Hook into strategy parameter optimization

**Acceptance criteria:**
- Each strategy has a defined set of favorable regimes
- SignalGenerator only runs strategies whose favorable regime matches the current detected regime
- Strategy-regime mapping is configurable (not hardcoded)
- Backtest evidence exists for each enabled strategy-regime pair

**Effort:** L

---

#### P3-2: Hook walk-forward backtester into strategy parameter optimization

**What:** `src/backtesting/walk_forward.py` exists but is not connected to live strategy configuration. Walk-forward optimization could periodically re-optimize strategy parameters (e.g., EMA periods, ATR multipliers) on recent data and update the live config.

**Why:** Markets evolve. Parameters optimized on 2025 data may underperform in 2026. Walk-forward ensures the bot adapts.

**Files:**
- `src/backtesting/walk_forward.py` -- Extend to output optimized parameters
- `scripts/` -- Add a weekly optimization script that runs walk-forward and updates config
- `config/` -- Add a mechanism for parameter hot-reload or scheduled config updates

**Acceptance criteria:**
- Walk-forward optimization can be run as a scheduled script (weekly)
- Output is a set of optimized parameters per strategy
- Parameters are written to a staging config file for human review before deployment
- No automatic deployment of optimized parameters without human approval

**Effort:** L

---

## Task Flow (Execution Order)

```
P0-4 (daily count reset) ──┐
P0-2 (persist peak equity) ─┤── All P0s can be done in parallel
P0-3 (Friday auto-close)  ──┤   (they touch different code paths)
P0-1 (periodic equity mon) ─┘
         │
         v
    P1-2 (test suite) ───── Write tests that cover ALL P0 fixes
         │                   to lock in correct behavior
         v
    P1-1 (real-time PnL) ── Builds on P0-1 wiring
         │
         v
    P2-1 (EmergencyStop)  ── Can be done after P0-1 proves
    P2-2 (error handling)     PropFirmGuard is sufficient
         │
         v
    P3-1 (strategy select) ── Requires backtest evidence
    P3-2 (walk-forward)       Long-term improvements
```

---

## Success Criteria

1. All P0 fixes deployed: periodic equity monitoring, persisted peak equity, Friday auto-close, daily count reset
2. Test suite covers all PropFirmGuard boundary conditions and passes in CI
3. No scenario exists where FundingPips can breach the account before the bot's safety system reacts
4. Bot can run for 7+ days without restart and maintain correct risk state
5. Slack alerts fire within 30 seconds of any safety-relevant event

---

## Guardrails

### Must Have
- All P0 changes must be tested (at minimum manually with a demo account) before deploying to the live prop firm account
- PropFirmGuard buffer calculations must remain conservative (buffer = extra safety margin)
- All position close operations must use market orders (not limit) for guaranteed execution
- State persistence must use the existing SQLite DB (no new infrastructure)

### Must NOT Have
- No changes to the Telegram listener or parser (working correctly, out of scope)
- No changes to the Claude AI filter (optional component, not in critical path)
- No new dependencies unless strictly necessary
- No modification of the event bus architecture (EventBus pattern is working)
- No removal of strategies -- only enabling/disabling via config
