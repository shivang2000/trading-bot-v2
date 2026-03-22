#!/usr/bin/env bash
# Tail bot logs on EC2.
# Usage: ./scripts/ec2-logs.sh <ec2-host> [lines]
set -euo pipefail

EC2_HOST="${1:?Usage: ec2-logs.sh <ec2-host> [lines]}"
LINES="${2:-100}"

ssh "${EC2_HOST}" "cd ~/trading-bot-v2 && docker compose -f docker-compose.ec2.yml logs --tail=${LINES} -f trading-bot"
