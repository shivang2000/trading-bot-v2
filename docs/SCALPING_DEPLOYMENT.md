# Scalping System Deployment Guide

## Overview

This guide covers deploying the scalping strategies from the `research/scalping` branch to the EC2 demo/live trading instance. The scalping system adds 11 strategies (3 proven profitable) alongside the existing M15 strategies (EMA Pullback + London Breakout).

## Prerequisites

- EC2 instance running trading-bot-v2 on `main` branch
- MT5 connected via RPyC (localhost:8001)
- Slack webhook configured for notifications
- Demo account with at least $100 capital

## Step 1: Switch to Scalping Branch

```bash
ssh ec2-user@<ec2-ip>
cd /path/to/trading-bot-v2
git fetch origin
git checkout research/scalping
git pull origin research/scalping
```

## Step 2: Configure Strategies

Edit `config/base.yaml`:

### Enable Only Proven Strategies
```yaml
scalping:
  enabled: true
  profit_growth_factor: 0.50    # use 50% of profits for sizing
  max_trades_per_strategy: 1     # 1 position per strategy
  max_total_open_positions: 10
  scan_interval_seconds: 15      # scan every 15 seconds
  strategies_enabled:
    - m5_mtf_momentum           # PF 1.36, DD 10% — BEST risk-adjusted
    - m5_keltner_squeeze        # PF 1.20, DD 19%
    - m5_dual_supertrend        # PF 1.15, high return but high DD
```

### Risk Limits
```yaml
risk:
  max_open_positions: 10
  max_positions_per_symbol: 5
  max_daily_trades: 200
```

### Account
```yaml
account:
  initial_balance: 100
  mode: "demo"
  balance_adjustments:
    - date: "2026-03-27"
      type: "deposit"
      amount: 100.0
      note: "Initial deposit"
```

## Step 3: Restart Bot

```bash
docker-compose down
docker-compose up -d
docker-compose logs -f trading-bot
```

## Step 4: Verify

Watch for these log messages:
```
SignalGenerator started (X strategies active: EMA Pullback, London Breakout, ...)
Scalping scan loop starting (interval=15s, 3 strategies)
Scalping strategy enabled: m5_mtf_momentum
Scalping strategy enabled: m5_keltner_squeeze
Scalping strategy enabled: m5_dual_supertrend
```

## Expected Behavior

| Strategy | Trades/Day | Avg P&L | Session |
|----------|-----------|---------|---------|
| MTF Momentum | 1-3 | +$0.24 | London+NY |
| Keltner Squeeze | 1-2 | +$0.17 | All sessions |
| Dual Supertrend | 5-10 | +$0.32 | London+NY |

Total expected: 7-15 trades/day on XAUUSD

## Slack Notifications

You'll receive:
- Trade opened: strategy name, confidence, R:R ratio
- Trade closed: P&L, duration, daily summary
- Profit milestones: alerts at $5, $10, $20, $30, $50
- Loss warnings: alerts at -$5, -$10
- Position updates: periodic unrealized P&L for all open trades

## Enabling/Disabling Strategies

To disable a strategy, comment it out in `config/base.yaml`:
```yaml
strategies_enabled:
  - m5_mtf_momentum
  # - m5_keltner_squeeze    # DISABLED
  - m5_dual_supertrend
```

Then restart: `docker-compose restart trading-bot`

## Adding/Removing Capital

When you deposit or withdraw money, update `config/base.yaml`:
```yaml
balance_adjustments:
  - date: "2026-03-27"
    type: "deposit"
    amount: 100.0
    note: "Initial deposit"
  - date: "2026-04-01"
    type: "withdrawal"
    amount: 30.0
    note: "Profit withdrawal"
```

This ensures the position sizer calculates risk correctly.

## Emergency Stop

If something goes wrong:
```bash
# Option 1: Stop the bot
docker-compose down

# Option 2: Disable all scalping (keep M15 strategies running)
# Edit config/base.yaml:
#   scalping:
#     enabled: false
# Then: docker-compose restart trading-bot
```

## Rollback to Main Branch

```bash
docker-compose down
git checkout main
docker-compose up -d
```

This returns to the original 2-strategy system (EMA Pullback + London Breakout).

## Monitoring Checklist

Daily checks:
- [ ] Slack notifications arriving
- [ ] Trades opening and closing within expected frequency
- [ ] No emergency stop triggered
- [ ] Equity growing or stable (not declining steadily)
- [ ] Trailing stops activating on profitable trades

Weekly review:
- [ ] Check per-strategy P&L in Slack strategy summary
- [ ] Disable any strategy with PF < 0.9 over 50+ trades
- [ ] Compare live results with backtest expectations
