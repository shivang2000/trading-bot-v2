"""Prop Firm Risk Guard — enforces funded account trading rules.

Designed for FundingPips 2-Step evaluation:
- 5% daily loss limit (hard breach = account closure)
- 10% max overall drawdown (static floor)
- Phase-aware profit targets
- Friday auto-close for master accounts
- Directional exposure limits
- Trade idea grouping (master: 3% max per trade idea)
- Drawdown recovery tiers
- Profit target auto-stop
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


@dataclass
class PropFirmConfig:
    """Configuration for prop firm trading rules."""

    account_size: float = 5000.0
    phase: str = "step1"  # step1, step2, master
    daily_loss_limit_pct: float = 5.0
    max_overall_dd_pct: float = 10.0
    max_risk_per_trade_pct: float = 2.0
    profit_target_pct: float = 10.0  # step1=10%, step2=5%
    safety_buffer_daily_pct: float = 1.0  # stop at 4% not 5%
    safety_buffer_dd_pct: float = 1.0  # floor buffer
    safety_buffer_daily_usd: float = 0.0  # when > 0, overrides pct buffer
    safety_buffer_dd_usd: float = 0.0     # when > 0, overrides pct buffer
    leverage_metals: float = 30.0
    commission_per_lot: float = 5.0
    friday_auto_close: bool = True
    friday_close_hour_utc: int = 21
    max_directional_positions: int = 3
    trade_idea_window_minutes: int = 10  # master only


class PropFirmGuard:
    """Enforces prop firm risk limits with safety buffers."""

    def __init__(self, config: PropFirmConfig | None = None) -> None:
        self._config = config or PropFirmConfig()
        c = self._config

        # Calculate limits
        self._dd_floor = c.account_size * (1 - c.max_overall_dd_pct / 100)
        self._daily_limit = c.account_size * c.daily_loss_limit_pct / 100

        if c.safety_buffer_daily_usd > 0:
            self._daily_limit_with_buffer = self._daily_limit - c.safety_buffer_daily_usd
        else:
            self._daily_limit_with_buffer = (
                c.account_size * (c.daily_loss_limit_pct - c.safety_buffer_daily_pct) / 100
            )

        if c.safety_buffer_dd_usd > 0:
            self._dd_floor_with_buffer = self._dd_floor + c.safety_buffer_dd_usd
        else:
            self._dd_floor_with_buffer = c.account_size * (
                1 - (c.max_overall_dd_pct - c.safety_buffer_dd_pct) / 100
            )
        self._profit_target = c.account_size * c.profit_target_pct / 100

        # Phase-specific profit targets (use config value, don't hardcode)
        # FundingPips: Step1=8%/10%, Step2=5%, Master/Funded=no target
        if c.phase in ("master", "funded"):
            self._profit_target = float("inf")
        # else: uses profit_target_pct from config (set per phase)

        # Daily tracking
        self._current_date: str = ""
        self._daily_start_equity: float = c.account_size
        self._daily_pnl: float = 0.0

        # Trade idea tracking (master only)
        self._recent_trades: list[tuple[datetime, str, float]] = []

        # Drawdown recovery tiers
        self._dd_tiers = [
            (c.account_size * 1.04, 1.0),   # > 4% profit: full risk
            (c.account_size, 0.8),            # breakeven: 80% risk
            (c.account_size * 0.96, 0.5),     # -4%: 50% risk
            (c.account_size * 0.92, 0.3),     # -8%: 30% risk
        ]

        logger.info(
            "PropFirmGuard: phase=%s, account=$%.0f, floor=$%.0f (buffer=$%.0f), "
            "daily_limit=$%.0f (buffer=$%.0f), target=$%.0f",
            c.phase, c.account_size, self._dd_floor, self._dd_floor_with_buffer,
            self._daily_limit, self._daily_limit_with_buffer, self._profit_target,
        )

    def update_daily(self, equity: float, bar_time: datetime) -> None:
        """Reset daily tracking on new day."""
        today = bar_time.strftime("%Y-%m-%d")
        if today != self._current_date:
            self._current_date = today
            self._daily_start_equity = equity
            self._daily_pnl = 0.0

    def can_trade(self, equity: float, bar_time: datetime) -> tuple[bool, str]:
        """Check if trading is allowed. Returns (allowed, reason)."""
        self.update_daily(equity, bar_time)
        self._daily_pnl = equity - self._daily_start_equity

        # Check DD floor (with buffer)
        if equity <= self._dd_floor_with_buffer:
            return False, f"DANGER: Equity ${equity:.2f} near DD floor ${self._dd_floor:.2f}"

        # Check daily loss (with buffer)
        if self._daily_pnl <= -self._daily_limit_with_buffer:
            return False, f"Daily loss ${self._daily_pnl:.2f} near limit ${-self._daily_limit:.2f}"

        # Friday auto-close check
        if self._config.friday_auto_close and self.should_friday_close(bar_time):
            return False, "Friday close window — no new trades"

        return True, ""

    def get_risk_multiplier(self, equity: float) -> float:
        """Get risk reduction multiplier based on current drawdown tier."""
        for threshold, multiplier in self._dd_tiers:
            if equity >= threshold:
                return multiplier
        return 0.0  # below all tiers = stop trading

    def max_position_risk(self, equity: float) -> float:
        """Maximum risk in $ for a single trade."""
        base_risk = equity * self._config.max_risk_per_trade_pct / 100.0
        multiplier = self.get_risk_multiplier(equity)
        return base_risk * multiplier

    def should_friday_close(self, bar_time: datetime) -> bool:
        """Check if we should close all positions (Friday evening)."""
        if not self._config.friday_auto_close:
            return False
        return bar_time.weekday() == 4 and bar_time.hour >= self._config.friday_close_hour_utc

    def should_stop_new_trades_friday(self, bar_time: datetime) -> bool:
        """Stop opening new trades 1 hour before Friday close."""
        if not self._config.friday_auto_close:
            return False
        return bar_time.weekday() == 4 and bar_time.hour >= (self._config.friday_close_hour_utc - 1)

    def check_directional_exposure(self, positions: list, new_direction: str) -> bool:
        """Check if adding a position in this direction is allowed."""
        same_dir = sum(
            1 for p in positions
            if getattr(p, "side", None) and p.side.value == new_direction
        )
        return same_dir < self._config.max_directional_positions

    def check_trade_idea(self, bar_time: datetime, direction: str, risk: float) -> bool:
        """Master account: check if this trade + recent same-direction trades
        exceed the 3% per trade idea limit (trades within 10 min window).
        """
        if self._config.phase != "master":
            return True  # no limit during evaluation

        window = timedelta(minutes=self._config.trade_idea_window_minutes)
        cutoff = bar_time - window

        # Clean old entries
        self._recent_trades = [
            (t, d, r) for t, d, r in self._recent_trades if t > cutoff
        ]

        # Sum risk for same direction in window
        same_dir_risk = sum(r for t, d, r in self._recent_trades if d == direction)
        max_idea_risk = self._config.account_size * 0.03  # 3% = $150

        if same_dir_risk + risk > max_idea_risk:
            logger.warning(
                "Trade idea limit: existing risk $%.2f + new $%.2f > $%.2f",
                same_dir_risk, risk, max_idea_risk,
            )
            return False

        self._recent_trades.append((bar_time, direction, risk))
        return True

    def check_profit_target(self, equity: float) -> tuple[bool, float]:
        """Check if profit target is reached. Returns (reached, profit)."""
        profit = equity - self._config.account_size
        reached = profit >= self._profit_target and self._config.phase not in ("master", "funded")
        return reached, profit

    def reset_after_payout(self, new_balance: float) -> None:
        """Reset guard state after a master account payout/withdrawal.

        Recalculates all limits from the new balance. Called manually
        after a withdrawal resets the funded account balance.

        IMPORTANT: Must only be called when ALL positions are closed.
        If called with open positions, daily loss tracking will be
        anchored to wrong reference point.
        """
        c = self._config
        old_balance = c.account_size
        c.account_size = new_balance

        self._dd_floor = new_balance * (1 - c.max_overall_dd_pct / 100)
        if c.safety_buffer_dd_usd > 0:
            self._dd_floor_with_buffer = self._dd_floor + c.safety_buffer_dd_usd
        else:
            self._dd_floor_with_buffer = new_balance * (
                1 - (c.max_overall_dd_pct - c.safety_buffer_dd_pct) / 100
            )

        self._daily_limit = new_balance * c.daily_loss_limit_pct / 100
        if c.safety_buffer_daily_usd > 0:
            self._daily_limit_with_buffer = self._daily_limit - c.safety_buffer_daily_usd
        else:
            self._daily_limit_with_buffer = (
                new_balance * (c.daily_loss_limit_pct - c.safety_buffer_daily_pct) / 100
            )

        self._dd_tiers = [
            (new_balance * 1.04, 1.0),
            (new_balance, 0.8),
            (new_balance * 0.96, 0.5),
            (new_balance * 0.92, 0.3),
        ]

        self._daily_start_equity = new_balance
        self._daily_pnl = 0.0
        self._recent_trades.clear()

        logger.info(
            "PropFirmGuard RESET after payout: $%.0f -> $%.0f, "
            "floor=$%.0f (buffer=$%.0f), daily=$%.0f (buffer=$%.0f)",
            old_balance, new_balance,
            self._dd_floor, self._dd_floor_with_buffer,
            self._daily_limit, self._daily_limit_with_buffer,
        )

    @property
    def dd_floor(self) -> float:
        return self._dd_floor

    @property
    def daily_limit(self) -> float:
        return self._daily_limit

    @property
    def profit_target(self) -> float:
        return self._profit_target
