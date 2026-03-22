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
    min_confidence: float = 0.5
    stale_price_threshold_pct: float = 1.0
    atr_sl_multiplier: float = 2.0
    atr_tp_multiplier: float = 3.0
    amendment_window_minutes: int = 5


class TrailingStopConfig(BaseModel):
    enabled: bool = True
    atr_multiplier: float = 1.5
    activation_pct: float = 0.5
    atr_period: int = 14
    atr_timeframe: str = "H1"


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


class EmaPullbackConfig(BaseModel):
    enabled: bool = True
    fast_ema: int = 8
    slow_ema: int = 21
    trend_ema: int = 50
    pullback_max_candles: int = 3
    entry_window_candles: int = 2
    atr_sl_multiplier: float = 2.0
    atr_tp_multiplier: float = 3.0
    entry_timeframe: str = "M15"
    regime_timeframe: str = "H1"


class LondonBreakoutConfig(BaseModel):
    enabled: bool = True
    asian_start_hour: int = 0
    asian_end_hour: int = 7
    breakout_buffer_pips: float = 5.0
    max_trades_per_day: int = 1
    tp_multiplier: float = 1.5
    timeframe: str = "M15"


class SmcConfluenceConfig(BaseModel):
    enabled: bool = True
    ob_confidence_boost: float = 0.10
    fvg_confidence_boost: float = 0.10
    bos_confidence_boost: float = 0.05
    liquidity_sweep_boost: float = 0.10
    opposing_ob_penalty: float = 0.15
    lookback_bars: int = 100


class StrategiesConfig(BaseModel):
    ema_pullback: EmaPullbackConfig = Field(default_factory=EmaPullbackConfig)
    london_breakout: LondonBreakoutConfig = Field(default_factory=LondonBreakoutConfig)
    smc_confluence: SmcConfluenceConfig = Field(default_factory=SmcConfluenceConfig)


class SignalGeneratorConfig(BaseModel):
    enabled: bool = True
    scan_interval_seconds: int = 300
    instruments: list[str] = Field(default_factory=lambda: ["XAUUSD", "XAGUSD", "BTCUSD", "ETHUSD"])
    allowed_sessions: list[str] = Field(
        default_factory=lambda: ["london", "new_york", "london_ny_overlap"]
    )


class AppConfig(BaseModel):
    """Root configuration model. Everything rolls up here."""

    mt5: MT5Config = Field(default_factory=MT5Config)
    account: AccountConfig = Field(default_factory=AccountConfig)
    instruments: list[InstrumentConfig] = Field(default_factory=list)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    signal_parser: SignalParserConfig = Field(default_factory=SignalParserConfig)
    trailing_stop: TrailingStopConfig = Field(default_factory=TrailingStopConfig)
    position_monitor: PositionMonitorConfig = Field(
        default_factory=PositionMonitorConfig
    )
    signal_generator: SignalGeneratorConfig = Field(default_factory=SignalGeneratorConfig)
    strategies: StrategiesConfig = Field(default_factory=StrategiesConfig)
    monitoring: MonitoringConfig = Field(default_factory=MonitoringConfig)
    telegram_listener: TelegramListenerConfig = Field(
        default_factory=TelegramListenerConfig
    )
    channels: list[ChannelConfig] = Field(default_factory=list)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
