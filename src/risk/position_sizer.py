"""Position sizer: fixed risk % per trade.

Calculates lot size based on account equity and distance to stop loss.
Auto-scales naturally — as equity grows, lot sizes grow proportionally.
"""

from __future__ import annotations

import logging
import math

from src.config.schema import AccountConfig
from src.core.models import AccountState, Signal

logger = logging.getLogger(__name__)


class PositionSizer:
    """Calculate position size using fixed risk percentage."""

    def __init__(self, account_config: AccountConfig) -> None:
        self._risk_pct = account_config.risk_per_trade_pct / 100.0
        self._min_lot = account_config.min_lot_size
        self._max_lot = account_config.max_lot_per_trade

    def calculate(
        self,
        signal: Signal,
        account_state: AccountState,
        symbol_info: dict,
    ) -> float:
        """Calculate lot size for a signal.

        Formula:
            risk_amount = equity * risk_pct
            pip_distance = |entry - stop_loss| / point
            lot_size = risk_amount / (pip_distance * tick_value)
        """
        equity = account_state.equity
        risk_amount = equity * self._risk_pct

        # Get symbol specs
        point = symbol_info.get("point", 0.00001)
        tick_value = symbol_info.get("trade_tick_value", 1.0)
        volume_min = symbol_info.get("volume_min", self._min_lot)
        volume_max = symbol_info.get("volume_max", self._max_lot)
        volume_step = symbol_info.get("volume_step", 0.01)

        # Calculate pip distance to stop loss
        entry = signal.entry_price or 0
        sl = signal.stop_loss or 0

        if entry == 0 or sl == 0 or point == 0 or tick_value == 0:
            logger.warning(
                "Cannot calculate lot size: entry=%.5f sl=%.5f point=%.5f tick_value=%.5f",
                entry, sl, point, tick_value,
            )
            return self._min_lot

        pip_distance = abs(entry - sl) / point
        if pip_distance == 0:
            return self._min_lot

        # Calculate lot size
        lot_size = risk_amount / (pip_distance * tick_value)

        # Clamp to config limits
        lot_size = max(lot_size, self._min_lot)
        lot_size = min(lot_size, self._max_lot)

        # Clamp to symbol limits
        lot_size = max(lot_size, volume_min)
        lot_size = min(lot_size, volume_max)

        # Round to volume step
        if volume_step > 0:
            lot_size = math.floor(lot_size / volume_step) * volume_step

        # Final safety clamp
        lot_size = max(lot_size, self._min_lot)

        logger.debug(
            "Position size: equity=%.2f risk=%.2f pip_dist=%.1f → %.2f lots",
            equity, risk_amount, pip_distance, lot_size,
        )

        return round(lot_size, 2)
