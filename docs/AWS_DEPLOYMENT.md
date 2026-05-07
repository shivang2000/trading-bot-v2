# AWS Deployment — trading-bot-v2

End-to-end deploy on AWS EC2 (ap-south-1 Mumbai). Designed for ~$45/mo
on-demand cost; ~2 months runway on $100 free credits.

Pairs with:
- `scripts/aws-provision.sh` — one-shot infra provisioner
- `scripts/bootstrap-ec2.sh` — EC2 first-boot setup (runs via user-data)
- `docs/RUNBOOK.md` — runtime ops + recovery
- `docs/post-mortem-5k.md` — discipline lessons (do not relitigate)

---

## 1. One-time AWS account setup

If this is your first AWS deploy from this laptop:

### 1a. Install AWS CLI v2 (mac)

```bash
curl "https://awscli.amazonaws.com/AWSCLIV2.pkg" -o "/tmp/AWSCLIV2.pkg"
sudo installer -pkg /tmp/AWSCLIV2.pkg -target /
aws --version  # should print aws-cli/2.x
```

### 1b. Get an IAM access key

In AWS Console:
1. IAM → Users → your user → Security credentials → Create access key
2. **Use case**: "Command Line Interface (CLI)"
3. Save the access key ID + secret somewhere safe (password manager)

Or simpler: create an IAM user `trading-bot-deploy` with `AdministratorAccess`
just for this deploy laptop. (Lock down later.)

### 1c. Configure CLI

```bash
aws configure
# AWS Access Key ID:     <paste>
# AWS Secret Access Key: <paste>
# Default region name:   ap-south-1
# Default output format: json
```

Verify:

```bash
aws sts get-caller-identity
# {
#   "UserId": "AIDA...",
#   "Account": "123456789012",   ← note this account ID
#   "Arn": "arn:aws:iam::123456789012:user/trading-bot-deploy"
# }
```

### 1d. Generate SSH key (if you don't have one)

```bash
[ -f ~/.ssh/id_ed25519 ] || ssh-keygen -t ed25519 -C "trading-bot-v2"
cat ~/.ssh/id_ed25519.pub  # confirm there's content
```

### 1e. Apply the $100 credit

In AWS Console:
- Billing → Credits → Apply credit code (if it's a code)
- OR: Billing → Free Tier (if it auto-applied)

Verify Credits dashboard shows balance.

---

## 2. Provision infrastructure

```bash
cd ~/dev/advanced-trading-bot/trading-bot-v2

export AWS_REGION=ap-south-1
export ACCOUNT_NAME=vantage-50
export REPO_URL=https://github.com/shivang2000/trading-bot-v2.git

./scripts/aws-provision.sh
```

The script will:
1. Create S3 bucket `trading-bot-v2-vantage-50-<account-id>` (versioned, 30-day lifecycle, all-public blocked)
2. Prompt you to enter SSM SecureString values (typed silently):
   - **MT5 master password** — from Vantage portal (rotate first per RUNBOOK §1.3 if leaked)
   - **Telegram bot token** — from BotFather (or empty to skip)
   - **Slack webhook URL** — from Slack app (or empty to skip)
   - **Anthropic API key** — sk-ant-... (or empty if not using Claude filter)
3. Create IAM role + instance profile (SSM read + S3 write + CloudWatch put)
4. Create security group (SSH + noVNC port 8080 from your current public IP only)
5. Import SSH key from `~/.ssh/id_ed25519.pub`
6. Find latest Ubuntu 24.04 LTS AMI in ap-south-1
7. Create 60 GB gp3 EBS data volume (DeleteOnTermination=false)
8. Launch t3.medium with bootstrap user-data
9. Allocate Elastic IP and attach
10. Print SSH + noVNC URLs

Total runtime: ~2 minutes for AWS calls + ~5 minutes for first-boot bootstrap on the instance.

---

## 3. First-boot verification

```bash
# Tail bootstrap progress
ssh ubuntu@<EIP> 'sudo tail -f /var/log/cloud-init-output.log'
# Watch for "Bootstrap complete" line at the end
```

Expected duration: 4-6 minutes (apt updates, Docker install, repo clone, image pull).

---

## 4. MT5 first-time login (one-time, manual)

The bot can't auto-login MT5 because Vantage's terminal stores credentials in
encrypted Windows registry — you log in once via VNC, MT5 remembers, bot uses
RPyC after that.

```
1. Open in browser: http://<EIP>:8080
2. VNC password: botpass
3. Inside MT5 (Wine):
   - File → Login to Trade Account
   - Login: <Vantage account number, e.g. 11836008>
   - Password: <master password — same one you put into SSM>
   - Server: VantageInternational-Demo  (or VantageInternational-Live for live)
4. Click "AutoTrading" green button (top toolbar) so EAs can submit orders
5. Close the noVNC tab — MT5 stays running
```

> **DO NOT** log in with master password from your laptop — only on the EC2
> noVNC. Master password should never leave AWS. Per RUNBOOK §1: any human
> chart inspection from your laptop must use the **investor password**.

---

## 5. Start the bot

```bash
ssh ubuntu@<EIP>
sudo -u bot bash -c 'cd /home/bot/trading-bot-v2 && docker compose -f docker-compose.ec2.yml restart trading-bot'

# Watch logs
sudo -u bot bash -c 'cd /home/bot/trading-bot-v2 && docker compose -f docker-compose.ec2.yml logs -f trading-bot'
```

Look for:

```
Connected to MT5 — account 11836008, balance $10000.00
RiskManager initialized: max_daily_loss_pct=5.0, max_drawdown_pct=15.0
SignalGenerator: scan_interval=15s, instruments=[XAUUSD]
PositionMonitor: started (poll_interval=30s)
TickStream: enabled=False (config.tick_engine.enabled)
```

If you see `MT5 reconnect attempt N/10` — MT5 isn't logged in. Re-do step 4.

---

## 6. Verify monitoring

After first 5 min:

```bash
# CloudWatch metrics
aws cloudwatch get-metric-statistics --region ap-south-1 \
    --namespace TradingBotV2 --metric-name mem_used_percent \
    --start-time $(date -u -v-15M +%FT%TZ) --end-time $(date -u +%FT%TZ) \
    --period 300 --statistics Average

# CloudWatch logs (will populate as bot writes to logs/trading.log)
aws logs tail /aws/trading-bot-v2 --region ap-south-1 --since 10m

# S3 backup will happen at 23:00 UTC nightly via cron — check next day:
aws s3 ls s3://trading-bot-v2-vantage-50-<account-id>/$(date -u +%Y-%m-%d)/
```

---

## 7. Cost monitoring

```bash
# Set a billing alarm at $80 (20 below the $100 credit cap)
aws cloudwatch put-metric-alarm --region us-east-1 \
    --alarm-name billing-trading-bot-v2 \
    --metric-name EstimatedCharges --namespace AWS/Billing \
    --dimensions Name=Currency,Value=USD \
    --statistic Maximum --period 21600 --evaluation-periods 1 \
    --threshold 80 --comparison-operator GreaterThanThreshold \
    --alarm-actions <your-SNS-topic-arn>
```

Reminder: **Billing alarms must be in us-east-1**, regardless of where your
EC2 lives.

Estimated monthly cost (ap-south-1):

| Item | $/mo |
|---|---|
| t3.medium on-demand | $35 |
| 60 GB gp3 (data) | $5 |
| 8 GB gp3 (root) | $0.65 |
| Elastic IP attached | $4 (only $0 when attached) |
| S3 backups (~10 GB) | $0.25 |
| CloudWatch logs + metrics | $1 |
| Outbound data (~1 GB/mo) | $0.10 |
| **Total** | **~$46/mo** |

$100 credits ≈ **2 months runway**. Migrate to Hetzner ($60/mo full setup)
before credits expire.

---

## 8. Recovery from instance termination

Per RUNBOOK §3. The data volume (`/data` mount) is preserved by
`DeleteOnTermination=false`. To recover:

```bash
# 1. Re-run aws-provision.sh in the same region/account_name
./scripts/aws-provision.sh
# It detects the existing data volume and reuses it

# 2. New instance launches, attaches existing data volume, mounts /data
# 3. SQLite, logs, mt5_data are all there — bot resumes trailing stops on
#    next start automatically
```

If the data volume itself is lost (or you're starting in a fresh region):
restore from S3:

```bash
mkdir recovered && aws s3 sync s3://<bucket>/$(date -u -d "1 day ago" +%Y-%m-%d)/ recovered/
sudo cp recovered/trading_bot_v2.db /data/db/trading_bot_v2.db
sudo -u bot docker compose -f docker-compose.ec2.yml restart trading-bot
```

---

## 9. Migrate to Hetzner before credits expire

Trigger: AWS billing dashboard shows credits remaining < 1 month of burn.

Steps:
1. Provision Hetzner CCX13 (~$16/mo) or CCX23 (~$26/mo).
2. SSH in; bootstrap-ec2.sh works as-is on Hetzner Ubuntu (no AWS-specifics
   except SSM/CloudWatch/S3 — flag those as optional in env)
3. Replace SSM with `.env` file containing the same secrets (carefully)
4. Replace S3 backup with Hetzner Object Storage (S3-compatible API: drop-in
   replacement, just change `--endpoint-url` in `nightly-backup.sh`)
5. Replace CloudWatch with self-hosted Prometheus + Grafana (or omit;
   nightly backup + cron heartbeat is the minimum)
6. DNS: point your domain (if any) at the Hetzner box's IPv4
7. Snapshot AWS data volume to S3 → restore to Hetzner
8. Stop AWS bot. Start Hetzner bot. Verify trades resume.
9. Terminate AWS instance + release EIP after 7 days dual-running.

Cost after migration: ~$16-26/mo (Hetzner instance) + ~$5/mo (Hetzner
Object Storage) = $20-30/mo. Half of AWS.

---

## 10. Tear-down (if needed)

```bash
# Identify resources
aws ec2 describe-instances --filters "Name=tag:Project,Values=trading-bot-v2" "Name=tag:Account,Values=vantage-50"

# Terminate instance (data volume survives because DeleteOnTermination=false)
aws ec2 terminate-instances --instance-ids <i-id>

# Release EIP
aws ec2 release-address --allocation-id <alloc-id>

# Delete data volume ONLY if you're certain you don't need it
aws ec2 delete-volume --volume-id <vol-id>

# Empty + delete S3 bucket
aws s3 rm s3://<bucket> --recursive
aws s3 rb s3://<bucket>

# Detach + delete IAM role + instance profile
aws iam remove-role-from-instance-profile --instance-profile-name <profile> --role-name <role>
aws iam delete-instance-profile --instance-profile-name <profile>
aws iam delete-role-policy --role-name <role> --policy-name trading-bot-v2-policy
aws iam delete-role --role-name <role>

# Delete SG (after instance gone)
aws ec2 delete-security-group --group-id <sg-id>

# SSM parameters
aws ssm delete-parameters --names $(aws ssm get-parameters-by-path \
    --path /trading-bot-v2/vantage-50 --query 'Parameters[].Name' --output text)
```
