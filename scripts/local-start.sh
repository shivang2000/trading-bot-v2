#!/usr/bin/env bash
# Start the trading bot locally using Docker Compose.
# Usage: ./scripts/local-start.sh
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Starting Trading Bot V2 (local)..."

# Build and start
docker compose up --build -d

echo "Bot started. View logs with: docker compose logs -f trading-bot"
