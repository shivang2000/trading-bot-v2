#!/usr/bin/env bash
# start-copytrader.sh — bring up the copy-trader stack and tail logs.
#
# Usage:
#   ./scripts/start-copytrader.sh        # start
#   ./scripts/start-copytrader.sh stop   # stop + remove containers
#   ./scripts/start-copytrader.sh logs   # tail copy-trader-bot logs

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

CMD="${1:-start}"
COMPOSE="docker compose -f docker-compose.copytrader.yml"

case "$CMD" in
  start)
    if [[ ! -f .env ]]; then
        echo "Warning: .env not found. SLACK_WEBHOOK_URL etc. won't be set." >&2
    fi
    $COMPOSE up -d
    echo
    echo "Stack started. Next steps:"
    echo "  1. Open http://localhost:8081 (noVNC for SOURCE) → log into Vantage A → click AutoTrading"
    echo "  2. Open http://localhost:8082 (noVNC for DEST)   → log into Vantage B → click AutoTrading"
    echo "  3. docker compose -f docker-compose.copytrader.yml restart copy-trader-bot"
    echo "  4. tail logs: $0 logs"
    ;;
  stop)
    $COMPOSE down
    ;;
  logs)
    $COMPOSE logs -f copy-trader-bot
    ;;
  ps|status)
    $COMPOSE ps
    ;;
  *)
    echo "Usage: $0 {start|stop|logs|ps}" >&2
    exit 2
    ;;
esac
