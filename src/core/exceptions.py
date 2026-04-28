"""Custom exception hierarchy for the trading bot."""


class TradingBotError(Exception):
    """Base exception for all trading bot errors."""


# --- MT5 Connection Errors ---


class MT5Error(TradingBotError):
    """Base exception for MT5-related errors."""


class MT5ConnectionError(MT5Error):
    """Failed to connect to the MT5 RPC gateway."""


class MT5APIError(MT5Error):
    """MT5 API returned an error response."""

    def __init__(self, message: str, error_code: int | None = None):
        super().__init__(message)
        self.error_code = error_code


class MT5TimeoutError(MT5Error):
    """MT5 RPC request timed out."""


# --- Telegram Errors ---


class TelegramError(TradingBotError):
    """Base exception for Telegram-related errors."""


class TelegramConnectionError(TelegramError):
    """Failed to connect to Telegram."""


class SignalParseError(TelegramError):
    """Failed to parse a signal from a Telegram message."""


# --- Risk Errors ---


class RiskError(TradingBotError):
    """Base exception for risk management errors."""


class RiskLimitExceeded(RiskError):
    """A risk limit has been breached.

    Values may be numeric (drawdown %, lot count) or string-typed (direction
    label, news-window reason). Format adapts to the value's type so the
    exception never crashes on construction.
    """

    def __init__(self, limit_name: str, current_value, limit_value):
        super().__init__(
            f"Risk limit '{limit_name}' exceeded: "
            f"{_format_value(current_value)} > {_format_value(limit_value)}"
        )
        self.limit_name = limit_name
        self.current_value = current_value
        self.limit_value = limit_value


def _format_value(v) -> str:
    """Render a numeric value with 4 decimals; pass strings through verbatim."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return f"{v:.4f}"
    return str(v)


# --- Execution Errors ---


class ExecutionError(TradingBotError):
    """Base exception for order execution errors."""


class OrderRejectedError(ExecutionError):
    """Order was rejected by the broker."""

    def __init__(self, reason: str, retcode: int | None = None):
        super().__init__(f"Order rejected: {reason}")
        self.retcode = retcode


# --- Config Errors ---


class ConfigError(TradingBotError):
    """Configuration loading or validation error."""
