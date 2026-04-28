"""Integration test: RiskManager rejects signals during news windows.

Confirms the central gate (Diff 1 in orchestration-plan-v2.md) works for
all signal sources — Telegram + M15 strategies + M5 scalping all converge
on RiskManager._validate_risk_limits, so one check covers all paths.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.analysis.news_filter import NewsEvent, NewsEventFilter
from src.config.schema import AppConfig, RiskConfig, SignalParserConfig
from src.core.enums import SignalAction
from src.core.events import EventBus
from src.core.exceptions import RiskLimitExceeded
from src.core.models import AccountState, Signal
from src.risk.manager import RiskManager

NFP = datetime(2026, 2, 6, 13, 30, tzinfo=timezone.utc)


def _filter_with_event() -> NewsEventFilter:
    nf = NewsEventFilter.__new__(NewsEventFilter)
    nf._block_before = timedelta(minutes=15)
    nf._block_after = timedelta(minutes=30)
    nf._events = [NewsEvent("Non-Farm Payrolls (NFP)", NFP)]
    return nf


def _account_at(equity: float = 5000.0) -> AccountState:
    return AccountState(
        balance=equity, equity=equity,
        margin=0.0, free_margin=equity, margin_level=0.0,
        profit=0.0, timestamp=datetime.now(timezone.utc),
    )


@pytest.fixture
def risk_manager_with_filter():
    config = AppConfig()
    config.signal_parser = SignalParserConfig(news_filter_enabled=True)
    config.risk = RiskConfig()  # defaults
    return RiskManager(
        config=config,
        event_bus=EventBus(),
        symbol_info_func=lambda s: {"spread": 1, "max_spread": 100},
        account_state_func=lambda: _account_at(),
        positions_func=lambda s: [],
        news_filter=_filter_with_event(),
    )


def _make_signal(ts: datetime, source: str = "telegram:yoforexgold") -> Signal:
    return Signal(
        source=source,
        symbol="XAUUSD",
        action=SignalAction.BUY,
        strength=0.9,
        timestamp=ts,
        entry_price=2650.0,
        stop_loss=2645.0,
        take_profit=2660.0,
    )


@pytest.mark.parametrize(
    "source",
    ["telegram:yoforexgold", "strategy:ema_pullback", "strategy:m5_mtf_momentum"],
)
def test_signal_5min_pre_nfp_rejected_for_all_sources(risk_manager_with_filter, source):
    """Telegram, M15-strategy, and M5-scalping signals all hit the same gate."""
    sig = _make_signal(NFP - timedelta(minutes=5), source=source)
    with pytest.raises(RiskLimitExceeded) as exc:
        risk_manager_with_filter._validate_risk_limits(sig)
    assert exc.value.limit_name == "news_window"


def test_signal_post_nfp_window_passes(risk_manager_with_filter):
    sig = _make_signal(NFP + timedelta(minutes=31))
    # Should not raise news_window. Other risk checks pass with empty positions.
    risk_manager_with_filter._validate_risk_limits(sig)


def test_news_check_runs_before_other_checks(risk_manager_with_filter):
    """News-window rejection takes precedence over rr_ratio / max_open_positions."""
    # Bad R:R (0.1) — would normally be rejected for rr_ratio. But during
    # news window the news_window reason should win because it's check #0.
    bad_rr = Signal(
        source="telegram:test",
        symbol="XAUUSD",
        action=SignalAction.BUY,
        strength=0.9,
        timestamp=NFP - timedelta(minutes=5),
        entry_price=2650.0,
        stop_loss=2645.0,
        take_profit=2650.5,  # tp_dist=0.5 vs sl_dist=5 → R:R=0.1
    )
    with pytest.raises(RiskLimitExceeded) as exc:
        risk_manager_with_filter._validate_risk_limits(bad_rr)
    assert exc.value.limit_name == "news_window"
