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


class BalanceAdjustment(BaseModel):
    date: str
    type: str = "deposit"  # "deposit" or "withdrawal"
    amount: float = 0.0
    note: str = ""


class AccountConfig(BaseModel):
    initial_balance: float = 100.0
    mode: str = "demo"
    risk_per_trade_pct: float = 1.0
    max_lot_per_trade: float = 100.0
    min_lot_size: float = 0.01
    balance_adjustments: list[BalanceAdjustment] = Field(default_factory=list)


def get_adjusted_initial_capital(config: AccountConfig) -> float:
    """Calculate effective initial capital from deposits minus withdrawals."""
    capital = 0.0
    for adj in config.balance_adjustments:
        if adj.type == "deposit":
            capital += adj.amount
        elif adj.type == "withdrawal":
            capital -= adj.amount
    return max(capital, 0.01)


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
    poll_interval_seconds: int = 1


class PartialProfitConfig(BaseModel):
    enabled: bool = True
    min_levels_for_partial: int = 2  # need at least 2 TPs to trigger partial closes
    breakeven_buffer_points: float = 1.0  # SL offset above/below entry on breakeven move


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


class NyMomentumConfig(BaseModel):
    enabled: bool = True
    range_breakout_buffer_pips: float = 3.0
    range_tp_multiplier: float = 2.0
    range_max_trades_per_day: int = 1
    momentum_max_trades_per_day: int = 1


class SmcConfluenceConfig(BaseModel):
    enabled: bool = True
    ob_confidence_boost: float = 0.10
    fvg_confidence_boost: float = 0.10
    bos_confidence_boost: float = 0.05
    liquidity_sweep_boost: float = 0.10
    opposing_ob_penalty: float = 0.15
    lookback_bars: int = 100
    fvg_entry_zone_boost: float = 0.15
    anchored_vwap_bounce_boost: float = 0.10
    volume_profile_poc_boost: float = 0.10


class InstrumentStrategyOverride(BaseModel):
    """Per-instrument, per-strategy risk configuration."""
    risk_pct: float = 1.0


class ScalpingConfig(BaseModel):
    enabled: bool = True
    max_trades_per_strategy: int = 1
    max_total_open_positions: int = 10
    max_daily_trades_per_strategy: int = 50
    max_daily_trades_total: int = 200
    daily_loss_limit_pct: float = 5.0
    risk_per_trade_pct: float = 1.0
    profit_growth_factor: float = 0.50  # use only 50% of profits for risk sizing
    use_tiered_lot_caps: bool = False
    lot_cap_tiers: list[list[float]] = Field(default_factory=lambda: [
        [0, 0.50], [500, 1.00], [2000, 2.00], [5000, 5.00], [10000, 10.00]
    ])
    scan_interval_seconds: int = 15
    instruments: list[str] = Field(default_factory=list)  # empty = use signal_generator.instruments
    strategies_enabled: list[str] = Field(default_factory=lambda: [
        "m5_dual_supertrend", "m5_keltner_squeeze", "m5_vwap_mean_reversion",
        "m5_stochrsi_adx", "m5_mtf_momentum", "m5_bb_squeeze", "m5_mean_reversion",
        "m1_heikin_ashi_momentum", "m1_rsi_scalp", "m1_supertrend_scalp", "m1_ema_micro",
    ])
    instrument_strategy_overrides: dict[str, dict[str, InstrumentStrategyOverride]] = Field(
        default_factory=dict,
        description="Per-instrument strategy whitelist with optimal risk. Key=symbol, Value=dict of strategy→override",
    )


class PropFirmConfig(BaseModel):
    enabled: bool = False
    provider: str = "fundingpips"
    account_size: float = 5000.0
    phase: str = "step1"  # step1, step2, master
    leverage_metals: float = 30.0
    commission_per_lot_metals: float = 5.0
    daily_loss_limit_pct: float = 5.0
    max_overall_dd_pct: float = 10.0
    max_risk_per_trade_pct: float = 2.0
    profit_target_pct: float = 10.0
    safety_buffer_daily_pct: float = 1.0
    safety_buffer_dd_pct: float = 1.0
    safety_buffer_daily_usd: float = 0.0  # when > 0, overrides pct buffer
    safety_buffer_dd_usd: float = 0.0     # when > 0, overrides pct buffer
    friday_auto_close: bool = True
    friday_close_hour_utc: int = 21
    news_filter_enabled: bool = True
    max_directional_positions: int = 3
    min_trading_days: int = 3
    inactivity_limit_days: int = 30


class StrategiesConfig(BaseModel):
    ema_pullback: EmaPullbackConfig = Field(default_factory=EmaPullbackConfig)
    london_breakout: LondonBreakoutConfig = Field(default_factory=LondonBreakoutConfig)
    ny_momentum: NyMomentumConfig = Field(default_factory=NyMomentumConfig)
    smc_confluence: SmcConfluenceConfig = Field(default_factory=SmcConfluenceConfig)
    scalping: ScalpingConfig = Field(default_factory=ScalpingConfig)


class InstrumentOverride(BaseModel):
    """Per-instrument parameter overrides."""
    risk_per_trade_pct: float | None = None
    atr_sl_multiplier: float | None = None
    atr_tp_multiplier: float | None = None


class SignalGeneratorConfig(BaseModel):
    enabled: bool = True
    scan_interval_seconds: int = 300
    instruments: list[str] = Field(default_factory=lambda: ["XAUUSD", "XAGUSD", "BTCUSD", "ETHUSD"])
    allowed_sessions: list[str] = Field(
        default_factory=lambda: ["london", "new_york", "london_ny_overlap"]
    )
    instrument_overrides: dict[str, InstrumentOverride] = Field(default_factory=dict)


class ClaudeFilterConfig(BaseModel):
    """Config for Claude AI pre-trade signal filter."""

    enabled: bool = False
    model: str = "claude-haiku-4-5-20251001"
    confidence_threshold: float = 0.65
    timeout_seconds: float = 5.0


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
    prop_firm: PropFirmConfig = Field(default_factory=PropFirmConfig)
    partial_profit: PartialProfitConfig = Field(default_factory=PartialProfitConfig)
    claude_filter: ClaudeFilterConfig = Field(default_factory=ClaudeFilterConfig)
