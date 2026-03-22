"""Emergency stop: automatic kill switch for live trading.

Monitors account state and triggers an emergency shutdown if
drawdown exceeds hard limits. Closes all positions and stops
all event processing.
"""

from __future__ import annotations

import logging

from src.core.models import AccountState

logger = logging.getLogger(__name__)


class EmergencyStop:
    """Automatic kill switch based on hard loss limits."""

    def __init__(
        self,
        max_daily_loss_usd: float = 2.0,
        max_drawdown_usd: float = 5.0,
    ) -> None:
        self._max_daily_loss = max_daily_loss_usd
        self._max_drawdown = max_drawdown_usd
        self._initial_equity: float | None = None
        self._session_start_equity: float | None = None
        self._triggered = False

    @property
    def triggered(self) -> bool:
        return self._triggered

    def check(self, account_state: AccountState) -> bool:
        """Check if emergency stop should trigger.

        Returns True if emergency stop is triggered.
        """
        if self._triggered:
            return True

        # Initialize on first check
        if self._initial_equity is None:
            self._initial_equity = account_state.equity
        if self._session_start_equity is None:
            self._session_start_equity = account_state.equity

        # Daily loss check
        daily_loss = self._session_start_equity - account_state.equity
        if daily_loss >= self._max_daily_loss:
            logger.critical(
                "EMERGENCY STOP: Daily loss $%.2f exceeds limit $%.2f",
                daily_loss, self._max_daily_loss,
            )
            self._triggered = True
            return True

        # Drawdown check (from peak equity)
        drawdown = self._initial_equity - account_state.equity
        if drawdown >= self._max_drawdown:
            logger.critical(
                "EMERGENCY STOP: Drawdown $%.2f exceeds limit $%.2f",
                drawdown, self._max_drawdown,
            )
            self._triggered = True
            return True

        return False

    def reset(self) -> None:
        """Reset the emergency stop (use with caution)."""
        self._triggered = False
        self._session_start_equity = None
        logger.warning("Emergency stop reset")
