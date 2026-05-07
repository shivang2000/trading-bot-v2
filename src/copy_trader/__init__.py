"""Copy-trading subsystem.

Mirrors trades from a *source* MT5 account (read-only via investor password)
to the *destination* MT5 account (the bot's regular trading account) with
configurable lot scaling, risk guards, and news blackout.

See `src/copy_trader/copy_trader.py` for the core class.
"""

from src.copy_trader.copy_trader import (  # noqa: F401
    CopyTrader,
    CopyTraderConfig,
    LotScalingMode,
)
