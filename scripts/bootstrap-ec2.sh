#!/usr/bin/env bash
# bootstrap-ec2.sh — first-boot setup for trading-bot-v2 EC2 instance.
#
# Used as user-data in EC2 launch (passed via `aws ec2 run-instances --user-data`)
# OR run manually after SSH:
#     curl -fsSL <raw-url>/bootstrap-ec2.sh | sudo bash -s -- <env>
#
# What it does:
#   1. Update apt + install Docker, Compose v2, AWS CLI v2, sqlite3, jq, awscli
#   2. Create unprivileged 'bot' user with Docker group
#   3. Mount the secondary EBS volume at /data (preserved across instance termination)
#   4. Pull repo + write .env from SSM SecureStrings + cron the nightly backup
#   5. Configure UFW + fail2ban (defense in depth on top of SG)
#   6. Set up CloudWatch agent for /tmp/bot_heartbeat alarm
#
# Reads from AWS SSM Parameter Store (configured by scripts/aws-provision.sh):
#     /trading-bot-v2/${ACCOUNT_NAME}/mt5_master   (SecureString, never logged)
#     /trading-bot-v2/${ACCOUNT_NAME}/telegram_bot_token
#     /trading-bot-v2/${ACCOUNT_NAME}/slack_webhook_url
#     /trading-bot-v2/${ACCOUNT_NAME}/anthropic_api_key
#     /trading-bot-v2/${ACCOUNT_NAME}/backup_s3_bucket
#
# Required EC2 IAM role permissions (see scripts/aws-provision.sh):
#   - ssm:GetParameter on /trading-bot-v2/*
#   - s3:PutObject + s3:ListBucket on backup bucket
#
# Required env at invocation:
#   ACCOUNT_NAME      e.g. "vantage-50" or "fundingpips-5k"
#   AWS_REGION        e.g. "ap-south-1"
#   REPO_URL          e.g. "https://github.com/shivang2000/trading-bot-v2.git"
#   GIT_BRANCH        default "main"

set -euo pipefail

ACCOUNT_NAME="${ACCOUNT_NAME:?ACCOUNT_NAME required}"
AWS_REGION="${AWS_REGION:-ap-south-1}"
REPO_URL="${REPO_URL:?REPO_URL required}"
GIT_BRANCH="${GIT_BRANCH:-main}"
BOT_USER="bot"
DATA_MOUNT="/data"
DATA_DEV="/dev/nvme1n1"  # Second EBS volume; ap-south-1 t3.medium uses nvme

log() { echo "[bootstrap $(date -Iseconds)] $*"; }

# ---------- 1. System packages ----------
log "Updating apt and installing base packages..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get upgrade -y
apt-get install -y \
    ca-certificates curl gnupg lsb-release \
    git sqlite3 jq unzip ufw fail2ban cron \
    python3 python3-pip

# Docker (official repo)
log "Installing Docker..."
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
    gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | \
    tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# AWS CLI v2 (the apt one is v1, missing some commands)
if ! command -v aws &>/dev/null; then
    log "Installing AWS CLI v2..."
    cd /tmp
    curl "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o awscliv2.zip
    unzip -q awscliv2.zip
    ./aws/install
    rm -rf awscliv2.zip aws
fi

# ---------- 2. Bot user ----------
if ! id "${BOT_USER}" &>/dev/null; then
    log "Creating user ${BOT_USER}..."
    useradd -m -s /bin/bash -G docker "${BOT_USER}"
fi

# ---------- 3. Mount /data on secondary EBS volume ----------
# DeleteOnTermination=false on this volume — survives instance replacement.
# See docs/RUNBOOK.md §2.
if [[ -b "${DATA_DEV}" ]]; then
    if ! blkid "${DATA_DEV}" &>/dev/null; then
        log "Formatting ${DATA_DEV} as ext4 (first-boot only)..."
        mkfs.ext4 -L trading-data "${DATA_DEV}"
    fi
    mkdir -p "${DATA_MOUNT}"
    mount -L trading-data "${DATA_MOUNT}" || true
    if ! grep -q "trading-data" /etc/fstab; then
        echo "LABEL=trading-data ${DATA_MOUNT} ext4 defaults,nofail 0 2" >> /etc/fstab
    fi
    chown -R "${BOT_USER}:${BOT_USER}" "${DATA_MOUNT}"
else
    log "WARNING: ${DATA_DEV} not present. Did you attach the secondary EBS volume?"
fi

# ---------- 4. Pull repo + secrets ----------
log "Cloning repo as ${BOT_USER}..."
sudo -u "${BOT_USER}" bash <<EOF
set -euo pipefail
cd ~
if [[ ! -d trading-bot-v2 ]]; then
    git clone --branch ${GIT_BRANCH} ${REPO_URL} trading-bot-v2
else
    cd trading-bot-v2 && git fetch origin && git checkout ${GIT_BRANCH} && git pull
fi
EOF

REPO_DIR="/home/${BOT_USER}/trading-bot-v2"

# Symlink /data/{db,logs,ticks} into the repo so EBS persistence is automatic
sudo -u "${BOT_USER}" bash <<EOF
set -euo pipefail
mkdir -p ${DATA_MOUNT}/db ${DATA_MOUNT}/logs ${DATA_MOUNT}/ticks ${DATA_MOUNT}/mt5_data
cd ${REPO_DIR}
[[ -L data ]] || { rm -rf data; ln -s ${DATA_MOUNT}/db data; }
[[ -L logs ]] || { rm -rf logs; ln -s ${DATA_MOUNT}/logs logs; }
[[ -L mt5_data ]] || { rm -rf mt5_data; ln -s ${DATA_MOUNT}/mt5_data mt5_data; }
EOF

# Pull secrets from SSM and write .env
log "Fetching secrets from SSM..."
fetch_ssm() {
    local key="$1"
    aws ssm get-parameter \
        --name "/trading-bot-v2/${ACCOUNT_NAME}/${key}" \
        --with-decryption \
        --region "${AWS_REGION}" \
        --query 'Parameter.Value' \
        --output text 2>/dev/null || echo ""
}

MT5_MASTER="$(fetch_ssm mt5_master)"
TELEGRAM_BOT_TOKEN="$(fetch_ssm telegram_bot_token)"
SLACK_WEBHOOK_URL="$(fetch_ssm slack_webhook_url)"
ANTHROPIC_API_KEY="$(fetch_ssm anthropic_api_key)"
BACKUP_S3_BUCKET="$(fetch_ssm backup_s3_bucket)"

# Write .env (mode 600, owned by bot)
ENV_FILE="${REPO_DIR}/.env"
sudo -u "${BOT_USER}" tee "${ENV_FILE}" >/dev/null <<EOF
ACCOUNT_NAME=${ACCOUNT_NAME}
MT5_HOST=metatrader5
MT5_PORT=8001
MT5_PASSWORD=${MT5_MASTER}
TELEGRAM_BOT_TOKEN=${TELEGRAM_BOT_TOKEN}
SLACK_WEBHOOK_URL=${SLACK_WEBHOOK_URL}
ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}
BACKUP_S3_BUCKET=${BACKUP_S3_BUCKET}
AWS_REGION=${AWS_REGION}
EOF
chmod 600 "${ENV_FILE}"
chown "${BOT_USER}:${BOT_USER}" "${ENV_FILE}"

# ---------- 5. Cron: nightly backup ----------
log "Wiring nightly-backup cron..."
CRON_LINE="0 23 * * * BACKUP_S3_BUCKET=${BACKUP_S3_BUCKET} ${REPO_DIR}/scripts/nightly-backup.sh >> ${REPO_DIR}/logs/backup.log 2>&1"
sudo -u "${BOT_USER}" bash -c "(crontab -l 2>/dev/null | grep -v 'nightly-backup.sh' ; echo '${CRON_LINE}') | crontab -"

# ---------- 6. Firewall + fail2ban ----------
log "Configuring UFW (deny by default, allow only SSH)..."
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH'
# noVNC + RPyC are localhost-only — Docker port-mapped to 0.0.0.0 by default.
# Override via SG rules; leave UFW closed.
ufw --force enable

systemctl enable --now fail2ban
systemctl enable --now docker

# ---------- 7. CloudWatch agent (optional, only if role has perms) ----------
log "Installing CloudWatch agent..."
curl -fsSL "https://s3.${AWS_REGION}.amazonaws.com/amazoncloudwatch-agent-${AWS_REGION}/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb" \
    -o /tmp/amazon-cloudwatch-agent.deb && \
    dpkg -i /tmp/amazon-cloudwatch-agent.deb || log "CloudWatch agent install failed (non-fatal)"

cat >/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<'EOF'
{
  "agent": { "metrics_collection_interval": 60 },
  "metrics": {
    "namespace": "TradingBotV2",
    "metrics_collected": {
      "mem":   { "measurement": ["mem_used_percent"] },
      "disk":  { "measurement": ["used_percent"], "resources": ["/data"] },
      "cpu":   { "measurement": ["usage_idle", "usage_iowait"], "totalcpu": true }
    }
  },
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          { "file_path": "/home/bot/trading-bot-v2/logs/trading.log",
            "log_group_name": "trading-bot-v2",
            "log_stream_name": "{instance_id}-trading" }
        ]
      }
    }
  }
}
EOF
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config -m ec2 -s -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json \
    || log "CloudWatch start failed (non-fatal)"

# ---------- 8. Pull docker images so first start is fast ----------
log "Pre-pulling docker images..."
sudo -u "${BOT_USER}" bash <<EOF
cd ${REPO_DIR}
docker compose -f docker-compose.ec2.yml pull || true
EOF

log "Bootstrap complete. To start the bot:"
log "    sudo -u ${BOT_USER} bash -c 'cd ${REPO_DIR} && docker compose -f docker-compose.ec2.yml up -d'"
log "Then open http://<EIP>:8080 in browser (VNC pwd 'botpass') to log MT5 in once,"
log "then restart trading-bot:  docker compose -f docker-compose.ec2.yml restart trading-bot"
