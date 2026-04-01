# Trading Bot V2 — FundingPips Prop Firm Edition

Automated XAUUSD trading bot built for FundingPips 2-Step challenges. Runs 5 strategies simultaneously across M5 scalping and M15 swing timeframes with prop firm safety guardrails.

## Current Status

- **$5k Account**: Step 1 PASSED (10% target hit Day 1). Waiting 3-day minimum.
- **Deployed on EC2**: 5 strategies live, prop firm guard active.
- **$30 Vantage Account**: Grew from $30 to $376 (12.5x in 10 days) — proof the strategies work.

## Architecture

```
Signal Generator (dual-loop)
├── _scan_loop (60s) ─── EMA Pullback + London Breakout (M15, 4 instruments)
├── _scalping_loop (15s) ── MTF Momentum + Keltner Squeeze + Dual Supertrend (M5, XAUUSD)
│
├── Claude AI Filter (optional, confidence gate 0.65)
├── SMC Confluence Layer (order blocks, FVGs, liquidity sweeps)
│
└── RiskManager
    ├── PropFirmGuard ($7 safety buffers, drawdown tiers, profit target halt)
    ├── Directional Exposure Check (max 2 same-direction)
    ├── Position Limits (max 4 total)
    └── PositionSizer (risk-based lot calculation with leverage)
```

## Strategies

| Strategy | Type | Timeframe | Instruments | R:R | Best Risk |
|---|---|---|---|---|---|
| **MTF Momentum** | Scalping | M5 | XAUUSD | 1:1.95 | 1% |
| **Keltner Squeeze** | Scalping | M5 | XAUUSD | 1:4.00 | 0.5-1% |
| **Dual Supertrend** | Scalping | M5 | XAUUSD | 1:1.92 | 1% |
| **EMA Pullback** | Swing | M15 | All 4 | 1:1.50 | 1% |
| **London Breakout** | Session | M15 | All 4 | 1:1.50 | 1% |

## Backtesting Results (18 months, Oct 2024 - Apr 2026)

### Optimal Configuration Per Account Size

| Account | Config | Risk | Monthly (80% split) | Annual |
|---|---|---|---|---|
| **$5,000** | `fundingpips-5k` | 1% | ~$284/mo | $3,408/yr |
| **$10,000** | `fundingpips-10k` | 0.5% | ~$574/mo | $6,888/yr |
| **$100,000** | `fundingpips-100k` | 1% | ~$5,739/mo | $68,858/yr |

Full matrix: 36 backtests (3 accounts x 4 risk levels x 3 strategies). See the HTML report.

## Key Files

### Configuration

| File | Purpose |
|---|---|
| `config/base.yaml` | Base config (all strategies, 4 instruments) |
| `config/fundingpips.yaml` | Current deployed config (active $5k account) |
| `config/fundingpips-5k.yaml` | Optimal $5k config: 1% risk, all 5 strategies |
| `config/fundingpips-10k.yaml` | Optimal $10k config: 0.5% risk, NO MTF (loses money on $10k) |
| `config/fundingpips-100k.yaml` | Optimal $100k config: 1% risk, all 5 strategies |

### Reports

| File | Purpose |
|---|---|
| `reports/propfirm_analysis.html` | Full backtesting matrix with charts and recommendations |
| `docs/DEPLOYMENT-GUIDE.md` | Step-by-step deployment instructions per account size |

### Core Source

| File | Purpose |
|---|---|
| `src/analysis/signal_generator.py` | Dual-loop signal scanner (M15 + M5) |
| `src/analysis/strategies/` | All strategy implementations |
| `src/analysis/smc_confluence.py` | SMC confidence booster (order blocks, FVGs, sweeps) |
| `src/analysis/claude_signal_filter.py` | Claude AI pre-trade evaluation (optional) |
| `src/risk/manager.py` | Risk manager with directional exposure check |
| `src/risk/prop_firm_guard.py` | FundingPips breach prevention + payout reset |
| `src/risk/position_sizer.py` | Leverage-aware lot sizing |
| `src/mt5/client.py` | MT5 RPyC connection to the trading terminal |
| `src/main.py` | Bot entry point, startup, daily counter reset |

### Scripts

| File | Purpose |
|---|---|
| `scripts/deploy-propfirm.sh` | Interactive deployment setup wizard |
| `scripts/backtest_scalping.py` | Scalping strategy backtester with prop firm mode |
| `scripts/run_propfirm_matrix.sh` | Run full 32-scenario backtest matrix |
| `scripts/generate_propfirm_report.py` | Generate HTML report from backtest results |

### Tests

| File | Purpose |
|---|---|
| `tests/unit/test_prop_firm_guard.py` | PropFirmGuard: payout reset + directional exposure |

## Quick Start

### 1. Deploy to EC2 (existing server)

```bash
# Set account config
echo "CONFIG_OVERLAY=fundingpips-5k" > .env
echo "ANTHROPIC_API_KEY=your-key" >> .env

# Sync to EC2
rsync -avz --exclude '.git' --exclude 'data/' --exclude '.env' \
  ./ ec2-user@<EC2-IP>:~/trading-bot-v2/

# On EC2: build and run
docker build -t trading-bot-v2-trading-bot .
docker-compose -f docker-compose.ec2.yml up -d
```

### 2. Switch between accounts

```bash
# In .env on EC2:
CONFIG_OVERLAY=fundingpips-5k    # For $5k account
CONFIG_OVERLAY=fundingpips-10k   # For $10k account
CONFIG_OVERLAY=fundingpips-100k  # For $100k account
```

### 3. Run backtests locally

```bash
# Single strategy
python3 scripts/backtest_scalping.py --symbol XAUUSD --timeframe M5 \
  --prop-firm --account-size 5000 --risk-pct 1.0 --strategy m5_mtf_momentum

# Full matrix
bash scripts/run_propfirm_matrix.sh

# Generate report
python3 scripts/generate_propfirm_report.py
open reports/propfirm_analysis.html
```

## FundingPips Rules

| Rule | Value |
|---|---|
| Daily Loss Limit | 5% of starting balance |
| Overall Drawdown | 10% from initial balance |
| Max Risk/Trade ($5k-$10k) | 3% |
| Max Risk/Trade ($50k+) | 2% |
| Min Trading Days | 3 |
| Profit Split (Funded) | 80/20 (you keep 80%) |
| Step 1 Target | 8% or 10% |
| Step 2 Target | 5% |

## Safety Features

- **PropFirmGuard**: $7 USD safety buffers before daily/DD hard limits
- **Directional Limit**: Max 2 same-direction positions
- **Position Cap**: Max 4 open simultaneously
- **Drawdown Tiers**: Risk auto-reduces at -4% (50%) and -8% (30%)
- **Profit Target Halt**: Stops trading when challenge target reached
- **Friday Auto-Close**: All positions closed at 21:00 UTC
- **Daily Counter Reset**: Auto-resets on restart (prevents stale block)
- **Payout Reset**: `reset_after_payout()` for funded account withdrawals

## Scaling Roadmap

```
$5k Step 1 ✓ → Step 2 (bot) → Funded (~$284/mo)
                                    ↓ save profits
$10k Step 1 → Step 2 (bot) → Funded (~$574/mo)
                                    ↓ save profits
$100k Step 1 → Step 2 (bot) → Funded (~$5,739/mo = $68,858/yr)
```

## Directory Structure

```
trading-bot-v2/
├── config/
│   ├── base.yaml                 # Base configuration
│   ├── fundingpips.yaml          # Active deployment config
│   ├── fundingpips-5k.yaml       # Optimal $5k config
│   ├── fundingpips-10k.yaml      # Optimal $10k config
│   ├── fundingpips-100k.yaml     # Optimal $100k config
│   └── news_calendar.csv         # High-impact news events
├── src/
│   ├── analysis/
│   │   ├── signal_generator.py   # Dual-loop scanner
│   │   ├── strategies/           # All strategy implementations
│   │   ├── smc_confluence.py     # SMC confidence booster
│   │   └── claude_signal_filter.py
│   ├── risk/
│   │   ├── manager.py            # Risk limits + directional check
│   │   ├── prop_firm_guard.py    # FundingPips safety guard
│   │   └── position_sizer.py    # Lot calculation
│   ├── mt5/client.py             # MT5 RPyC connection
│   ├── main.py                   # Entry point
│   └── tracking/database.py      # Trade tracking DB
├── scripts/
│   ├── backtest_scalping.py      # Backtester
│   ├── run_propfirm_matrix.sh    # Full matrix runner
│   ├── generate_propfirm_report.py # Report generator
│   └── deploy-propfirm.sh        # Deployment wizard
├── tests/unit/
│   └── test_prop_firm_guard.py
├── reports/
│   └── propfirm_analysis.html    # Full backtest report
├── docs/
│   └── DEPLOYMENT-GUIDE.md       # Deployment instructions
├── data/
│   ├── backtest_cache/           # Historical price data
│   └── backtest_results/         # Backtest JSON outputs
└── docker-compose.ec2.yml        # EC2 deployment
```
