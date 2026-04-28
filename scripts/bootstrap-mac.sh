#!/usr/bin/env bash
# bootstrap-mac.sh — set up trading-bot-v2 on a fresh macOS for dev / paper / backtest.
#
# Live funded trading should NOT run on a laptop (sleep / network / power risks).
# Use bootstrap-ec2.sh for production. See orchestration-plan-v2.md.
set -euo pipefail

echo "==> 1. Homebrew"
if ! command -v brew >/dev/null; then
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

echo "==> 2. Core CLI tools"
brew install python@3.12 node git jq awscli || true

echo "==> 3. Caffeine (anti-sleep, menu bar app) + Docker Desktop"
brew install --cask caffeine || true
if ! command -v docker >/dev/null; then
  echo "  Installing Docker Desktop (cask)..."
  brew install --cask docker || true
  echo "  ! Open Docker Desktop manually once to accept terms."
  echo "  ! Settings → Resources → Memory ≥ 6 GB, CPUs ≥ 4."
fi

echo "==> 4. Python venv + project install"
PY=python3.12
if ! command -v "$PY" >/dev/null; then PY=python3; fi
"$PY" -m venv ~/.venvs/trading-bot-v2
# shellcheck disable=SC1090
source ~/.venvs/trading-bot-v2/bin/activate
pip install --upgrade pip
pip install -e '.[dev]'

echo "==> 5. Claude Code CLI (same binary as EC2)"
if ! command -v claude >/dev/null; then
  npm install -g @anthropic-ai/claude-code
fi

echo "==> 6. Pre-flight directory structure"
mkdir -p \
  "$HOME/trading-bot-v2/data/bot" \
  "$HOME/trading-bot-v2/data/mt5_data" \
  "$HOME/trading-bot-v2/logs/bot"

cat <<'EOF'

==> Bootstrap complete.

Manual remaining steps:
  1. Open Caffeine from menu bar → turn ON (☕). Right-click for "Activate
     when computer starts".
  2. System Settings → Lock Screen → "Turn display off when inactive": Never.
     Plug in to power during paper / live runs.
  3. claude login    (browser OAuth flow)
  4. cp .env.example .env   (then fill in TELEGRAM_API_ID, etc.)
  5. ./scripts/run.sh up -d
  6. Open noVNC: http://localhost:8080  (default port from docker-compose.yml)
     Log MT5 in with DEMO credentials only on a laptop.

NOT recommended for live funded trading. See orchestration-plan-v2.md
"Deployment portability" — laptop is for dev/paper, EC2 is for live.

EOF
