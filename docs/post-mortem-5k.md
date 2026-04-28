# Post-mortem: $5k FundingPips bust

**Date:** 2026-04-24 (interview-based reconstruction; trade-level data lost)
**Account:** fp-5k-v1 (terminated, EBS not preserved)
**Phase at bust:** Step 2 (Step 1 had passed Day 1 with +10%)
**Breach type:** Daily DD / Overall DD (precise category unrecoverable)
**Reconstruction confidence:** medium — based on user recall, not query data

## Timeline (reconstructed)

| When | What |
|---|---|
| early March 2026 | $5k Step 1 purchased and bot deployed on EC2 |
| Day 1 of Step 1 | Bot hit 10% target. Step 1 PASSED |
| Step 1 → 2 cooldown | 3 trading-day minimum window |
| Step 2 day N | High-impact US news event (NFP / FOMC / CPI) was scheduled |
| During news window | **User opened a manual trade alongside the running bot** |
| Same window | Manual trade(s) + live volatility blew through DD limit |
| Bust | FundingPips marked the account failed |
| Post-bust | EC2 instance terminated to stop hosting cost |
| Post-bust | EBS volume was destroyed with the instance — data lost |

## Root cause

**Human discipline failure during a news event.** Not a bot bug. The bot was tagging every position with `magic=200000`; the manual trade had a different magic and was completely invisible to:

- `RiskManager.PropFirmGuard` — only tracked equity and bot-placed trade count
- `PositionMonitor` — `_is_bot_position` detected bot trades for management but did nothing about non-bot trades
- News filter — only ran in `_scan_scalping`, so it gated 1 of 4 scan loops; the manual trade trivially bypassed it

Drawdown attribution couldn't be split bot-vs-human because the journal didn't tag positions with their source. We can't tell from the data we *don't* have whether the bot was contributing to the bleed or holding flat.

## Classification

- [x] **Operator (manual trading) during news event** — primary cause
- [ ] Strategy drawdown + variance — no evidence (data lost)
- [ ] Risk-manager bug — partial: filter missed non-bot trades and bypassed Telegram/M15 paths
- [ ] Execution fault — no evidence
- [ ] Bad signal through gate — no evidence
- [ ] Config mismatch — no evidence

The "Risk-manager bug" partial classification reflects the **system gap**, not the proximate cause: the bot's defenses didn't help in a scenario it could have helped in.

## Why we can't run a proper SQL post-mortem

The local DB at `data/trading_bot_v2.db` is empty (March 16 schema-init only). The EC2 was terminated with `DeleteOnTermination=true`. No S3 backup existed. The only data sources are user memory and the README/repo state at the time.

This is itself a P1 blocker — the data-durability hardening in `bootstrap-ec2.sh` and the Turso warm tier in `orchestration-plan-v2.md` exist specifically so this never happens again.

## Fix landed (PR #1, branch `feat/p1-news-foreign-position`)

The system can't enforce discipline by force, but it can:

1. **See foreign positions in real time.** `src/monitoring/position_monitor.py::_check_foreign_positions` polls every cycle, alerts Slack + Telegram on any position with `magic != 200000`. Bot does NOT auto-close (race-risk against the human).
2. **Block bot trades during news windows.** `src/risk/manager.py::_validate_risk_limits` Check #0 — every signal source converges here. Closes the M5-only filter coverage gap (was ~25%, now 100% of bot-originated paths).
3. **Pre-news FLAT.** `src/monitoring/position_monitor.py::_check_pre_news_flat` closes BOT positions ≤5 min before high-impact events. Skips foreign positions deliberately.
4. **Calendar expansion.** `config/news_calendar.csv` grew 63 → 143 events: ECB, BoE, US PPI, US Retail Sales added on top of NFP/CPI/FOMC.

## What discipline still has to do

The system can alert. The system **cannot** prevent a human from logging into MT5 with the master password and placing a trade. The strongest mitigation is operational, not code:

- **Use the MT5 investor password for any human session.** Master password lives only in SSM and only the bot uses it. Investor password is read-only — clicking "Buy" on the investor terminal returns `Invalid account`. This single change would have prevented the $5k bust.
- Deployment Agent (when paperclip lands) sends a Telegram pre-news alert ~30 min before high-impact events: "Bot flatting at 14:00 UTC. Do not open manual trades."

Both are documented in `docs/NEXT_CHALLENGE_SAFETY_GATE.md`.

## Decisions

- [x] Root cause identified
- [x] Code-side fix designed and merged (PR #1)
- [x] Operational mitigation defined (investor password)
- [x] Data-durability hardening planned (S3 sync, EBS DeleteOnTermination=false, Turso warm tier)
- [ ] **Do NOT buy the next FundingPips challenge until:** PR #1 merged, safety-gate runbook executed end-to-end on a demo account, MT5 investor password verified to reject manual trades

## Lessons codified into the system

| Lesson | Where it lives now |
|---|---|
| Bot must see human trades in real time | Foreign-position monitor (Diff 3) |
| News filter must cover all signal paths | Central RiskManager gate (Diff 1) |
| Don't lose data when EC2 dies | EBS DeleteOnTermination=false + nightly S3 sync + Turso warm tier (orchestration-plan-v2.md) |
| Discipline can fail; design for failure | MT5 investor password pattern |
| Every trade must trace to a source | Multi-tenant schema: `trades.account_id`, `trades.signal_source`, `trades.system_id`, `trades.magic` |
| Decision audit trail per signal | `signal_executions` table — captures rejected_news, rejected_risk, executed for every signal per account |

This document is itself a lesson — recovered information about the bust now lives in the repo where the next maintainer (or next AI agent) can read it.
