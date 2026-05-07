"""Pydantic config schema for the copy-trader process.

Deliberately separate from `src.config.schema.AppConfig` to keep the two
process stacks isolated. The copy-trader does NOT load AppConfig; it
loads a `CopyTraderAppConfig` from `config/copy_trader.yaml`.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MT5EndpointConfig(BaseModel):
    """One MT5 RPyC endpoint (a single account, single container)."""

    name: str = "source"  # logical label: "source" | "dest"
    rpyc_host: str = "localhost"
    rpyc_port: int = 8001
    connection_timeout: int = 30
    # Account login number — used as a sanity check at boot
    expected_account_login: int = 0  # 0 = skip check


class CopyTraderConfig(BaseModel):
    """Mirror behaviour."""

    enabled: bool = True
    poll_interval_ms: int = 100  # source poll cycle
    # Lot scaling — only "exact" implemented in v1; structure exposes future modes
    lot_mode: str = "exact"  # "exact" | "proportional" | "multiplier"
    lot_multiplier: float = 1.0  # used by "multiplier" mode
    lot_min: float = 0.01
    lot_max: float = 100.0
    # Skip pre-existing positions at boot
    ignore_existing_at_boot: bool = True
    # Reconciliation — on restart, verify mirror_map entries still match reality
    reconcile_on_restart: bool = True
    # Modification detection thresholds (avoid noise from broker-side decimal jitter)
    sl_change_min_points: float = 1.0
    tp_change_min_points: float = 1.0
    # Retry policy for mirror-side order_send failures
    order_send_max_retries: int = 1
    order_send_retry_delay_seconds: float = 0.5


class SlackConfig(BaseModel):
    enabled: bool = True
    webhook_url: str | None = ""   # None when env var unset; treated as disabled
    # Channel suffix on alerts so dest deploy can be told apart from existing bot
    channel_label: str = "copy-trader"


class JournalConfig(BaseModel):
    db_path: str = "data/mirror_journal.db"
    # Trim closed-trade rows older than N days
    retention_days: int = 90


class CopyTraderAppConfig(BaseModel):
    """Root config for the copy-trader process."""

    source: MT5EndpointConfig = Field(default_factory=lambda: MT5EndpointConfig(
        name="source", rpyc_host="mt5-source", rpyc_port=8001,
    ))
    dest: MT5EndpointConfig = Field(default_factory=lambda: MT5EndpointConfig(
        name="dest", rpyc_host="mt5-dest", rpyc_port=8001,
    ))
    copy_trader: CopyTraderConfig = Field(default_factory=CopyTraderConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    journal: JournalConfig = Field(default_factory=JournalConfig)
    log_level: str = "INFO"
