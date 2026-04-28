# Funded Trader Markets (FTM) — bot-allowed approval

**Status:** PENDING — written confirmation required before any FTM account is activated.

## What we'll ask

Use the template in `orchestration-plan-v2.md` Spec 2 / Template B. Send via FTM support portal (NOT chat — paper trail required). Forward the response email/PDF and link or paste it below.

## Response received

**Date:** _(YYYY-MM-DD)_
**Channel:** _(support portal ticket # / email / Discord)_
**Reviewer:** _(name + role)_

### Q1. EA / automated trading permitted on 1-Step / 2-Step / Instant Funding / Simulated Funded?

> _(verbatim)_

### Q2. Prohibited strategies

- HFT / latency arbitrage: _(allowed/not allowed)_
- Reverse martingale / grid: _(allowed/not allowed)_
- News trading windows: _(specific minutes around HIGH events?)_
- Sub-N-second scalping: _(allowed/not allowed)_
- Copy-trading between accounts: _(allowed/not allowed)_
- Frequency / count limits per EA: _(any cap)_

### Q3. Funded payout under EA use — honored?

> _(verbatim)_

### Q4. Platform availability for India

> _(MT5 available? cTrader? Match-Trader? TradeLocker?)_

### Q5. MT5 investor password support?

> _(yes/no — note FTM also supports cTrader/Match-Trader/TradeLocker; investor pattern may differ per platform)_

## Verdict

- [ ] Approved (variant: ___, phase: ___)
- [ ] Approved with restrictions
- [ ] **Not approved** — do NOT activate any FTM account in `config/accounts.yaml`

## Until written approval lands

`config/propfirms/ftm.yaml` ships with `bot_allowed: unknown` for every variant. The Management Agent's pre-flight refuses to mark an account `active` unless `propfirms.<slug>.variants.<variant>.bot_allowed == true` AND `bot_allowed_evidence` references this doc with verdict approved. This is a hard gate, not a soft warning.

## Notes

_(operational implications, link to ticket reference, anything FTM mentions about Indian residents specifically)_
