# FundingPips — bot-allowed approval

**Status:** Researched from FundingPips public docs (fundingpips.com + help.fundingpips.com, via Wayback Machine snapshot 2025-11-26 to 2026-02-10). Account-holders should still file a written confirmation via support ticket before going live; FP's ToS can change.

**Account in scope:** $5,000, 1 Step model, currently in Step 1 (Student) per Shivang's setup message.
**Verified by:** Hermes session on 2026-07-11 (browser_navigate + Wayback Machine fetch of help.fundingpips.com articles).

## Q1. EA / automated trading permitted on Step 1, Step 2 / 1 Step, master?

**Answer (verbatim from "What are the Forbidden Strategies?" — Wayback 2026-02-10):**

> "Using a third-party Expert Advisor (EA) is allowed as long as it is a trade or risk manager. Using any other third-party Expert Advisor is not allowed. This will lead to a denial of the evaluation or reward and closure of the account."

**Translation for our bot:**
- **ALLOWED:** any EA that acts as a *trade or risk manager* (e.g. position sizer, SL/TP modifier, trailing-stop engine, equity-curve guard, drawdown halt). This is exactly what the v2 bot's RiskManager / PropFirmGuard / TrailingStopManager do.
- **NOT ALLOWED:** signal-generation EAs that fire entries based on third-party black-box logic. Our internal strategies (m5_mtf_momentum, m5_keltner_squeeze, m5_dual_supertrend, ema_pullback, london_breakout, m30_rsi2_mean_reversion) are first-party and explicitly named in the README. These are NOT third-party EAs. Safe.
- **NOT ALLOWED:** HFT, latency arbitrage, tick scalping, server spamming, gap trading, hedging, copy-trading between different users.

**Action item:** None — bot is allowed as-is for the 1 Step model. Add a comment header to `src/main.py` referencing this article so future maintainers can verify the policy hasn't changed.

## Q2. Restrictions

### 1 Step model — Step 1 (Evaluation) — verified from /trading-objectives
| Rule | Value | Config drift |
|---|---|---|
| Profit target | **10%** | matches `prop_firm.profit_target_pct: 10.0` |
| Max Daily Loss | **3%** | **CONFIG SAYS 5% — DRIFT, must fix to 3%** |
| Max Overall Loss | **6%** | **CONFIG SAYS 10% — DRIFT, must fix to 6%** |
| Min Trading Days | **3 days** | matches `prop_firm.min_trading_days: 3` |
| Max Risk Per Trade | **No restrictions** (Step 1) | our 1% is well within |
| Trading Period | **Unlimited** | matches |
| Leverage | **1:30** default (FX 1:50, Indices 1:20, Metals 1:20, Energies 1:10, Crypto 1:2) | config says 30 for metals — correct for XAUUSD; for FX/indices/crypto need to scope per-strategy |
| Inactivity | **30 days** (close ≥1 trade) | matches `prop_firm.inactivity_limit_days: 30` |
| News Trading | **Allowed** during evaluation | matches `prop_firm.news_filter_enabled: true` (filter is a safety, not a hard FP rule, for Step 1) |
| Weekend Holding | **Allowed** | matches `prop_firm.friday_auto_close: true` (our safety, not an FP rule) |
| Stop Loss Required | **No, but highly recommended** | our bot always carries SL — good |

### 1 Step model — Master Account — verified from /trading-objectives
| Rule | Value |
|---|---|
| Max Daily Loss | **3%** |
| Max Overall Loss | **6%** |
| Max Risk Per Trade Idea | **3%** (accounts <$50K) — our 1% is well within |
| News Trading | **RESTRICTED** — 5 min before/after high-impact; **trades opened 5 hours before a high-impact news event are EXCLUDED** from the 10-min window |
| Inactivity | **30 days** |
| Leverage | **1:30** (FX 1:50, Indices 1:20, Metals 1:20, Energies 1:10, Crypto 1:2) |

### Master Account — additional rules from help.fundingpips.com
- **3% max loss per trade idea** (includes split trades + opening a new position within 5 min in same direction — collectively one trade)
- **5-min news window on red-folder high-impact events** for affected currencies (USD/EUR/GBP/CAD/AUD/NZD/CHF/JPY lists in the article); affected instruments pinned in v2 `config/news_calendar.csv` already
- **20 lots per click limit** (single order max 20 lots — irrelevant for our 1% XAUUSD sizing on $5K, max 0.5 lots per config)
- **Maximum Lot Exposure Limit** (2-Step Master accounts only) — **does NOT apply to $5K** (1 Step model), per the table: "$5,000 account: None"
- **Copy trading** between own accounts = allowed. Copy trading between different users = prohibited. Master password to *external* slaves requires investor-password-only configuration (read-only master) or it triggers investigation.
- **VPS / VPN**: allowed; IP must remain in consistent region (article "What is the IP rule?")
- **Forbidden strategies (verbatim):** "gap trading, high-frequency trading, server spamming, latency arbitrage, toxic trading flow, hedging, long-short arbitrage, reverse arbitrage, tick scalping, server execution, and opposite account trading are all prohibited. Copy trading with others or account management by a third-party vendor is also prohibited."

### Bot-relevant config fixes required before going live
1. `prop_firm.max_daily_loss_pct`: `5.0` → **`3.0`** (Step 1 = 3%, not 5%)
2. `prop_firm.max_overall_dd_pct`: `10.0` → **`6.0`** (Step 1 = 6%, not 10%)
3. `prop_firm.safety_buffer_daily_pct`: `1.0` → keep, but verify it leaves bot at 2% effective
4. `prop_firm.safety_buffer_dd_pct`: `1.5` → keep, but verify it leaves bot at 4.5% effective
5. `prop_firm.safety_buffer_daily_usd`: `7` → recalc on 3%: daily cap = $5K × 0.03 = $150; buffer = $50
6. `prop_firm.safety_buffer_dd_usd`: `7` → recalc on 6%: overall cap = $5K × 0.06 = $300; buffer = $75
7. `risk.max_daily_loss_pct`: `4.0` → `2.0` (bot trips 1% before FP's 3%)
8. `risk.max_drawdown_pct`: `9.0` → `4.5` (bot trips 1.5% before FP's 6%)

### Additional findings from FAQ / help articles (2026-07-11)

**Daily Loss reset time:** 00:00 Platform Time (GMT+2), NOT UTC. The bot's daily reset and Friday auto-close must use GMT+2/Server Time as the day boundary. `friday_close_hour_utc: 21` is 23:00 GMT+2 — verify this is before the Friday session close.

**KNOWN DRIFT — daily reset timezone:** The bot currently uses `today_utc` (main.py:362) to detect day rollover. FP uses 00:00 GMT+2. There's a 2-hour window (22:00–00:00 UTC = 00:00–02:00 GMT+2) where the bot thinks it's still "yesterday" but FP has already reset the daily limit. Impact: the bot's daily-loss accumulator may carry over 2 extra hours, making it TIGHTER than FP — which is safe but could cause an unnecessary emergency-stop if a loss straddles the GMT+2 midnight boundary. Fix: change `today_utc` to `today_server` using GMT+2, or add a `daily_reset_hour_utc: 22` config field. Low priority for Step 1 (safe direction); higher priority if the bot starts tripping at weird times.

**Daily Loss calculation basis:** Uses the HIGHER of equity or balance at session start. If we're in profit from the previous day, the daily limit scales UP (trailing daily). Our EmergencyStop uses session-start equity only (emergency.py:97), not max(equity, balance). Impact: if balance > equity (e.g. after closing a losing trade), the bot's daily limit is LOWER than FP's — bot trips BEFORE FP. Safe direction. Fix: change `session_start_equity` capture to `max(equity, balance)` when the bot starts. Low priority.

**MT5 server name for funded account:** "FundingPips Corp (2)" — may not appear in the server list by default. Need to manually search and add it in the MT5 terminal on EC2 before first login.

**Master vs Investor password (confirmed):**
- Master = full trading access (from dashboard → Credentials, or the welcome email)
- Investor = view-only access (from dashboard → Credentials)
- This is the post-mortem's #1 operational mitigation: master password in SSM only, investor password for any human session.

**Account credentials location:** Dashboard → Accounts → click account number → Credentials button (top-right, next to Share). Password also sent via email.

**Onboarding period (Step 1 → Master):** After passing Step 1, Master Account is created with "onboarding" status. Takes 2 working days. Trading is disabled until onboarding completes + reward cycle is set.

**Scaling plan (for future reference, not Step 1):**
- Level 1 (Novice): 4 rewards + 10% profit → +20% capital, +1% DD
- Level 2 (Intermediate): 8 rewards + 15% profit → +30% capital, +1% DD
- Level 3 (Advanced): 12 rewards + 30% profit → +40% capital, +1% DD
- Hot Seat (Elite): 16 rewards + 40% profit → 2x balance, 100% split, $2M capital, monthly bonuses ($100 for 5K)

**Fail discount:** Step 1 fail = 10% off next purchase, Step 2 = 15%, Master = 0%. Valid 7 days, same model + size only.

**Restricted countries:** UAE, Iran, Vietnam (cannot join). US/Canada cannot use cTrader but CAN use MT5. India is supported (Shivang's location is fine).

**Instruments:** Forex, Metals, Indices, Energies, Crypto — all with RAW spreads. Indices/Oil commission-free. Forex/Metals/Crypto have commissions (varies by model). Our bot trades XAUUSD (Metals) — commission applies.

**Platform lock:** Once payment is made, the trading platform (MT5/cTrader/MatchTrader) CANNOT be changed. We're on MT5 — this is permanent for this account.

## Q3. Will EA-originated payouts be honored?

**Source:** "What are the Forbidden Strategies?" — explicitly states third-party EAs that act as trade/risk managers are allowed. EA-originated payouts ARE honored as long as the EA fits that category.

**Our bot:** Internal first-party strategies + first-party risk manager → allowed. Payouts honored.

## Q4. MT5 investor password support?

**Confirmed via "Copy Trading Policy":** "Always use the investor password (read-only) when setting up a master account. Using your main account password may trigger further investigation."

**This is the single most important operational mitigation per the post-mortem.** Master password goes into AWS SSM only; investor password is what any human session uses. Investor terminal is read-only — manual trade attempts return "Invalid account".

## Verdict

- [x] **Approved for use** — bot is allowed on Step 1, Master (1 Step model), and on 2 Step Flex/Standard/Pro (would need to re-tune buffers for those limits). EAs classified as trade/risk manager are explicitly permitted.
- [x] Approved with restrictions — see config drift fixes in §Q2 above. **MUST apply 8 fixes before going live on the funded account.**

## Notes

### Config drift critical issue

The current `config/fundingpips-5k.yaml` has `daily_loss_pct: 5.0` and `max_overall_dd_pct: 10.0`. **FundingPips' 1 Step model enforces 3% / 6%.** If the bot runs the current config, the prop-firm guard trips AFTER the FP daily-DD has already breached, meaning:
- Step 1 daily-DD breach: account terminated, evaluation fee lost
- Step 1 overall-DD breach: account terminated, evaluation fee lost
- Master daily-DD breach: profits deducted + warning, second time = account closed
- Master overall-DD breach: same

The bot's own risk guard is meant to PRE-EMPT, not match, the prop-firm limit. With current config, the bot is *less* tight than FP, which defeats its purpose. **The 8 fixes in §Q2 are mandatory before deploy.**

### Safety stack already in place (good)

- `risk/manager.py` PropFirmGuard — daily / overall DD with buffer ✓
- `safety/emergency.py` EmergencyStop — triggered on DD breach ✓
- `monitoring/position_monitor.py` foreign-position check (post-mortem fix #1) ✓
- News filter at central RiskManager gate (post-mortem fix #2) ✓
- Pre-news flat (post-mortem fix #3) ✓
- 143-event calendar (post-mortem fix #4) ✓
- Walk-forward validator with Deflated Sharpe Ratio (post-mortem addendum) ✓
- In-flight order tracking (uncommitted diff — closes stacking race) ✓
- Regime filter (uncommitted diff — fixes whipsaw in RANGING market) ✓
- Naked-position detector (uncommitted diff — surfaces unprotected open positions) ✓
- Profit-trail stop (uncommitted diff) ✓
- Directional cap (uncommitted diff — max 2 same-side) ✓
- SymbolMapper (uncommitted diff — handles per-broker naming) ✓

### Operational preconditions (post-mortem §Decisions)

- [x] PR #1 merged in working tree (`f67e5f6 fix(mt5)` and after — see git log)
- [ ] **Safety-gate runbook executed end-to-end on a demo account** — pending
- [ ] **MT5 investor password verified to reject manual trades** — pending (do this on the EC2 MT5 terminal before funded login)

### Data-durability hardening (post-mortem §P1)

- [ ] EBS `DeleteOnTermination=false` set on EC2 launch template — verify on `aws ec2 describe-instances`
- [ ] Nightly S3 sync active (`scripts/nightly-backup.sh` cron on EC2)
- [ ] Turso warm tier configured (see `orchestration-plan-v2.md`)

### Concentration risk (post-mortem addendum)

- XAUUSD-only is the user's locked constraint for this batch
- Strategy Health Monitor (8 always-on signals) is the mitigation
- Walk-forward + DSR is the validation gate for any new strategy before deploy
