#!/usr/bin/env bash
# Check bot status on EC2.
# Usage: ./scripts/ec2-status.sh <ec2-host>
set -euo pipefail

EC2_HOST="${1:?Usage: ec2-status.sh <ec2-host>}"

ssh "${EC2_HOST}" "cd ~/trading-bot-v2 && docker compose -f docker-compose.ec2.yml ps"
