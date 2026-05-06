#!/usr/bin/env bash
# Nightly backup of SQLite trade DB + log files to S3.
#
# Cron entry on EC2:
#   0 23 * * * /home/ec2-user/trading-bot-v2/scripts/nightly-backup.sh >> /home/ec2-user/trading-bot-v2/logs/backup.log 2>&1
#
# Required env (in /etc/environment or shell profile):
#   BACKUP_S3_BUCKET  — destination bucket (e.g. s3://my-trading-backups)
#   BACKUP_RETENTION_DAYS — int, default 30
#
# This is the operational counterpart to docs/post-mortem-5k.md — when the
# next EC2 dies (terminates, EBS detached, region eviction), the bot can
# restart anywhere with this snapshot and resume trailing stops, partial
# profit state, and session_start_equity baseline.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -z "${BACKUP_S3_BUCKET:-}" ]]; then
    echo "[$(date -Iseconds)] BACKUP_S3_BUCKET not set — skipping" >&2
    exit 1
fi

DATE_TAG="$(date -u +%Y-%m-%d)"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"

echo "[$(date -Iseconds)] Starting backup → ${BACKUP_S3_BUCKET}/${DATE_TAG}/"

# 1. Snapshot the SQLite DB to a temp file using the online-backup API
# (regular cp can race with an active writer and corrupt the snapshot).
TMP_DB="/tmp/trading_bot_v2.db.${DATE_TAG}"
sqlite3 data/trading_bot_v2.db ".backup ${TMP_DB}"

# 2. Push DB + logs to S3 with date-prefixed key
aws s3 cp "${TMP_DB}" "${BACKUP_S3_BUCKET}/${DATE_TAG}/trading_bot_v2.db"
aws s3 sync logs/ "${BACKUP_S3_BUCKET}/${DATE_TAG}/logs/" \
    --exclude "*.tmp" --exclude "*.lock"

rm -f "${TMP_DB}"

# 3. Prune older snapshots beyond retention
CUTOFF="$(date -u -d "${RETENTION_DAYS} days ago" +%Y-%m-%d 2>/dev/null \
    || date -u -v-"${RETENTION_DAYS}d" +%Y-%m-%d)"
aws s3 ls "${BACKUP_S3_BUCKET}/" | awk '{print $2}' | sed 's:/$::' | while read -r dir; do
    if [[ "$dir" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ && "$dir" < "$CUTOFF" ]]; then
        echo "Pruning old snapshot: $dir"
        aws s3 rm --recursive "${BACKUP_S3_BUCKET}/${dir}/"
    fi
done

echo "[$(date -Iseconds)] Backup complete"
