"""Diff 8 — daily-loss baseline survives restart.

Closes the $5k-bust gap: without persistence, every restart reset
session_start_equity to live equity, allowing a mid-day restart to mask
up to a full daily-limit drawdown.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.config.schema import AppConfig, RiskConfig, SignalParserConfig
from src.core.enums import SignalAction
from src.core.events import EventBus
from src.core.exceptions import RiskLimitExceeded
from src.core.models import AccountState, Signal
from src.risk.manager import RiskManager


def _make_signal(ts: datetime | None = None) -> Signal:
    return Signal(
        source="telegram:test",
        symbol="XAUUSD",
        action=SignalAction.BUY,
        strength=0.9,
        timestamp=ts or datetime.now(timezone.utc),
        entry_price=2650.0,
        stop_loss=2645.0,
        take_profit=2660.0,
    )


def _account(equity: float) -> AccountState:
    return AccountState(
        balance=equity,
        equity=equity,
        margin=0.0,
        free_margin=equity,
        margin_level=0.0,
        profit=0.0,
        timestamp=datetime.now(timezone.utc),
    )


def _build_rm(start_equity: float, current_equity: float) -> RiskManager:
    config = AppConfig()
    config.risk = RiskConfig(max_daily_loss_pct=5.0)
    config.signal_parser = SignalParserConfig(news_filter_enabled=False)
    rm = RiskManager(
        config=config,
        event_bus=EventBus(),
        symbol_info_func=lambda s: {"spread": 1, "max_spread": 100},
        account_state_func=lambda: _account(current_equity),
        positions_func=lambda s: [],
    )
    rm.set_session_start_equity(start_equity)
    rm.set_peak_equity(start_equity)
    return rm


def test_persisted_baseline_blocks_signal_after_restart():
    """Started day at 5000. Lost down to 4749 (-5.02%). Restart restores
    baseline=5000 from DB → daily-loss limit (5%) crossed → reject."""
    rm = _build_rm(start_equity=5000.0, current_equity=4749.0)
    with pytest.raises(RiskLimitExceeded) as exc:
        rm._validate_risk_limits(_make_signal())
    assert exc.value.limit_name == "max_daily_loss_pct"


def test_no_persistence_would_have_allowed_signal():
    """Without Diff 8, the same loss is invisible: baseline reset to
    current equity on restart, daily P&L looks like 0%, gate passes.
    Documents the bug class explicitly so it doesn't regress."""
    rm = _build_rm(start_equity=4749.0, current_equity=4749.0)  # baseline clobbered
    rm._validate_risk_limits(_make_signal())  # passes — old (buggy) behavior


def test_session_start_equity_property():
    rm = _build_rm(start_equity=10000.0, current_equity=10000.0)
    assert rm.session_start_equity == 10000.0
    rm.set_session_start_equity(9500.0)
    assert rm.session_start_equity == 9500.0


def test_peak_equity_persists_via_setter():
    rm = _build_rm(start_equity=10000.0, current_equity=10000.0)
    rm.set_peak_equity(11500.0)
    assert rm.peak_equity == 11500.0


def test_initialize_does_not_clobber_baseline():
    """Critical: RiskManager.initialize() must NOT reset session_start_equity
    from live account_state_func. main.py restores from DB before calling
    initialize, and the previous behavior wiped that restoration."""
    import asyncio
    rm = _build_rm(start_equity=5000.0, current_equity=4900.0)
    asyncio.run(rm.initialize())
    # Baseline preserved despite initialize seeing live equity=4900
    assert rm.session_start_equity == 5000.0
    assert rm.peak_equity == 5000.0


def test_drawdown_limit_uses_persisted_peak():
    """Peak=12000 from DB, current=10199 → DD=15.01% > 15% limit → reject."""
    rm = _build_rm(start_equity=10000.0, current_equity=10199.0)
    rm.set_peak_equity(12000.0)
    with pytest.raises(RiskLimitExceeded) as exc:
        rm._validate_risk_limits(_make_signal())
    assert exc.value.limit_name == "max_drawdown_pct"
