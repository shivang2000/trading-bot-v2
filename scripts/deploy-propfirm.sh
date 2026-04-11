#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# FundingPips Prop Firm Bot — Quick Deploy Setup
# ═══════════════════════════════════════════════════════════════════════
# Usage: ./scripts/deploy-propfirm.sh
# This script configures the bot for your FundingPips account.
# ═══════════════════════════════════════════════════════════════════════
set -euo pipefail

cd "$(dirname "$0")/.."

echo "═══════════════════════════════════════════════════════════════"
echo "  FundingPips Prop Firm Trading Bot — Setup"
echo "═══════════════════════════════════════════════════════════════"
echo ""

# ── Account Size ──
echo "Select your FundingPips account size:"
echo "  1) \$5,000"
echo "  2) \$10,000"
echo "  3) \$25,000"
echo "  4) \$50,000"
echo "  5) \$100,000"
read -rp "Choice [1-5]: " acct_choice
case "$acct_choice" in
  2) ACCOUNT_SIZE=10000 ;;
  3) ACCOUNT_SIZE=25000 ;;
  4) ACCOUNT_SIZE=50000 ;;
  5) ACCOUNT_SIZE=100000 ;;
  *) ACCOUNT_SIZE=5000 ;;
esac

# ── Phase ──
echo ""
echo "Select your current challenge phase:"
echo "  1) Step 1 (Student)      — 8% or 10% profit target"
echo "  2) Step 2 (Practitioner) — 5% profit target"
echo "  3) Master (Funded)       — No target, live trading"
read -rp "Choice [1/2/3]: " phase_choice
case "$phase_choice" in
  2) PHASE="step2"; TARGET_PCT=5.0 ;;
  3)
    PHASE="master"; TARGET_PCT=0.0
    echo ""
    echo "  Master account rules:"
    echo "  - News trading RESTRICTED (5-min window before/after high-impact news)"
    echo "  - Max risk per trade: 2% for accounts \$50k+ (FundingPips rule)"
    echo "  - No profit target — keep losses within daily/DD limits"
    ;;
  *)
    PHASE="step1"
    echo ""
    echo "Select Step 1 profit target:"
    echo "  1) 8%  — Standard challenge (higher cost)"
    echo "  2) 10% — Discounted challenge (lower cost, higher target)"
    read -rp "Choice [1/2]: " target_choice
    case "$target_choice" in
      2) TARGET_PCT=10.0 ;;
      *) TARGET_PCT=8.0 ;;
    esac
    ;;
esac

# ── Risk Level ──
echo ""
# Master accounts on $50k+ are capped at 2% per FundingPips rules
if [ "$PHASE" = "master" ] && [ "$ACCOUNT_SIZE" -ge 50000 ]; then
  echo "Master account ≥\$50k: risk capped at 2% (FundingPips rule)"
  RISK_PCT=2.0
else
  echo "Select risk level per trade:"
  echo "  1) Conservative — 1% risk"
  echo "  2) Standard     — 2% risk [recommended]"
  if [ "$PHASE" != "master" ] && [ "$ACCOUNT_SIZE" -lt 50000 ]; then
    echo "  3) Aggressive   — 3% risk (~\$150/win, higher DD)"
  fi
  read -rp "Choice [1/2/3]: " risk_choice
  case "$risk_choice" in
    1) RISK_PCT=1.0 ;;
    3) RISK_PCT=3.0 ;;
    *) RISK_PCT=2.0 ;;
  esac
fi

# ── Update fundingpips.yaml ──
echo ""
echo "Updating config/fundingpips.yaml..."
sed -i.bak \
  -e "s/initial_balance: [0-9]*/initial_balance: $ACCOUNT_SIZE/" \
  -e "s/account_size: [0-9]*/account_size: $ACCOUNT_SIZE/" \
  -e "s/phase: \"[a-z0-9]*\"/phase: \"$PHASE\"/" \
  -e "s/profit_target_pct: [0-9.]*/profit_target_pct: $TARGET_PCT/" \
  -e "s/risk_per_trade_pct: [0-9.]*/risk_per_trade_pct: $RISK_PCT/" \
  config/fundingpips.yaml
rm -f config/fundingpips.yaml.bak

# ── Create .env if missing ──
if [ ! -f .env ]; then
  echo "Creating .env from .env.example..."
  cp .env.example .env
  echo ""
  echo "⚠  IMPORTANT: Edit .env and fill in your API keys:"
  echo "   nano .env"
  echo ""
fi

# Ensure CONFIG_OVERLAY is set
if ! grep -q "CONFIG_OVERLAY" .env 2>/dev/null; then
  echo "CONFIG_OVERLAY=fundingpips" >> .env
fi

# ── Calculate limits ──
DAILY_LIMIT=$(echo "$ACCOUNT_SIZE * 5 / 100" | bc)
DD_FLOOR=$(echo "$ACCOUNT_SIZE * 90 / 100" | bc)
PROFIT_TARGET=$(echo "$ACCOUNT_SIZE * ${TARGET_PCT%.*} / 100" | bc 2>/dev/null || echo "N/A")
BUFFER_DAILY=$(echo "$ACCOUNT_SIZE * 4 / 100" | bc)
BUFFER_DD=$(echo "$ACCOUNT_SIZE * 91 / 100" | bc)

# ── Print Summary ──
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo "  Configuration Summary"
echo "═══════════════════════════════════════════════════════════════"
echo ""
echo "  Account:        \$$ACCOUNT_SIZE"
echo "  Phase:          $PHASE"
echo "  Risk/trade:     ${RISK_PCT}%"
echo ""
echo "  ── FundingPips Breach Rules ──"
echo "  Daily loss:     \$$DAILY_LIMIT (5% — bot stops at \$$BUFFER_DAILY)"
echo "  DD floor:       \$$DD_FLOOR (10% — bot stops at \$$BUFFER_DD)"
if [ "$PHASE" != "funded" ]; then
echo "  Profit target:  \$$PROFIT_TARGET (${TARGET_PCT}%)"
fi
echo "  Min trading days: 3"
echo ""
echo "  ── Strategies Enabled ──"
echo "  EMA Pullback        — M15, all instruments (proven \$30→\$376)"
echo "  London Breakout     — M15, all instruments, 2 trades/day"
echo "  M5 MTF Momentum     — scalping, XAUUSD, +31.7%, R:R 1:1.95"
echo "  M5 Keltner Squeeze  — scalping, XAUUSD, +74.7%, R:R 1:4.00"
echo "  M5 Dual Supertrend  — scalping, XAUUSD, +4329%, R:R 1:1.92"
echo "  Claude AI Filter    — enabled (Haiku, ~\$0.01/day)"
echo ""
echo "═══════════════════════════════════════════════════════════════"
echo ""

# ── Validate config ──
echo "Validating configuration..."
CONFIG_OVERLAY=fundingpips python -c "
from src.config.loader import load_config
cfg = load_config()
pf = cfg.prop_firm
print(f'  PropFirm: enabled={pf.enabled}, phase={pf.phase}, account=\${pf.account_size:.0f}')
print(f'  Targets: profit={pf.profit_target_pct}%, daily_limit={pf.daily_loss_limit_pct}%, DD={pf.max_overall_dd_pct}%')
print(f'  Claude filter: enabled={cfg.claude_filter.enabled}')
print('  ✓ Config validated successfully')
" || { echo "✗ Config validation FAILED"; exit 1; }

echo ""
read -rp "Deploy now? (docker compose up) [y/N]: " deploy_choice
if [ "$deploy_choice" = "y" ] || [ "$deploy_choice" = "Y" ]; then
  echo "Starting deployment..."
  docker compose -f docker-compose.ec2.yml up -d
  echo ""
  echo "Bot deployed! Check logs:"
  echo "  docker compose -f docker-compose.ec2.yml logs -f trading-bot"
else
  echo ""
  echo "To deploy manually:"
  echo "  docker compose -f docker-compose.ec2.yml up -d"
fi
