# Trading Bot V2 — Runbook

Operational playbook. Pairs with `docs/DEPLOYMENT-GUIDE.md` (deploy mechanics)
and `docs/post-mortem-5k.md` (incident lessons).

---

## 1. MT5 Password Pattern

**Rule:** the master password lives only in AWS SSM and is injected into the
trading-bot container at start. The investor (read-only) password is what a
human uses for any VNC inspection.

| Password | Who uses it | Stored where | Permissions |
|---|---|---|---|
| **Master** | Bot (container env) | AWS SSM Parameter Store, `/trading-bot-v2/<account>/mt5_master` | Trade + read |
| **Investor** | Human via noVNC | Password manager (1Password etc.) — never in-repo | Read-only |

Why: the $5k FundingPips bust was triggered by a manual trade during news
(`docs/post-mortem-5k.md`). With investor-only access, MT5 rejects the
"Buy/Sell" buttons with `Invalid account` — you cannot place a trade by
mistake.

### Setup

1. In Vantage portal, set the investor password explicitly (it's separate from
   master):
   ```
   Vantage portal → Account → Change MT5 password → Investor (read-only)
   ```
2. Store master in SSM:
   ```bash
   aws ssm put-parameter \
       --name /trading-bot-v2/vantage-50/mt5_master \
       --type SecureString \
       --value "<master-pwd>"
   ```
3. The bot's `start-mt5.sh` reads this at container boot — never bake into
   the AMI or commit to `.env`.

### Rotation

**Mandatory after:** any time credentials surface in logs, chat, or shared docs.
Already triggered once this project (see plan note about leaked Vantage creds).

```bash
# 1. Set new master in Vantage portal
# 2. Update SSM
aws ssm put-parameter --name /trading-bot-v2/vantage-50/mt5_master \
    --type SecureString --value "<new>" --overwrite
# 3. Restart bot stack
docker compose -f docker-compose.ec2.yml restart
```

---

## 2. EBS Durability

`scripts/bootstrap-ec2.sh` provisions the EBS volume with
`DeleteOnTermination=false`. Verify on every fresh launch:

```bash
aws ec2 describe-instances --instance-ids <i-id> \
    --query 'Reservations[].Instances[].BlockDeviceMappings[].Ebs'
# Look for: "DeleteOnTermination": false
```

Combined with the nightly S3 sync (`scripts/nightly-backup.sh`) you get
two layers of recovery: instance-local EBS and S3 object backup.

---

## 3. Recovery from Lost EC2

### Symptoms
- Instance terminated (manual / auto-scaling / stop-with-delete)
- EBS detached/destroyed
- Bot unreachable

### Steps

1. **Launch new EC2** in same VPC + AZ. Mount the preserved EBS if alive,
   otherwise pull the most recent S3 snapshot:
   ```bash
   aws s3 sync s3://<BACKUP_S3_BUCKET>/$(date -u +%Y-%m-%d)/ ./recovered/
   # or yesterday's if today's hasn't been written
   aws s3 sync s3://<BACKUP_S3_BUCKET>/$(date -u -d "1 day ago" +%Y-%m-%d)/ ./recovered/
   ```
2. **Restore SQLite**:
   ```bash
   mkdir -p data
   cp recovered/trading_bot_v2.db data/
   ```
3. **Re-deploy stack** (assumes IAM role can read SSM):
   ```bash
   docker compose -f docker-compose.ec2.yml up -d
   ```
4. **Verify state restoration** in logs:
   ```
   Restored N trailing stop(s) from database
   Restored N partial profit state(s) from database
   Pre-synced N MT5 position(s) into cache
   ```
5. **Sanity check** by checking a known position's SL via VNC matches
   `trailing_stops.current_sl` in the SQLite snapshot.

---

## 4. Pre-news Operational Discipline

When the NewsEventFilter flags a high-impact event ≤30 min away:

1. Bot will Telegram-alert: `Pre-news flat at HH:MM UTC`.
2. Verify via VNC that no manual positions exist (foreign-position monitor
   would also alert, but human eyes catch what magic-number checks miss).
3. **Do NOT log into MT5 with master password** during the news window. If
   you must look at the chart, use noVNC + investor password only.
4. After event clears, bot resumes auto-trading — no manual intervention
   needed.

---

## 5. Tick Engine Promotion Checklist

Before flipping `tick_engine.enabled: true` on a live account:

- [ ] `python3 scripts/measure_tick_latency.py --symbol XAUUSD --duration 600`
      passes p95 < 350ms during London session
- [ ] 14 days of demo run with tick engine show same-or-better P&L vs bar engine
- [ ] No broker `position_modify` rejections clustered in logs
- [ ] Foreign-position alert never falsely fired on bot's own ticket
- [ ] News blackout caught all events in `config/news_calendar.csv`
- [ ] Slippage logged in `tick_execution_log` table averages ≤ 0.5 pip on XAUUSD
- [ ] Master password rotated since any prior leak

---

## 6. Common Failure Modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `MT5 reconnect attempt 10/10` then exit | RPyC port closed / Wine crashed | `docker compose restart metatrader5` |
| `TickStream started: 0 symbols` | `tick_engine.symbols` empty AND `signal_generator.instruments` empty | Set one of them in account overlay YAML |
| Modify rejected `Invalid stops` | SL too close to bid/ask (broker minimum-stop) | Increase `tick_engine.min_sl_change_points` in config |
| Broker rejects `Trade is disabled` after market open | Vantage AutoTrading toggle off in MT5 terminal | Open via VNC, click the green AutoTrading button |
| `FOREIGN POSITION` alert fires on every poll | Magic mismatch — usually a stray manual trade left open | Close it via VNC investor session |
| `EMERGENCY STOP TRIGGERED` | Daily loss exceeded `max_daily_loss_pct` | Investigate trades; do NOT reset until root cause clear |

---

## 7. Useful Commands

```bash
# Live tail bot logs
docker logs -f trading-bot-v2

# Last 30 lines on EC2 from local mac
ssh ec2-host "docker logs trading-bot-v2 --tail 30"

# Inspect SQLite state
sqlite3 data/trading_bot_v2.db ".tables"
sqlite3 data/trading_bot_v2.db "SELECT * FROM trailing_stops;"
sqlite3 data/trading_bot_v2.db "SELECT * FROM partial_profit_tracking;"

# Force tick engine off (emergency)
# Edit account overlay YAML: tick_engine.enabled: false
# then: docker compose restart trading-bot
```
