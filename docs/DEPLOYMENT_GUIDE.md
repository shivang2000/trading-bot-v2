# FundingPips Trading Bot — EC2 Deployment Guide

## Architecture

```
EC2 Instance (x86_64, Amazon Linux)
├── metatrader5 container  — MT5 terminal (VNC + RPyC port 8001)
└── trading-bot container  — Python bot (reads config via Docker volume)
         │
         ├── config/fundingpips.yaml  (overlaid via CONFIG_OVERLAY=fundingpips)
         ├── data/                    (trade history, session state)
         └── logs/trading.log        (live log stream)
```

**Active strategies (prop firm):**
- `m5_keltner_squeeze` — ~$203/win, 6.6% DD, R:R 5.2x
- `m5_mtf_momentum`   — ~$133/win, 2.8% DD, R:R 2.2x

**Safety buffer (dollar-based):** Bot stops $7 before the hard breach limit.

| Account | Daily hard limit | Bot stops at | DD hard floor | Bot stops at |
|---------|-----------------|--------------|---------------|--------------|
| $5,000  | $250            | **$243**     | $4,500        | **$4,507**   |
| $10,000 | $500            | **$493**     | $9,000        | **$9,007**   |

---

## Prerequisites

- EC2 instance: `t3.medium` or larger, Amazon Linux 2023, **x86_64** (MT5 requires x86)
- Security group: inbound `22` (SSH), `8080` (noVNC browser), optionally `5900` (VNC client)
- Docker + Docker Compose v2 installed
- FundingPips MT5 login credentials (server, account number, password)
- `.env` file with required keys (see step 1)

---

## Step 1: Configure Locally

Run the interactive setup script — it patches `config/fundingpips.yaml` and creates `.env`:

```bash
cd trading-bot-v2
./scripts/deploy-propfirm.sh
```

The script will ask:
1. **Account size** — `$5,000` or `$10,000`
2. **Phase** — Step 1 (evaluation), Step 2 (verification), or Funded
3. **Step 1 profit target** (if Step 1) — `8%` standard or `10%` discounted (cheaper challenge)
4. **Risk per trade** — 1% conservative / 2% standard (recommended) / 3% aggressive

After running, the script prints a summary and validates the config loads without errors.

### Required `.env` Variables

```
CONFIG_OVERLAY=fundingpips

# MT5 connection (set by docker-compose automatically via container name)
MT5_HOST=metatrader5
MT5_PORT=8001

# Telegram signal listener
TELEGRAM_API_ID=<your api id>
TELEGRAM_API_HASH=<your api hash>
TELEGRAM_PHONE=<your phone e.g. +1234567890>

# Slack notifications
SLACK_WEBHOOK_URL=<your webhook url>

# Claude AI pre-trade filter
ANTHROPIC_API_KEY=<your key>
```

---

## Step 2: Transfer to EC2

```bash
# From your local machine (trading-bot-v2 directory)
rsync -avz --exclude '__pycache__' --exclude '.git' --exclude 'data/' \
  ./ ec2-user@<EC2_IP>:~/trading-bot-v2/

# Or if already cloned on EC2:
ssh ec2-user@<EC2_IP>
cd ~/trading-bot-v2
git pull origin main
```

---

## Step 3: First-Time MT5 Setup

```bash
# On EC2 — start both containers
docker compose -f docker-compose.ec2.yml up -d
```

1. Open **`http://<EC2_IP>:8080`** in your browser (noVNC web interface)
2. Log into MT5 with your FundingPips credentials (server + account + password)
3. Click **AutoTrading** button in MT5 toolbar (must be green/enabled)
4. Restart the bot container to pick up the logged-in MT5 session:
   ```bash
   docker compose -f docker-compose.ec2.yml restart trading-bot
   ```

**Subsequent starts** (after first login): `docker compose -f docker-compose.ec2.yml up -d` — MT5 session persists in `./mt5_data/`.

---

## Step 4: Verify Deployment

```bash
# Check container status and health
./scripts/ec2-status.sh ec2-user@<EC2_IP>

# Stream live logs
./scripts/ec2-logs.sh ec2-user@<EC2_IP>
```

**Expected log lines on startup:**
```
PropFirmGuard: phase=step1, account=$5000, floor=$4500 (buffer=$4507), daily_limit=$250 (buffer=$243), target=$400
RiskManager initialized
Signal generator started — instruments=[XAUUSD]
MT5 connected: broker=..., account=...
```

If `PropFirmGuard` shows `buffer=$4507` and `buffer=$243` — dollar buffers are active.

---

## Step 5: Verify Config for Account Size

To switch between `$5k` and `$10k`, re-run the setup script locally and re-sync:

```bash
# Local
./scripts/deploy-propfirm.sh   # select $10,000

# Sync config only (fast — no rebuild)
rsync -avz config/ ec2-user@<EC2_IP>:~/trading-bot-v2/config/
rsync -avz .env ec2-user@<EC2_IP>:~/trading-bot-v2/.env

# Restart bot to pick up new config
./scripts/ec2-restart.sh ec2-user@<EC2_IP>
```

---

## Ongoing Operations

### Advancing from Step 1 → Step 2
```bash
./scripts/deploy-propfirm.sh   # select Step 2, profit_target_pct = 5%
rsync -avz config/ ec2-user@<EC2_IP>:~/trading-bot-v2/config/
./scripts/ec2-restart.sh ec2-user@<EC2_IP>
```

### Enabling/Disabling Strategies
Edit `config/fundingpips.yaml` → `strategies.scalping.strategies_enabled` list, then rsync + restart.

### Adding Capital (balance adjustment)
Edit `config/base.yaml` → `account.balance_adjustments`, add a new entry:
```yaml
balance_adjustments:
  - date: "2026-04-01"
    type: "deposit"
    amount: 5000.0
    note: "FundingPips Step 2 account"
```

### Changing Risk Level
Re-run `./scripts/deploy-propfirm.sh` — select new risk % — rsync config — restart.

---

## Emergency Procedures

### Stop All Trading Immediately
```bash
ssh ec2-user@<EC2_IP>
cd ~/trading-bot-v2
docker compose -f docker-compose.ec2.yml stop trading-bot
```

### Check If Guard Fired
```bash
./scripts/ec2-logs.sh ec2-user@<EC2_IP> | grep "BLOCKED by PropFirmGuard"
./scripts/ec2-logs.sh ec2-user@<EC2_IP> | grep "Daily loss"
```

### Roll Back to Conservative Risk (1%)
```bash
# Locally
./scripts/deploy-propfirm.sh   # select Conservative 1%
rsync -avz config/ ec2-user@<EC2_IP>:~/trading-bot-v2/config/
./scripts/ec2-restart.sh ec2-user@<EC2_IP>
```

### Full Restart (if bot hangs)
```bash
ssh ec2-user@<EC2_IP>
cd ~/trading-bot-v2
docker compose -f docker-compose.ec2.yml down
docker compose -f docker-compose.ec2.yml up -d
```

---

## Monitoring Checklist

### Daily
- [ ] `ec2-status.sh` — both containers healthy
- [ ] No `BLOCKED by PropFirmGuard` in logs
- [ ] Current equity above DD floor buffer ($4,507 / $9,007)
- [ ] Daily loss not approaching buffer ($243 / $493)
- [ ] Trades appearing in Slack notifications

### Weekly
- [ ] Profit progress toward phase target (Step 1: 8% or 10%, Step 2: 5%)
- [ ] Per-strategy performance: Keltner PF ≥ 1.2, MTF PF ≥ 1.3
- [ ] Review `logs/trading.log` for any errors or warnings

---

## Strategy Performance Reference

Backtested on XAUUSD M5, 15-month period (Jan 2025 – Mar 2026), 2% risk, $5k account:

| Strategy | Avg Win | Profit Factor | Max DD | R:R | Notes |
|----------|---------|---------------|--------|-----|-------|
| m5_keltner_squeeze | ~$203 | ≥ 1.20 | 6.6% | 5.2x | Best absolute return |
| m5_mtf_momentum | ~$133 | ≥ 1.36 | 2.8% | 2.2x | Best risk-adjusted |

**Step 1 pass estimate** (2% risk, Keltner only): ~26 winning trades to reach 8% target on $5k.
