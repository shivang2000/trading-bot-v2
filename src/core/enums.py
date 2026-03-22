"""Core enumerations for the trading bot."""

from enum import Enum, IntEnum


class Timeframe(str, Enum):
    """MT5 timeframe constants mapped to human-readable names."""

    M1 = "M1"
    M2 = "M2"
    M3 = "M3"
    M4 = "M4"
    M5 = "M5"
    M6 = "M6"
    M10 = "M10"
    M12 = "M12"
    M15 = "M15"
    M20 = "M20"
    M30 = "M30"
    H1 = "H1"
    H2 = "H2"
    H3 = "H3"
    H4 = "H4"
    H6 = "H6"
    H8 = "H8"
    H12 = "H12"
    D1 = "D1"
    W1 = "W1"
    MN1 = "MN1"

    @property
    def mt5_value(self) -> int:
        """Return the MT5 integer constant for this timeframe."""
        return _TIMEFRAME_MT5_MAP[self]

    @property
    def seconds(self) -> int:
        """Return the duration in seconds for this timeframe."""
        return _TIMEFRAME_SECONDS[self]


# MT5 TIMEFRAME constants (from MetaTrader5 library)
_TIMEFRAME_MT5_MAP: dict[Timeframe, int] = {
    Timeframe.M1: 1,
    Timeframe.M2: 2,
    Timeframe.M3: 3,
    Timeframe.M4: 4,
    Timeframe.M5: 5,
    Timeframe.M6: 6,
    Timeframe.M10: 10,
    Timeframe.M12: 12,
    Timeframe.M15: 15,
    Timeframe.M20: 20,
    Timeframe.M30: 30,
    Timeframe.H1: 16385,
    Timeframe.H2: 16386,
    Timeframe.H3: 16387,
    Timeframe.H4: 16388,
    Timeframe.H6: 16390,
    Timeframe.H8: 16392,
    Timeframe.H12: 16396,
    Timeframe.D1: 16408,
    Timeframe.W1: 32769,
    Timeframe.MN1: 49153,
}

_TIMEFRAME_SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: 60,
    Timeframe.M2: 120,
    Timeframe.M3: 180,
    Timeframe.M4: 240,
    Timeframe.M5: 300,
    Timeframe.M6: 360,
    Timeframe.M10: 600,
    Timeframe.M12: 720,
    Timeframe.M15: 900,
    Timeframe.M20: 1200,
    Timeframe.M30: 1800,
    Timeframe.H1: 3600,
    Timeframe.H2: 7200,
    Timeframe.H3: 10800,
    Timeframe.H4: 14400,
    Timeframe.H6: 21600,
    Timeframe.H8: 28800,
    Timeframe.H12: 43200,
    Timeframe.D1: 86400,
    Timeframe.W1: 604800,
    Timeframe.MN1: 2592000,
}


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"

    @property
    def mt5_value(self) -> int:
        return _ORDER_TYPE_MT5_MAP[self]


_ORDER_TYPE_MT5_MAP: dict[OrderType, int] = {
    OrderType.MARKET: 0,
    OrderType.LIMIT: 2,
    OrderType.STOP: 4,
    OrderType.STOP_LIMIT: 6,
}


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    CLOSE_BUY = "CLOSE_BUY"
    CLOSE_SELL = "CLOSE_SELL"
    CLOSE_ALL = "CLOSE_ALL"


class CloseReason(str, Enum):
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    SIGNAL = "SIGNAL"
    MANUAL = "MANUAL"
    EMERGENCY = "EMERGENCY"
