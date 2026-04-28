#!/usr/bin/env bash
# Platform-detect launcher for trading-bot-v2 + paperclip stack.
#
# Usage:
#   ./scripts/run.sh up -d       # start
#   ./scripts/run.sh logs -f     # tail logs
#   ./scripts/run.sh down        # stop
#   ./scripts/run.sh restart     # restart all
#
# On macOS  → docker-compose.yml + .macbook.override.yml + .paperclip.override.yml
# On Linux  → docker-compose.yml + .ec2.override.yml      + .paperclip.override.yml
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"

case "$(uname -s)" in
  Darwin)
    PLATFORM="macbook"
    BASE="$here/docker-compose.yml"
    OVERLAY="$here/docker-compose.macbook.override.yml"
    ;;
  Linux)
    PLATFORM="ec2"
    # docker-compose.ec2.yml is self-contained (MT5 + bot in one file)
    BASE="$here/docker-compose.ec2.yml"
    OVERLAY=""
    ;;
  *)
    echo "Unsupported platform: $(uname -s). Supported: Darwin (macOS), Linux." >&2
    exit 1
    ;;
esac

# Paperclip overlay opt-in via PAPERCLIP=1 env var (default off — Wave 1 ships
# without paperclip; users add the env when bringing up the orchestration layer).
COMPOSE_ARGS=(-f "$BASE")
[ -n "$OVERLAY" ] && [ -f "$OVERLAY" ] && COMPOSE_ARGS+=(-f "$OVERLAY")
if [ "${PAPERCLIP:-0}" = "1" ] && [ -f "$here/docker-compose.paperclip.override.yml" ]; then
  COMPOSE_ARGS+=(-f "$here/docker-compose.paperclip.override.yml" --profile paperclip)
fi

echo "» trading-bot-v2 launcher (platform=$PLATFORM, paperclip=${PAPERCLIP:-0})"

cd "$here"
exec docker compose "${COMPOSE_ARGS[@]}" "$@"
