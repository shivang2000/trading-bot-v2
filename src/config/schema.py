"""Pydantic models for configuration validation.

Every config value is validated at startup. If your YAML has a typo or
invalid type, the bot won't start — fail fast, not mid-trade.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MT5Config(BaseModel):
    rpyc_host: str = "localhost"
    rpyc_port: int = 8001
    connection_timeout: int = 30


class InstrumentConfig(BaseModel):
    symbol: str
    point_size: float = 0.01
    tick_value: float = 1.0
    min_lot: float = 0.01
    max_lot: float = 100.0
    lot_step: float = 0.01


class AccountConfig(BaseModel):
    initial_balance: float = 30.0
    risk_per_trade_pct: float = 1.0
    max_lot_per_trade: float = 0.10
    min_lot_size: float = 0.01


class RiskConfig(BaseModel):
    max_open_positions: int = 3
    max_positions_per_symbol: int = 1
    max_daily_trades: int = 10
    max_daily_loss_pct: float = 5.0
    max_drawdown_pct: float = 15.0


class SignalParserConfig(BaseModel):
    model: str = "claude-haiku-4-5-20251001"
    timeout_ms: int = 5000
    stale_price_threshold_pct: float = 1.0
    atr_sl_multiplier: float = 2.0
    atr_tp_multiplier: float = 3.0
    amendment_window_minutes: int = 5


class PositionMonitorConfig(BaseModel):
    poll_interval_seconds: int = 30


class TelegramNotificationConfig(BaseModel):
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


class SlackConfig(BaseModel):
    enabled: bool = False
    webhook_url: str = ""


class MonitoringConfig(BaseModel):
    log_level: str = "INFO"
    log_file: str = "logs/trading.log"
    telegram: TelegramNotificationConfig = Field(
        default_factory=TelegramNotificationConfig
    )
    slack: SlackConfig = Field(default_factory=SlackConfig)


class TelegramListenerConfig(BaseModel):
    """Config for the Telethon user account connection."""

    api_id: str = ""
    api_hash: str = ""
    phone: str = ""
    session_path: str = "data/telegram_session"


class ChannelConfig(BaseModel):
    """Config for a single Telegram signal channel."""

    id: str
    name: str = ""
    enabled: bool = True
    instruments: list[str] = Field(default_factory=list)
    notes: str = ""


class DatabaseConfig(BaseModel):
    path: str = "data/trading_bot_v2.db"


class AppConfig(BaseModel):
    """Root configuration model. Everything rolls up here."""

    mt5: MT5Config = Field(default_factory=MT5Config)
    account: AccountConfig = Field(default_factory=AccountConfig)
    instruments: list[InstrumentConfig] = Field(default_factory=list)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    signal_parser: SignalParserConfig = Field(default_factory=SignalParserConfig)
    position_monitor: PositionMonitorConfig = Field(
        default_factory=PositionMonitorConfig
    )
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    telegram_listener: TelegramListenerConfig = Field(
        default_factory=TelegramListenerConfig
    )
    channels: list[ChannelConfig] = Field(default_factory=list)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
