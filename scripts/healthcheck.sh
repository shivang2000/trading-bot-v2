#!/bin/sh
# Docker healthcheck for Trading Bot V2.
# Checks that the EventBus heartbeat file was updated within 5 minutes.
# Exit 0 = healthy, Exit 1 = unhealthy (Docker will restart the container).

HEARTBEAT="/tmp/bot_heartbeat"
MAX_AGE=300  # 5 minutes in seconds

if [ ! -f "$HEARTBEAT" ]; then
    echo "UNHEALTHY: heartbeat file missing"
    exit 1
fi

# File age in seconds
FILE_AGE=$(( $(date +%s) - $(stat -c %Y "$HEARTBEAT" 2>/dev/null || stat -f %m "$HEARTBEAT" 2>/dev/null) ))

if [ "$FILE_AGE" -gt "$MAX_AGE" ]; then
    echo "UNHEALTHY: heartbeat stale (${FILE_AGE}s old, max ${MAX_AGE}s)"
    exit 1
fi

echo "HEALTHY: heartbeat ${FILE_AGE}s old"
exit 0
