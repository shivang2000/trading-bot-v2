#!/usr/bin/env bash
# Restart the bot on EC2.
# Usage: ./scripts/ec2-restart.sh <ec2-host>
set -euo pipefail

EC2_HOST="${1:?Usage: ec2-restart.sh <ec2-host>}"

ssh "${EC2_HOST}" "cd ~/trading-bot-v2 && docker compose -f docker-compose.ec2.yml restart trading-bot"
echo "Bot restarted on ${EC2_HOST}"
