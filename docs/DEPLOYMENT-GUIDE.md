# FundingPips Deployment Guide

## Quick Reference: Which Config for Which Account

| Account | Config File | Risk | Strategies | Monthly Income (80%) |
|---|---|---|---|---|
| **$5,000** | `config/fundingpips-5k.yaml` | 1% | MTF + Keltner + DST + EMA + London | ~$284/mo |
| **$10,000** | `config/fundingpips-10k.yaml` | 0.5% | Keltner + DST + EMA + London (NO MTF) | ~$574/mo |
| **$100,000** | `config/fundingpips-100k.yaml` | 1% | MTF + Keltner + DST + EMA + London | ~$5,739/mo |

## Deployment Steps

### 1. Set the config overlay in `.env`

```bash
# For $5k account:
CONFIG_OVERLAY=fundingpips-5k

# For $10k account:
CONFIG_OVERLAY=fundingpips-10k

# For $100k account:
CONFIG_OVERLAY=fundingpips-100k
```

### 2. Update phase in the config file

Edit the config file for your account size:
```yaml
prop_firm:
  phase: "step1"        # Starting a new challenge
  profit_target_pct: 10.0   # Step1=10.0 (or 8.0 for standard)
```

Phase progression:
```
step1 → profit_target_pct: 10.0 (or 8.0)
step2 → profit_target_pct: 5.0
funded → profit_target_pct: 0  (change phase to "master")
```

### 3. Enter MT5 credentials

Log into MT5 via VNC (`http://<EC2-IP>:8080`):
- File → Login to Trade Account
- Enter FundingPips credentials
- Enable AutoTrading (green play button)

### 4. Deploy

```bash
# On EC2:
cd ~/trading-bot-v2
docker-compose -f docker-compose.ec2.yml stop trading-bot
docker build -t trading-bot-v2-trading-bot .
docker rm -f trading-bot-v2
docker-compose -f docker-compose.ec2.yml up -d --no-build trading-bot

# Verify:
docker logs trading-bot-v2 --tail 20
```

Look for:
- `PropFirmGuard: phase=step1, account=$XXXX`
- `Trading Bot V2 is LIVE`
- `Risk per trade: X.X%`
- Strategy names in logs

### 5. Monitor

```bash
# Live logs:
docker logs -f trading-bot-v2

# Check for trades:
docker logs trading-bot-v2 2>&1 | grep -E "FILLED|BLOCKED|SKIPPED"
```

## Scaling Roadmap

```
Phase 1: $5k Challenge ($30 entry fee)
├── Step 1: 10% target ($500) — PASSED ✓
├── Step 2: 5% target ($250) — bot at 1% risk, ~14 weeks
├── Funded: earning ~$284/month
└── Save profits for $10k challenge

Phase 2: $10k Challenge (~$60 entry fee)
├── Step 1: 10% target ($1,000)
├── Step 2: 5% target ($500) — bot at 0.5% risk
├── Funded: earning ~$574/month
└── Save profits for $100k challenge

Phase 3: $100k Challenge (~$500 entry fee)
├── Step 1: 10% target ($10,000)
├── Step 2: 5% target ($5,000) — bot at 1% risk
└── Funded: earning ~$5,739/month ($68,858/year)
```

## Strategy Reference

| Strategy | Type | Timeframe | Instruments | Notes |
|---|---|---|---|---|
| MTF Momentum | Scalping | M5 | XAUUSD | Best all-rounder. Loses money on $10k. |
| Keltner Squeeze | Scalping | M5 | XAUUSD | Best R:R (1:4). Profitable at all account sizes. |
| Dual Supertrend | Scalping | M5 | XAUUSD | High frequency. Loses money at 2% risk. |
| EMA Pullback | Swing | M15 | All 4 | Proven live ($30→$376). Wide SL, small lots. |
| London Breakout | Session | M15 | All 4 | Asian range breakout. Max 2 trades/day. |

## FundingPips Rules

| Rule | $5k/$10k | $100k |
|---|---|---|
| Daily Loss Limit | 5% | 5% |
| Overall DD | 10% (from initial) | 10% (from initial) |
| Max Risk/Trade | 3% | 2% |
| Min Trading Days | 3 | 3 |
| Inactivity Limit | 30 days | 30 days |
| Friday Auto-Close | 21:00 UTC | 21:00 UTC |
| Profit Split (Funded) | 80/20 | 80/20 |

## Safety Features

- **PropFirmGuard**: Stops trading $7 before daily/DD hard limits
- **Directional Limit**: Max 2 same-direction positions (prevents correlated losses)
- **Position Cap**: Max 4 open positions across all strategies
- **Drawdown Tiers**: Risk auto-reduces at -4% (50%) and -8% (30%)
- **Profit Target Halt**: Stops new trades when challenge target is reached
- **Friday Auto-Close**: Closes all positions before weekend gap
- **Daily Counter Reset**: Resets on bot restart (prevents stale count blocking)

## Troubleshooting

| Issue | Fix |
|---|---|
| "max_daily_trades exceeded" | Restart bot (counter resets automatically) |
| Claude filter SKIPPED all signals | Set `claude_filter.enabled: false` |
| No scalping signals | Normal — strategies wait for specific setups |
| Wrong MT5 account | VNC → MT5 → File → Login with correct creds |
| Profit target reached | Change phase in config, restart |
