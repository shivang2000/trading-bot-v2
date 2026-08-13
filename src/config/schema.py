"""Pydantic models for configuration validation.

Every config value is validated at startup. If your YAML has a typo or
invalid type, the bot won't start — fail fast, not mid-trade.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from src.youtube.stream_config import YouTubeConfig


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
    # Max simultaneous positions in the SAME (symbol, direction). The real guard
    # against correlated stacking — e.g. 5 gold longs from one strategy burst all
    # hitting stop together (−$235, 2026-06-02). 2 = allow up to two same-side.
    max_positions_per_direction: int = 2
    max_daily_trades: int = 10
    max_daily_loss_pct: float = 5.0
    max_drawdown_pct: float = 15.0
    # R:R gate. External (Telegram/manual) signals must clear min_rr_ratio —
    # the junk-signal guard. Internal scalping strategies carry backtest-
    # validated geometry where expectancy can come from win rate, not R:R
    # (m30_rsi2 MR runs TP 1xATR / SL 2xATR = R:R 0.5 with 70% WR), so they
    # only clear the lower sanity floor. A flat 1.5 gate silently rejected
    # 100% of M30 MR signals on 2026-06-03/04.
    min_rr_ratio: float = 1.5
    min_rr_ratio_scalping: float = 0.4
    # When the bot is inside a high-impact news window (NewsEventFilter +/-
    # window minutes), PositionSizer reduces the calculated lot by this
    # percentage. Research: spreads widen 50+ pips on NFP/CPI/FOMC; experienced
    # news traders cut lot by 50% on gold (per FXNX, MarketPulse, Vantage).
    news_window_lot_reduction_pct: float = 50.0
    news_window_minutes: int = 30


class SignalParserConfig(BaseModel):
    model: str = "claude-haiku-4-5-20251001"
    timeout_ms: int = 5000
    min_confidence: float = 0.5
    stale_price_threshold_pct: float = 1.0
    atr_sl_multiplier: float = 2.0
    atr_tp_multiplier: float = 3.0
    amendment_window_minutes: int = 5
    # Central news-filter switch read by RiskManager + SignalGenerator
    news_filter_enabled: bool = True
    # Pre-news FLAT window (minutes). PositionMonitor closes bot positions if a
    # high-impact event is within this window.
    pre_news_flat_minutes: int = 5


class TrailingStopConfig(BaseModel):
    enabled: bool = True
    atr_multiplier: float = 1.5
    activation_pct: float = 0.5
    atr_period: int = 14
    atr_timeframe: str = "H1"
    # Profit-trail — forwarded to TrailingStopManager.
    # Breakeven activates once the trade is +activation_profit_points in favor;
    # the SL then trails, giving back giveback_pct of peak profit (capped at
    # max_giveback_points) and is floored at entry (a winner never reverts to a loss).
    #
    # units="price": thresholds are absolute price units (legacy, gold-tuned —
    #   12 price units is 0.3% on gold but a trade-strangling 0.02% on US30).
    # units="percent": thresholds are % of entry price, so one config scales
    #   across gold/indices/crypto (2026-08-13 US30 fix: +$1.32 exit on a
    #   $34-target ORB trade).
    units: str = "price"
    activation_profit_points: float = 5.0
    giveback_pct: float = 0.10
    max_giveback_points: float = 10.0
    # One-time partial profit-book at a fixed favorable move. Only fires when the
    # lot can be split (volume >= 2x the broker minimum) — e.g. the $10k account.
    partial_book_enabled: bool = False
    partial_book_trigger_points: float = 10.0
    partial_book_fraction: float = 0.5


class PositionMonitorConfig(BaseModel):
    poll_interval_seconds: int = 1


class PartialProfitConfig(BaseModel):
    enabled: bool = True
    min_levels_for_partial: int = 2  # need at least 2 TPs to trigger partial closes
    breakeven_buffer_points: float = 1.0  # SL offset above/below entry on breakeven move


class TickEngineConfig(BaseModel):
    """Tick-driven exit-management engine.

    When enabled, TickStream polls MT5 every poll_interval_ms and routes
    each tick to TickPositionManager which runs trailing-stop and
    partial-profit logic on every price update (no longer waiting for
    the 30s position-monitor poll). Entry signals are unaffected.
    """

    enabled: bool = False  # off-by-default; opt in per account config
    poll_interval_ms: int = 200
    symbols: list[str] = Field(default_factory=list)  # empty = use signal_generator.instruments
    # Tightened from 2.0 → 8.0 after research: FTMO publishes 2,000
    # server-requests/day cap. 2s/ticket = 30/min/ticket; with 3 open positions
    # that's ~5,400 mods/hour, above the cap. 8s/ticket gives ~1,350/hr safe
    # envelope. See plan section 5 + airis:deep-research finding.
    modify_rate_limit_seconds: float = 8.0  # min seconds between SL modifies per ticket
    drop_unchanged_modifies: bool = True
    # Distance below which we don't bother sending a modify (avoids broker rejection
    # and pointless RPC). Expressed in MT5 points.
    min_sl_change_points: float = 5.0
    # When tick engine owns trailing/partial work, suppress duplicate logic in
    # PositionMonitor's poll loop. Position close-detection + foreign-position
    # scan + pre-news flat all stay on the poll path.
    suppress_poll_position_management: bool = True


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
    # Regime gate: run breakout/trend strategies only in trending regimes and
    # mean-reversion only in ranging; block all in CHOPPY. The #1 win-rate fix —
    # live losses were breakout strats whipsawing in a RANGING market.
    regime_filter_enabled: bool = True
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


class XauusdNyOrbConfig(BaseModel):
    """NY Open Range Breakout — bar-armed, tick-fired XAUUSD strategy.

    Evidence: Zarattini SSRN 4729284, IEEE TORB, yulz008/GOLD_ORB.
    Off-by-default until walk-forward gate clears (RUNBOOK §5).
    """

    enabled: bool = False
    consolidation_bars: int = 3
    velocity_window_ticks: int = 30
    velocity_atr_mult: float = 0.5
    stale_buffer_pips: float = 3.0
    adx_min: float = 22.0
    atr_m1_period: int = 14
    atr_m5_period: int = 14
    pdh_pdl_confluence_atr_pct: float = 0.30
    pdh_pdl_confluence_score: float = 0.20
    sl_atr_m1_mult: float = 1.0
    risk_pct: float = 1.0
    daily_max_entries: int = 3
    hold_time_floor_seconds: int = 120
    london_secondary: bool = True


class XauusdPullbackWindowConfig(BaseModel):
    """4-phase EMA pullback state machine — research-only until walk-forward
    + DSR > 0.5 gate passes. Port of ilahuerta-IA/backtrader-pullback-window.

    DO NOT enable in production until validation gate clears.
    """

    enabled: bool = False
    fast_ema: int = 1
    medium_ema: int = 14
    confirm_ema: int = 18
    slow_ema: int = 24
    pullback_max_bars: int = 3
    entry_window_bars: int = 2
    sl_atr_mult: float = 2.5
    tp_atr_mult: float = 12.0
    risk_pct: float = 1.0
    timeframe: str = "M5"


class AITrendRiderV2Config(BaseModel):
    """Video-derived Trend Rider candidate and paid-source activation gate.

    The numerical defaults below were transcribed from the public video. They
    are sufficient to implement a candidate, but ``parameters_verified`` must
    remain false until its trades reproduce the Trader.dev v2 report or the
    paid Pine source is independently audited.
    """

    enabled: bool = False
    parameters_verified: bool = False
    strategy_id: str = "01KXRP30E3WXTASJV22W8Q5771"
    backtest_id: str = "01KXRP3JJ5FNRNF3PCQH1FM9NF"
    symbol: str = "XAUUSD"
    timeframe: str = "H1"
    risk_pct: float = Field(default=0.25, gt=0, le=0.5)
    completed_candle_only: bool = True
    pyramiding: bool = False

    # Public-video inputs (TradingView settings, 2026-07-13 video).
    t3_length: int = Field(default=8, ge=1)
    t3_factor: float = Field(default=0.7, gt=0, le=1)
    range_filter_sampling_period: int = Field(default=50, ge=2)
    range_filter_multiplier: float = Field(default=2.5, gt=0)
    atr_length: int = Field(default=14, ge=1)
    atr_stop_multiplier: float = Field(default=2.5, gt=0)
    reward_risk_ratio: float = Field(default=3.8, gt=0)
    highest_high_lookback: int = Field(default=50, ge=1)
    lowest_low_lookback: int = Field(default=50, ge=1)
    entry_mode: str = "flip"
    trade_direction: str = "long_only"

    @model_validator(mode="after")
    def require_verified_parameters_before_activation(self) -> AITrendRiderV2Config:
        if self.enabled and not self.parameters_verified:
            raise ValueError("parameters_verified must be true before enabling AI Trend Rider v2")
        if self.entry_mode != "flip":
            raise ValueError("only the publicly disclosed 'flip' entry mode is implemented")
        if self.trade_direction != "long_only":
            raise ValueError("only the publicly disclosed 'long_only' direction is implemented")
        return self


class StrategyHealthConfig(BaseModel):
    """Live-account early-warning thresholds. See StrategyHealthMonitor."""

    enabled: bool = True
    spread_baseline_window: int = 20
    spread_multiplier: float = 1.5
    spread_consecutive_breach: int = 3
    slippage_avg_points_max: float = 10.0
    slippage_window: int = 20
    modify_rejection_pct_max: float = 5.0
    modify_window: int = 100
    atr_expansion_multiplier: float = 2.5
    atr_session_window: int = 20
    wr_window: int = 20
    wr_floor_pct: float = 40.0
    hold_time_floor_seconds: int = 120
    hold_time_breach_pct_max: float = 10.0
    hold_time_window: int = 20
    dd_proximity_pct_of_limit: float = 60.0
    trade_frequency_window_minutes: int = 15
    trade_frequency_max_entries: int = 3


class StrategiesConfig(BaseModel):
    ema_pullback: EmaPullbackConfig = Field(default_factory=EmaPullbackConfig)
    london_breakout: LondonBreakoutConfig = Field(default_factory=LondonBreakoutConfig)
    ny_momentum: NyMomentumConfig = Field(default_factory=NyMomentumConfig)
    smc_confluence: SmcConfluenceConfig = Field(default_factory=SmcConfluenceConfig)
    scalping: ScalpingConfig = Field(default_factory=ScalpingConfig)
    xauusd_ny_orb: XauusdNyOrbConfig = Field(default_factory=XauusdNyOrbConfig)
    xauusd_pullback_window: XauusdPullbackWindowConfig = Field(
        default_factory=XauusdPullbackWindowConfig
    )
    ai_trend_rider_v2: AITrendRiderV2Config = Field(
        default_factory=AITrendRiderV2Config
    )


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


class AIEndpointConfig(BaseModel):
    """One OpenAI-compatible API endpoint (Ollama Cloud, Moonshot, ...)."""

    base_url: str
    api_key_env: str


class AIModelRef(BaseModel):
    """Points a role at (endpoint, model)."""

    endpoint: str
    model: str


class AIModelsConfig(BaseModel):
    """Role-based model routing: reasoning drives the tool loop, vision
    reads chart images, heavy (optional) serves deep/scanner analysis."""

    reasoning: AIModelRef = Field(
        default_factory=lambda: AIModelRef(endpoint="ollama", model="glm-5.2")
    )
    vision: AIModelRef | None = Field(
        default_factory=lambda: AIModelRef(endpoint="ollama", model="minimax-m3")
    )
    heavy: AIModelRef | None = None


class AIAnalystConfig(BaseModel):
    """Fail-open pre-trade AI confirmation layer."""

    enabled: bool = False
    dry_run: bool = True
    total_timeout_seconds: float = 12.0
    request_timeout_seconds: float = 8.0
    max_iterations: int = 4
    min_confidence_to_veto: float = 0.6
    allow_downsize: bool = True
    min_risk_pct: float = 0.1
    max_calls_per_hour: int = 30
    cache_ttl_seconds: int = 240
    circuit_breaker_failures: int = 3
    circuit_breaker_cooldown_minutes: int = 15
    # Strategy allowlist; empty list = all strategies
    strategies: list[str] = Field(
        default_factory=lambda: ["m30_rsi2_mean_reversion"]
    )


class AIScannerConfig(BaseModel):
    """Shadow-mode AI market scanner (P1 — no execution)."""

    enabled: bool = False
    interval_seconds: int = 900
    instruments: list[str] = Field(default_factory=list)
    max_scans_per_day: int = 40
    shadow_ttl_hours: int = 24


class AIConfig(BaseModel):
    """AI layer root config. All AI runs on Ollama Cloud by default."""

    endpoints: dict[str, AIEndpointConfig] = Field(
        default_factory=lambda: {
            "ollama": AIEndpointConfig(
                base_url="https://ollama.com/v1", api_key_env="OLLAMA_API_KEY"
            ),
            "kimi": AIEndpointConfig(
                base_url="https://api.moonshot.ai/v1", api_key_env="KIMI_API_KEY"
            ),
        }
    )
    models: AIModelsConfig = Field(default_factory=AIModelsConfig)
    analyst: AIAnalystConfig = Field(default_factory=AIAnalystConfig)
    scanner: AIScannerConfig = Field(default_factory=AIScannerConfig)


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
    tick_engine: TickEngineConfig = Field(default_factory=TickEngineConfig)
    claude_filter: ClaudeFilterConfig = Field(default_factory=ClaudeFilterConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    strategy_health: StrategyHealthConfig = Field(default_factory=StrategyHealthConfig)
    # YouTube live stream signal source (optional, default disabled)
    youtube: YouTubeConfig = Field(default_factory=YouTubeConfig)
