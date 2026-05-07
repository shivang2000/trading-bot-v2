# Copy-Trader Runbook

Operational playbook for `copy-trader-bot` — mirrors trades from Vantage
Account A (source, traded by a human partner) to Vantage Account B (dest,
your other account) on the same broker.

Pairs with: `docs/superpowers/specs/2026-05-08-copy-trader-design.md`
(design rationale) and the existing `docs/RUNBOOK.md` (which covers the
unrelated trading-bot-v2 stack).

> **Hard rule:** Account A and Account B run NO existing trading-bot
> strategies. They are pure mirror-only. The bot in
> `docker-compose.ec2.yml` and the bot in `docker-compose.copytrader.yml`
> are independent stacks — different containers, different MT5 logins,
> different SQLite DBs.

---

## 1. First-time setup

### 1.1 Prepare environment

Create `.env` in repo root with:

```bash
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
MT5_SOURCE_LOGIN=12345678   # Vantage Account A login number
MT5_DEST_LOGIN=87654321     # Vantage Account B login number
MT5_VNC_PASSWORD=botpass    # noVNC password (default fine)
```

The bot uses `MT5_*_LOGIN` only as a sanity guard — it does NOT log in
on your behalf. You log in interactively via noVNC.

### 1.2 Start the stack

```bash
./scripts/start-copytrader.sh
```

Containers come up in 30s.

### 1.3 Log MT5 in (one-time per container)

You must do this once for each MT5 container — credentials persist in
the volume mount across restarts.

**For Account A (source):**

1. Open http://&lt;host&gt;:8081 in browser
2. VNC password: `botpass` (or `MT5_VNC_PASSWORD` value)
3. Inside MT5 Wine:
   - File → Login to Trade Account
   - Login: `<Vantage Account A number>`
   - Password: `<master password for A>`
   - Server: `VantageInternational-Live` (or `-Demo`)
4. Click **AutoTrading** green button (top toolbar) — required even
   though we only READ from this account
5. Close noVNC tab — MT5 stays running

**For Account B (dest):** repeat steps with http://&lt;host&gt;:8082 and
Account B credentials.

### 1.4 Restart copy-trader-bot to pick up logged-in MT5s

```bash
docker compose -f docker-compose.copytrader.yml restart copy-trader-bot
./scripts/start-copytrader.sh logs
```

Look for in logs:

```
[source] connected
[dest] connected
Boot snapshot: N source positions ignored (pre-existing)
copy-trader-bot started (poll=100ms)
```

If `[source] connecting` retries forever — MT5 inside the container
isn't logged in. Re-do step 1.3.

---

## 2. Daily operation

The trader works on Account A. Bot mirrors trades onto Account B
automatically. You don't intervene unless an alert fires.

### Watch live activity

```bash
./scripts/start-copytrader.sh logs
```

Or via Slack — every mirror open/close/modify posts to your `[copy-trader]` channel.

### Check stats

```bash
sqlite3 data/mirror_journal.db "
  SELECT event_type, COUNT(*), AVG(latency_ms), MAX(latency_ms)
  FROM mirror_events
  WHERE ts > datetime('now', '-1 day')
  GROUP BY event_type;
"
```

Expected p95 latency: 200-500ms. p99 < 1000ms.

---

## 3. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `MT5 connect attempt N/12` loops | MT5 inside container not logged in | noVNC step 1.3 |
| `MIRROR OPEN failed: margin too low` Slack | Dest B doesn't have enough margin | Top up B or stop trading on A until you do |
| `MIRROR CLOSE failed: position not found` | Dest position already closed (manually or by SL/TP hit) | Bot logs error + drops mapping. Source A still tracked normally on next cycle |
| Source closed at 14:00, dest stays open | Bot was down between 14:00 and 14:01 | On bot restart, reconciliation removes orphaned mirror — but dest position will need manual close OR will be force-closed on next cycle if source is still gone |
| Slack spammed during scalping | Bot is mirroring fast trades | Set `slack.enabled: false` temporarily, or tune to errors-only by editing notifier |
| Both MT5 containers OOM | Wine runtime grew | `docker compose restart mt5-source mt5-dest` |

### Source RPyC dropped > 30s

Bot logs `MT5 reconnect attempt N/10` and posts Slack error. Existing
exponential backoff handles transient drops. If it persists:

1. `docker logs mt5-source` — check Wine state
2. `docker compose restart mt5-source`
3. Re-login if VNC shows MT5 lost session

### Bot crashed mid-trade

Bot is configured with `restart: unless-stopped`. On restart:

1. Reads `mirror_map` from journal — knows which dest tickets correspond
   to which source tickets
2. Cycles: detects positions on source not in map → mirrors them; detects
   map entries no longer on source → closes corresponding dest tickets

Risk window: any trade A opened DURING the crash that A then closed
BEFORE bot restarted = silent loss of mirror. The journal will not have
recorded it. Mitigation: keep restart-time as low as possible (`restart:
unless-stopped` brings it back in seconds).

---

## 4. Recovery scenarios

### EC2/host died

1. Provision new host
2. Restore `data/mirror_journal.db` from S3 backup (per nightly-backup.sh)
3. Restore `mt5_data_source/`, `mt5_data_dest/` (Wine state — has saved
   MT5 logins). If lost, re-login via noVNC manually.
4. `./scripts/start-copytrader.sh`
5. On boot, journal restores mirror_map; bot reconciles open positions

### Source account is wrong / tracking the wrong trader

1. `docker compose -f docker-compose.copytrader.yml stop copy-trader-bot`
2. Open noVNC for source (port 8081), log out, log into correct account
3. Wipe journal: `mv data/mirror_journal.db data/mirror_journal.db.bak`
   (so previous mappings don't carry over)
4. `docker compose -f docker-compose.copytrader.yml start copy-trader-bot`
5. New boot snapshot = new ignored set; mirroring starts fresh

### Dest account locked out / blown up

1. Bot will fail every `OPEN` with `not enough margin` or
   `trade is disabled`
2. Slack will spam — silence by `slack.enabled: false` or stopping the
   stack: `./scripts/start-copytrader.sh stop`
3. After fixing dest account, restart. Existing mappings stay tracked
   (source positions A still has open will mirror onto B again on next
   cycle's reconciliation… but only if mapping was preserved — usually
   broken ones get cleaned).

---

## 5. Promotion checklist (demo → live)

Before flipping from Vantage demo to real money:

- [ ] 14 days continuous demo run with both accounts on Vantage demo
- [ ] `mirror_journal.db` shows zero `success=0` events (or you've
      diagnosed each one)
- [ ] Latency p95 < 500ms (query: see §2 stats command)
- [ ] Restart-recovery tested at least once: `docker compose down`,
      manually open trade on A, `docker compose up`, verify A trade is
      ignored (was open before bot started) — and that subsequent A
      trades mirror correctly
- [ ] Slack alerts working: open/close/modify each visible in channel
- [ ] Master passwords for both accounts in 1Password / SSM (not in
      `.env` committed to git)
- [ ] Hetzner / EC2 host has DeleteOnTermination=false on data volume
      (per existing RUNBOOK §2)
- [ ] Tiny live first: $30 + $30 for 7 days, P&L deltas A vs B within
      slippage (target ±$2 per trade)

---

## 6. Tear-down

```bash
./scripts/start-copytrader.sh stop
docker compose -f docker-compose.copytrader.yml down -v   # also wipes mt5_data volumes
rm -rf data/mirror_journal.db
```

This leaves the existing `trading-bot-v2` stack untouched.
