"""Leverage-aware position sizer for prop firm trading.

Calculates lot size considering:
1. Risk-based sizing (% of equity at risk)
2. Leverage/margin constraints (1:30 for metals)
3. Commission impact on expected P&L

Also contains the original PositionSizer class for backward compatibility.
"""

from __future__ import annotations

import logging
import math

from src.config.schema import AccountConfig
from src.core.models import AccountState, Signal

logger = logging.getLogger(__name__)


def calculate_lot_size(
    equity: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
    point_size: float = 0.01,
    tick_value: float = 1.0,
    min_lot: float = 0.01,
    max_lot: float = 0.50,
    risk_multiplier: float = 1.0,
) -> float:
    """Standard position sizer (existing logic, extracted as function)."""
    risk_dollars = equity * risk_pct / 100.0 * risk_multiplier
    sl_distance = abs(entry_price - stop_loss)
    if sl_distance <= 0:
        return min_lot
    sl_pips = sl_distance / point_size if point_size > 0 else sl_distance
    cost_per_lot = sl_pips * tick_value
    volume = risk_dollars / cost_per_lot if cost_per_lot > 0 else min_lot
    return round(max(min_lot, min(volume, max_lot)), 2)


def calculate_lot_size_prop_firm(
    equity: float,
    risk_pct: float,
    entry_price: float,
    stop_loss: float,
    leverage: float = 30.0,
    contract_size: float = 100.0,
    point_size: float = 0.01,
    tick_value: float = 1.0,
    min_lot: float = 0.01,
    commission_per_lot: float = 5.0,
    risk_multiplier: float = 1.0,
    max_margin_usage_pct: float = 50.0,
) -> float:
    """Leverage-aware position sizer for prop firm accounts.

    Respects both risk-based and margin-based constraints.
    On a $5K account with 1:30 leverage on gold at $3000:
    - Margin per lot = $3000 * 100 / 30 = $10,000
    - Max lots from margin (50% usage) = $2,500 / $10,000 = 0.25 lots
    """
    risk_dollars = equity * risk_pct / 100.0 * risk_multiplier
    sl_distance = abs(entry_price - stop_loss)
    if sl_distance <= 0:
        return min_lot

    # Lot size from risk
    sl_pips = sl_distance / point_size if point_size > 0 else sl_distance
    cost_per_pip = tick_value
    risk_lot = risk_dollars / (sl_pips * cost_per_pip) if sl_pips * cost_per_pip > 0 else min_lot

    # Lot size from leverage (margin constraint)
    margin_per_lot = (entry_price * contract_size) / leverage if leverage > 0 else float("inf")
    max_margin = equity * max_margin_usage_pct / 100.0
    margin_lot = max_margin / margin_per_lot if margin_per_lot > 0 else min_lot

    # Use the smaller of risk-based and margin-based
    lot_size = min(risk_lot, margin_lot)
    lot_size = max(min_lot, round(lot_size, 2))

    logger.debug(
        "Prop firm sizer: equity=$%.2f risk=%.1f%% SL=$%.2f → risk_lot=%.2f margin_lot=%.2f → %.2f",
        equity, risk_pct, sl_distance, risk_lot, margin_lot, lot_size,
    )
    return lot_size


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
        risk_pct: float | None = None,
    ) -> float:
        """Calculate lot size for a signal.

        Args:
            risk_pct: Optional per-instrument risk override (e.g. 1.0 for 1%).
                      If provided, overrides the global account risk_per_trade_pct.

        Formula:
            risk_amount = equity * risk_pct
            pip_distance = |entry - stop_loss| / point
            lot_size = risk_amount / (pip_distance * tick_value)
        """
        equity = account_state.equity
        effective_risk = (risk_pct / 100.0) if risk_pct else self._risk_pct
        risk_amount = equity * effective_risk

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
