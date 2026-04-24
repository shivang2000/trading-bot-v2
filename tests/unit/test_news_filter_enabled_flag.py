"""Verify the news_filter_enabled config flag short-circuits the central gate.

This test guards against the previous behaviour where the flag was cosmetic —
the filter ran unconditionally inside _scan_scalping. After the central
RiskManager gate landed, the flag must actually disable the check.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.analysis.news_filter import NewsEvent, NewsEventFilter
from src.config.schema import AppConfig, SignalParserConfig
from src.core.enums import SignalAction
from src.core.events import EventBus
from src.core.exceptions import RiskLimitExceeded
from src.core.models import AccountState, Signal
from src.risk.manager import RiskManager

NFP = datetime(2026, 2, 6, 13, 30, tzinfo=timezone.utc)


def _build_filter() -> NewsEventFilter:
    nf = NewsEventFilter.__new__(NewsEventFilter)
    nf._block_before = timedelta(minutes=15)
    nf._block_after = timedelta(minutes=30)
    nf._events = [NewsEvent("Non-Farm Payrolls (NFP)", NFP)]
    return nf


def _build_signal(ts: datetime) -> Signal:
    return Signal(
        source="telegram:test",
        symbol="XAUUSD",
        action=SignalAction.BUY,
        strength=0.9,
        timestamp=ts,
        entry_price=2650.0,
        stop_loss=2645.0,
        take_profit=2660.0,
    )


def _account() -> AccountState:
    return AccountState(
        balance=5000.0,
        equity=5000.0,
        margin=0.0,
        free_margin=5000.0,
        margin_level=0.0,
        profit=0.0,
        timestamp=datetime.now(timezone.utc),
    )


def _build_risk_manager(news_filter_enabled: bool, news_filter: NewsEventFilter):
    config = AppConfig()
    # Default risk caps don't trigger here — only the news gate is exercised.
    config.signal_parser = SignalParserConfig(news_filter_enabled=news_filter_enabled)
    rm = RiskManager(
        config=config,
        event_bus=EventBus(),
        symbol_info_func=lambda s: {"spread": 1, "max_spread": 100},
        account_state_func=_account,
        positions_func=lambda s: [],
        news_filter=news_filter,
    )
    return rm


def test_news_window_rejects_when_enabled():
    rm = _build_risk_manager(news_filter_enabled=True, news_filter=_build_filter())
    sig = _build_signal(NFP - timedelta(minutes=5))
    with pytest.raises(RiskLimitExceeded) as exc:
        rm._validate_risk_limits(sig)
    assert exc.value.limit_name == "news_window"


def test_flag_disabled_skips_news_check():
    """news_filter_enabled=False → gate is bypassed even with filter present."""
    rm = _build_risk_manager(news_filter_enabled=False, news_filter=_build_filter())
    sig = _build_signal(NFP - timedelta(minutes=5))
    # Should NOT raise news_window — other limits with empty positions list pass.
    rm._validate_risk_limits(sig)


def test_no_filter_skips_news_check():
    """If news_filter is None, the gate is also skipped."""
    rm = _build_risk_manager(news_filter_enabled=True, news_filter=None)
    sig = _build_signal(NFP - timedelta(minutes=5))
    rm._validate_risk_limits(sig)


def test_signal_outside_window_passes():
    rm = _build_risk_manager(news_filter_enabled=True, news_filter=_build_filter())
    sig = _build_signal(NFP - timedelta(hours=2))
    rm._validate_risk_limits(sig)
