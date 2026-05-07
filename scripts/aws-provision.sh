#!/usr/bin/env bash
# aws-provision.sh — one-shot AWS infra provisioner for trading-bot-v2.
#
# Creates everything needed for a fresh deploy:
#   1. S3 bucket for nightly backups (versioning + lifecycle)
#   2. SSM SecureString parameters (you fill in the values interactively)
#   3. IAM role + instance profile (SSM read + S3 write)
#   4. Security group (SSH from your IP + outbound all)
#   5. SSH keypair imported from ~/.ssh/id_ed25519.pub
#   6. EBS volume (DeleteOnTermination=false — survives instance churn)
#   7. EC2 instance (t3.medium, Ubuntu 24.04 LTS) with bootstrap user-data
#   8. (no Elastic IP — uses auto-assigned public IP; saves ~$4/mo)
#
# Usage:
#     export AWS_REGION=ap-south-1
#     export ACCOUNT_NAME=vantage-50
#     export REPO_URL=https://github.com/shivang2000/trading-bot-v2.git
#     ./scripts/aws-provision.sh
#
# Output: prints EIP and SSH command at end.
#
# Cost estimate (ap-south-1, May 2026):
#   t3.medium             $35/mo
#   60 GB gp3 EBS         $5
#   8 GB root EBS         $0.65
#   Auto-assigned IP      $0   (charged only when instance stopped >1h)
#   S3 backups (~10GB)    $0.25
#   CloudWatch metrics    $1
#   SSM parameters (free) $0
#   ─────────────────────────
#   TOTAL                 ~$42/mo  → $100 credits ≈ 2.4 months runway

set -euo pipefail

AWS_REGION="${AWS_REGION:?AWS_REGION required (e.g. ap-south-1)}"
ACCOUNT_NAME="${ACCOUNT_NAME:?ACCOUNT_NAME required (e.g. vantage-50)}"
REPO_URL="${REPO_URL:?REPO_URL required}"
GIT_BRANCH="${GIT_BRANCH:-main}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.medium}"
DATA_VOLUME_GB="${DATA_VOLUME_GB:-60}"
KEY_NAME="${KEY_NAME:-trading-bot-v2-key}"
PUBKEY_PATH="${PUBKEY_PATH:-${HOME}/.ssh/id_ed25519.pub}"

PROJECT_TAG="trading-bot-v2"
TAG_SPEC="ResourceType={resource_type},Tags=[{Key=Project,Value=${PROJECT_TAG}},{Key=Account,Value=${ACCOUNT_NAME}}]"

# ---------- Logging + state tracking ----------
LOG_DIR="${LOG_DIR:-/tmp}"
LOG_FILE="${LOG_DIR}/aws-provision-${ACCOUNT_NAME}-$(date -u +%Y%m%dT%H%M%SZ).log"
STATE_FILE="${LOG_DIR}/aws-provision-${ACCOUNT_NAME}.state"

# Tee everything (stdout + stderr) to LOG_FILE while preserving terminal output
exec > >(tee -a "${LOG_FILE}") 2>&1

aws_cli() { aws --region "${AWS_REGION}" "$@"; }
acct_id() { aws_cli sts get-caller-identity --query Account --output text; }

log()   { echo "[provision $(date -Iseconds)] $*"; }
ok()    { echo "[ok    $(date -Iseconds)] $*"; }
warn()  { echo "[warn  $(date -Iseconds)] $*"; }
fatal() { echo "[FATAL $(date -Iseconds)] $*"; exit 1; }

# Persist created resource IDs so we can cleanup on failure or rerun idempotently
record_resource() {
    # Usage: record_resource <type> <id>   e.g. record_resource bucket trading-bot-v2-vantage-50-123
    local type="$1"; local id="$2"
    echo "$(date -Iseconds)|${type}|${id}" >> "${STATE_FILE}"
}

# Trap any error: dump state file + last 30 lines of log so user can debug/cleanup
on_error() {
    local exit_code=$?
    local line="${BASH_LINENO[0]}"
    echo
    echo "============================================================"
    echo "  PROVISION FAILED at line ${line} (exit ${exit_code})"
    echo "============================================================"
    echo "Resources created so far (see ${STATE_FILE} for full list):"
    if [[ -f "${STATE_FILE}" ]]; then
        column -t -s '|' "${STATE_FILE}" | tail -30
    else
        echo "  (none recorded)"
    fi
    echo
    echo "Full log: ${LOG_FILE}"
    echo
    echo "To clean up:"
    echo "  See docs/AWS_DEPLOYMENT.md §10 for tear-down commands."
    echo "  Or rerun this script — most steps are idempotent and will skip"
    echo "  resources that already exist."
    exit "${exit_code}"
}
trap on_error ERR

log "Provision started — log: ${LOG_FILE}"
log "  region:        ${AWS_REGION}"
log "  account_name:  ${ACCOUNT_NAME}"
log "  instance_type: ${INSTANCE_TYPE}"
log "  data_volume:   ${DATA_VOLUME_GB} GB gp3"
log "  repo:          ${REPO_URL} (${GIT_BRANCH})"
log "  state file:    ${STATE_FILE}"

# ---------- 0. Verify CLI works ----------
log "Verifying AWS CLI configured for region ${AWS_REGION}..."
ACCOUNT_ID="$(acct_id)"
ok "Logged in to AWS account ${ACCOUNT_ID}"

# ---------- 1. S3 backup bucket ----------
BUCKET_NAME="trading-bot-v2-${ACCOUNT_NAME}-${ACCOUNT_ID}"
log "Creating S3 bucket s3://${BUCKET_NAME}..."
if aws_cli s3api head-bucket --bucket "${BUCKET_NAME}" 2>/dev/null; then
    ok "Bucket already exists"
else
    if [[ "${AWS_REGION}" == "us-east-1" ]]; then
        aws_cli s3api create-bucket --bucket "${BUCKET_NAME}"
    else
        aws_cli s3api create-bucket --bucket "${BUCKET_NAME}" \
            --create-bucket-configuration LocationConstraint="${AWS_REGION}"
    fi
fi
aws_cli s3api put-bucket-versioning --bucket "${BUCKET_NAME}" \
    --versioning-configuration Status=Enabled
# 30-day retention via lifecycle policy
aws_cli s3api put-bucket-lifecycle-configuration --bucket "${BUCKET_NAME}" \
    --lifecycle-configuration '{
        "Rules": [{
            "ID": "expire-30d",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "Expiration": {"Days": 30},
            "NoncurrentVersionExpiration": {"NoncurrentDays": 30}
        }]
    }'
aws_cli s3api put-public-access-block --bucket "${BUCKET_NAME}" \
    --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
record_resource bucket "${BUCKET_NAME}"
ok "Bucket configured (versioning, 30d lifecycle, all-public blocked)"

# ---------- 2. SSM SecureString parameters ----------
log "Creating SSM parameters (skip if exists)..."
upsert_secure_param() {
    # Args: <ssm-suffix> <prompt> <env-var-name>
    # Source order: env var → existing SSM → interactive prompt → skip
    local name="$1"; local prompt="$2"; local env_var="$3"
    local full_name="/trading-bot-v2/${ACCOUNT_NAME}/${name}"
    local val=""

    if aws_cli ssm get-parameter --name "${full_name}" --with-decryption --query Parameter.Value --output text >/dev/null 2>&1; then
        ok "  ${full_name} already exists in SSM"
        return
    fi

    # Env var override — preferred for non-interactive runs (CI, scripted, this provisioner from inside Claude)
    if [[ -n "${!env_var:-}" ]]; then
        val="${!env_var}"
        aws_cli ssm put-parameter --name "${full_name}" --type SecureString --value "${val}" >/dev/null
        ok "  ${full_name} written from \$${env_var}"
        return
    fi

    # Interactive prompt — only if stdin is a tty
    if [[ -t 0 ]]; then
        echo -n "Enter ${prompt} (or empty to skip): "
        read -rs val || val=""
        echo
        if [[ -z "${val}" ]]; then
            warn "  ${full_name} skipped (empty input)"
            return
        fi
        aws_cli ssm put-parameter --name "${full_name}" --type SecureString --value "${val}" >/dev/null
        ok "  ${full_name} written"
    else
        warn "  ${full_name} skipped (no tty + no \$${env_var}); set later with: aws ssm put-parameter --region ${AWS_REGION} --name ${full_name} --type SecureString --value '...'"
    fi
}
upsert_plain_param() {
    local name="$1"; local val="$2"
    local full_name="/trading-bot-v2/${ACCOUNT_NAME}/${name}"
    aws_cli ssm put-parameter --name "${full_name}" --type String --value "${val}" --overwrite
    ok "  ${full_name} = ${val}"
}

upsert_secure_param "mt5_master"           "MT5 master password (Vantage portal)" MT5_MASTER
upsert_secure_param "telegram_bot_token"   "Telegram bot token (BotFather)"        TELEGRAM_BOT_TOKEN
upsert_secure_param "slack_webhook_url"    "Slack webhook URL"                     SLACK_WEBHOOK_URL
upsert_secure_param "anthropic_api_key"    "Anthropic API key (sk-ant-...)"        ANTHROPIC_API_KEY
upsert_plain_param  "backup_s3_bucket"     "s3://${BUCKET_NAME}"

# ---------- 3. IAM role + instance profile ----------
ROLE_NAME="${PROJECT_TAG}-${ACCOUNT_NAME}-role"
INSTANCE_PROFILE_NAME="${ROLE_NAME}-instance-profile"

if ! aws_cli iam get-role --role-name "${ROLE_NAME}" >/dev/null 2>&1; then
    log "Creating IAM role ${ROLE_NAME}..."
    aws_cli iam create-role --role-name "${ROLE_NAME}" \
        --assume-role-policy-document '{
            "Version": "2012-10-17",
            "Statement": [{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]
        }'
    record_resource iam-role "${ROLE_NAME}"
fi

# Inline policy: SSM read + S3 write + CloudWatch put + EC2 describe (for self-introspection)
aws_cli iam put-role-policy --role-name "${ROLE_NAME}" --policy-name trading-bot-v2-policy \
    --policy-document '{
        "Version": "2012-10-17",
        "Statement": [
            {"Effect":"Allow","Action":["ssm:GetParameter","ssm:GetParameters","ssm:GetParametersByPath"],
             "Resource":"arn:aws:ssm:'"${AWS_REGION}"':'"${ACCOUNT_ID}"':parameter/trading-bot-v2/'"${ACCOUNT_NAME}"'/*"},
            {"Effect":"Allow","Action":["s3:PutObject","s3:GetObject","s3:ListBucket","s3:DeleteObject"],
             "Resource":["arn:aws:s3:::'"${BUCKET_NAME}"'","arn:aws:s3:::'"${BUCKET_NAME}"'/*"]},
            {"Effect":"Allow","Action":["cloudwatch:PutMetricData","logs:CreateLogGroup","logs:CreateLogStream","logs:PutLogEvents","logs:DescribeLogStreams"],
             "Resource":"*"},
            {"Effect":"Allow","Action":["ec2:DescribeInstances","ec2:DescribeTags","ec2:DescribeVolumes"],
             "Resource":"*"}
        ]
    }'
ok "IAM policy attached"

if ! aws_cli iam get-instance-profile --instance-profile-name "${INSTANCE_PROFILE_NAME}" >/dev/null 2>&1; then
    aws_cli iam create-instance-profile --instance-profile-name "${INSTANCE_PROFILE_NAME}"
    aws_cli iam add-role-to-instance-profile --instance-profile-name "${INSTANCE_PROFILE_NAME}" --role-name "${ROLE_NAME}"
    record_resource iam-instance-profile "${INSTANCE_PROFILE_NAME}"
    ok "Instance profile created"
    sleep 10  # IAM eventual consistency
fi

# ---------- 4. Security group ----------
SG_NAME="${PROJECT_TAG}-${ACCOUNT_NAME}-sg"
VPC_ID="$(aws_cli ec2 describe-vpcs --filters "Name=is-default,Values=true" --query 'Vpcs[0].VpcId' --output text)"
if [[ "${VPC_ID}" == "None" ]]; then
    log "ERROR: No default VPC in ${AWS_REGION}. Create one or specify --vpc-id." >&2
    exit 1
fi

SG_ID="$(aws_cli ec2 describe-security-groups --filters "Name=group-name,Values=${SG_NAME}" "Name=vpc-id,Values=${VPC_ID}" --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")"
if [[ "${SG_ID}" == "None" ]]; then
    SG_ID="$(aws_cli ec2 create-security-group --group-name "${SG_NAME}" --description "trading-bot-v2 ${ACCOUNT_NAME}" --vpc-id "${VPC_ID}" --query GroupId --output text)"
    record_resource security-group "${SG_ID}"
    ok "Security group created: ${SG_ID}"
fi

# Allow SSH from your current public IP
MY_IP="$(curl -fsSL https://checkip.amazonaws.com 2>/dev/null | tr -d '[:space:]' || true)"
if [[ -z "${MY_IP}" ]]; then
    log "WARN: Couldn't detect your public IP; SG will allow SSH from 0.0.0.0/0 (less safe)"
    MY_IP_CIDR="0.0.0.0/0"
else
    MY_IP_CIDR="${MY_IP}/32"
fi
aws_cli ec2 authorize-security-group-ingress --group-id "${SG_ID}" --protocol tcp --port 22 --cidr "${MY_IP_CIDR}" 2>/dev/null || true
# noVNC web (8080) for MT5 first-time setup — also from your IP only
aws_cli ec2 authorize-security-group-ingress --group-id "${SG_ID}" --protocol tcp --port 8080 --cidr "${MY_IP_CIDR}" 2>/dev/null || true
ok "SG ingress: SSH (22) + noVNC (8080) from ${MY_IP_CIDR}"

# ---------- 5. SSH keypair ----------
if ! aws_cli ec2 describe-key-pairs --key-names "${KEY_NAME}" >/dev/null 2>&1; then
    if [[ ! -f "${PUBKEY_PATH}" ]]; then
        log "ERROR: ${PUBKEY_PATH} not found. Generate with: ssh-keygen -t ed25519" >&2
        exit 1
    fi
    aws_cli ec2 import-key-pair --key-name "${KEY_NAME}" --public-key-material "fileb://${PUBKEY_PATH}"
    record_resource keypair "${KEY_NAME}"
    ok "Imported keypair ${KEY_NAME} from ${PUBKEY_PATH}"
fi

# ---------- 6. Lookup latest Ubuntu 24.04 LTS AMI ----------
AMI_ID="$(aws_cli ec2 describe-images --owners 099720109477 \
    --filters "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*" \
              "Name=state,Values=available" \
    --query "sort_by(Images, &CreationDate)[-1].ImageId" --output text)"
ok "Ubuntu 24.04 LTS AMI: ${AMI_ID}"

# ---------- 7. EBS data volume (DeleteOnTermination=false implicitly: separate volume) ----------
AZ="$(aws_cli ec2 describe-availability-zones --query 'AvailabilityZones[0].ZoneName' --output text)"
EXISTING_DATA_VOL="$(aws_cli ec2 describe-volumes \
    --filters "Name=tag:Project,Values=${PROJECT_TAG}" "Name=tag:Account,Values=${ACCOUNT_NAME}" "Name=tag:Role,Values=data" \
    --query 'Volumes[0].VolumeId' --output text 2>/dev/null || echo None)"
if [[ "${EXISTING_DATA_VOL}" == "None" ]]; then
    DATA_VOL="$(aws_cli ec2 create-volume \
        --availability-zone "${AZ}" --size "${DATA_VOLUME_GB}" --volume-type gp3 \
        --tag-specifications "ResourceType=volume,Tags=[{Key=Project,Value=${PROJECT_TAG}},{Key=Account,Value=${ACCOUNT_NAME}},{Key=Role,Value=data},{Key=Name,Value=trading-data}]" \
        --query VolumeId --output text)"
    record_resource ebs-volume "${DATA_VOL}"
    ok "Created data EBS volume ${DATA_VOL} (${DATA_VOLUME_GB} GB gp3, AZ ${AZ})"
else
    DATA_VOL="${EXISTING_DATA_VOL}"
    ok "Reusing existing data volume ${DATA_VOL}"
fi

# ---------- 8. Launch EC2 instance ----------
USER_DATA="$(cat <<EOF
#!/usr/bin/env bash
set -euxo pipefail

# Wait briefly for cloud-init network
sleep 10

# Mount the secondary EBS volume (attached as nvme1n1 by AWS)
ACCOUNT_NAME="${ACCOUNT_NAME}"
AWS_REGION="${AWS_REGION}"
REPO_URL="${REPO_URL}"
GIT_BRANCH="${GIT_BRANCH}"
export ACCOUNT_NAME AWS_REGION REPO_URL GIT_BRANCH

# Pull and run bootstrap-ec2.sh from repo
cd /tmp
apt-get update -y && apt-get install -y git
git clone --depth 1 --branch \${GIT_BRANCH} \${REPO_URL} bootstrap-clone
bash bootstrap-clone/scripts/bootstrap-ec2.sh
EOF
)"

INSTANCE_ID="$(aws_cli ec2 run-instances \
    --image-id "${AMI_ID}" \
    --instance-type "${INSTANCE_TYPE}" \
    --key-name "${KEY_NAME}" \
    --security-group-ids "${SG_ID}" \
    --iam-instance-profile "Name=${INSTANCE_PROFILE_NAME}" \
    --block-device-mappings "[{\"DeviceName\":\"/dev/sda1\",\"Ebs\":{\"VolumeSize\":8,\"VolumeType\":\"gp3\",\"DeleteOnTermination\":true}}]" \
    --placement "AvailabilityZone=${AZ}" \
    --user-data "${USER_DATA}" \
    --tag-specifications \
        "ResourceType=instance,Tags=[{Key=Project,Value=${PROJECT_TAG}},{Key=Account,Value=${ACCOUNT_NAME}},{Key=Name,Value=trading-bot-v2-${ACCOUNT_NAME}}]" \
    --query 'Instances[0].InstanceId' --output text)"
record_resource ec2-instance "${INSTANCE_ID}"
ok "Launched instance ${INSTANCE_ID}"

log "Waiting for instance to enter 'running' state..."
aws_cli ec2 wait instance-running --instance-ids "${INSTANCE_ID}"

# Attach data volume (DeleteOnTermination=false because it's a separate, pre-existing volume)
aws_cli ec2 attach-volume --volume-id "${DATA_VOL}" --instance-id "${INSTANCE_ID}" --device "/dev/sdf"
ok "Attached data volume ${DATA_VOL} to ${INSTANCE_ID}"

# ---------- 9. Public IP (auto-assigned, NOT Elastic IP) ----------
# Auto-assigned IP costs nothing while instance is running. Caveat: changes
# on stop/start. For 24/7 runtime with `restart: unless-stopped` containers
# this is fine — IP only changes if you fully stop+start the instance.
EIP="$(aws_cli ec2 describe-instances --instance-ids "${INSTANCE_ID}" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"
if [[ "${EIP}" == "None" || -z "${EIP}" ]]; then
    fatal "Instance launched but no public IP assigned. Check VPC settings (subnet must enable auto-assign public IP)."
fi
ok "Public IP ${EIP} (auto-assigned; will change on stop/start)"

# ---------- 10. Output summary ----------
cat <<SUMMARY

============================================================
  AWS provisioning complete for account: ${ACCOUNT_NAME}
============================================================
  Region:           ${AWS_REGION}
  Instance:         ${INSTANCE_ID} (${INSTANCE_TYPE})
  Public IP:        ${EIP}  (auto-assigned; re-check after instance stop/start)
  Data volume:      ${DATA_VOL} (${DATA_VOLUME_GB} GB, persists across instance termination)
  S3 backups:       s3://${BUCKET_NAME}
  IAM role:         ${ROLE_NAME}
  Security group:   ${SG_ID} (SSH/noVNC from ${MY_IP_CIDR})

  SSH:              ssh -i ${PUBKEY_PATH%.pub} ubuntu@${EIP}
  noVNC (MT5):      http://${EIP}:8080  (pwd: botpass)

  First-boot bootstrap is running via user-data (~5 min).
  Tail it with:     ssh ubuntu@${EIP} 'sudo tail -f /var/log/cloud-init-output.log'

  After bootstrap completes:
    1. SSH in
    2. Open noVNC, log into MT5 with master password from SSM
    3. Enable AutoTrading button in MT5
    4. sudo -u bot bash -c 'cd /home/bot/trading-bot-v2 && docker compose -f docker-compose.ec2.yml restart trading-bot'
    5. Verify:  docker logs -f trading-bot-v2

  RUNBOOK §3 covers recovery from termination — data volume survives.

  Logs:             ${LOG_FILE}
  Resources:        ${STATE_FILE}   (use for tear-down per AWS_DEPLOYMENT.md §10)
============================================================
SUMMARY

log "Provision succeeded. Resource state saved to ${STATE_FILE}"
log "Full log: ${LOG_FILE}"
