"""Tests for StrategyHealthMonitor — 8 early-warning signals."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.monitoring.strategy_health_monitor import (
    HealthAction,
    HealthAlert,
    HealthSignal,
    HealthThresholds,
    StrategyHealthMonitor,
)


def _ts(seconds: int = 0) -> datetime:
    return datetime(2026, 5, 7, 12, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)


def _make(thresholds: HealthThresholds | None = None, dd_limit: float = 250.0) -> tuple[StrategyHealthMonitor, list[HealthAlert]]:
    captured: list[HealthAlert] = []
    mon = StrategyHealthMonitor(
        thresholds=thresholds,
        alert_callback=captured.append,
        daily_dd_limit_usd=dd_limit,
    )
    return mon, captured


# ----- Spread regime -----


class TestSpreadRegime:
    def test_no_alert_under_baseline(self) -> None:
        mon, captured = _make(HealthThresholds(spread_baseline_window=10, spread_consecutive_breach=2))
        for i in range(15):
            mon.on_trade_closed(_ts(i), pnl=1.0, hold_seconds=180, spread_at_entry_points=2.0, slippage_points=0.0)
        assert all(a.signal != HealthSignal.SPREAD_REGIME for a in captured)

    def test_fires_after_consecutive_breaches(self) -> None:
        t = HealthThresholds(spread_baseline_window=10, spread_consecutive_breach=3, spread_multiplier=1.5)
        mon, captured = _make(t)
        # Fill baseline at spread=2.0
        for i in range(10):
            mon.on_trade_closed(_ts(i), pnl=1.0, hold_seconds=180, spread_at_entry_points=2.0, slippage_points=0.0)
        # 3 consecutive breaches at spread=4.0 (2x baseline)
        mon.on_trade_closed(_ts(11), pnl=1.0, hold_seconds=180, spread_at_entry_points=4.0, slippage_points=0.0)
        mon.on_trade_closed(_ts(12), pnl=1.0, hold_seconds=180, spread_at_entry_points=4.0, slippage_points=0.0)
        alerts = mon.on_trade_closed(_ts(13), pnl=1.0, hold_seconds=180, spread_at_entry_points=4.0, slippage_points=0.0)
        assert any(a.signal == HealthSignal.SPREAD_REGIME for a in alerts)
        assert mon.is_entries_suspended()

    def test_resets_on_normal_spread(self) -> None:
        t = HealthThresholds(spread_baseline_window=10, spread_consecutive_breach=3, spread_multiplier=1.5)
        mon, _ = _make(t)
        for i in range(10):
            mon.on_trade_closed(_ts(i), pnl=1.0, hold_seconds=180, spread_at_entry_points=2.0, slippage_points=0.0)
        mon.on_trade_closed(_ts(11), pnl=1.0, hold_seconds=180, spread_at_entry_points=4.0, slippage_points=0.0)
        mon.on_trade_closed(_ts(12), pnl=1.0, hold_seconds=180, spread_at_entry_points=2.0, slippage_points=0.0)  # reset
        mon.on_trade_closed(_ts(13), pnl=1.0, hold_seconds=180, spread_at_entry_points=4.0, slippage_points=0.0)
        # Only 1 breach in current streak — not enough to fire
        assert not mon.is_entries_suspended()


# ----- Slippage drift -----


class TestSlippageDrift:
    def test_fires_when_avg_exceeds_threshold(self) -> None:
        t = HealthThresholds(slippage_window=5, slippage_avg_points_max=5.0)
        mon, captured = _make(t)
        for i in range(5):
            mon.on_trade_closed(_ts(i), pnl=1.0, hold_seconds=180, spread_at_entry_points=2.0, slippage_points=10.0)
        assert any(a.signal == HealthSignal.SLIPPAGE_DRIFT for a in captured)

    def test_no_fire_below_threshold(self) -> None:
        t = HealthThresholds(slippage_window=5, slippage_avg_points_max=5.0)
        mon, captured = _make(t)
        for i in range(5):
            mon.on_trade_closed(_ts(i), pnl=1.0, hold_seconds=180, spread_at_entry_points=2.0, slippage_points=2.0)
        assert all(a.signal != HealthSignal.SLIPPAGE_DRIFT for a in captured)


# ----- Modify rejection -----


class TestModifyRejection:
    def test_fires_at_threshold_pct(self) -> None:
        t = HealthThresholds(modify_window=20, modify_rejection_pct_max=5.0)
        mon, captured = _make(t)
        # 19 successes + 1 rejection = 5% — at boundary, not over
        for _ in range(19):
            mon.on_modify_attempt(success=True)
        mon.on_modify_attempt(success=False)
        assert all(a.signal != HealthSignal.MODIFY_REJECTION for a in captured)
        # Add one more rejection; now 2/21 ~ 9.5% but the deque only holds 20.
        # After 21st event the deque drops the first success and contains
        # 18 success + 2 reject = 10% which exceeds 5%.
        mon.on_modify_attempt(success=False)
        assert any(a.signal == HealthSignal.MODIFY_REJECTION for a in captured)

    def test_warmup_suppresses(self) -> None:
        t = HealthThresholds(modify_window=20)
        mon, captured = _make(t)
        for _ in range(5):
            mon.on_modify_attempt(success=False)
        assert captured == []  # window not full yet


# ----- ATR expansion -----


class TestAtrExpansion:
    def test_fires_on_spike_vs_baseline(self) -> None:
        t = HealthThresholds(atr_session_window=10, atr_expansion_multiplier=2.0)
        mon, captured = _make(t)
        for _ in range(10):
            mon.on_atr_snapshot(0.5)
        alert = mon.on_atr_snapshot(2.0)  # 4x median
        assert alert is not None
        assert alert.signal == HealthSignal.ATR_EXPANSION
        assert mon.is_entries_suspended()

    def test_warmup_suppresses(self) -> None:
        t = HealthThresholds(atr_session_window=10)
        mon, captured = _make(t)
        # Only 5 samples — under warm-up window
        for _ in range(5):
            mon.on_atr_snapshot(0.5)
        # Spike won't fire because we don't have a full baseline yet
        assert mon.on_atr_snapshot(5.0) is None


# ----- WR degradation -----


class TestWrDegradation:
    def test_fires_below_floor(self) -> None:
        t = HealthThresholds(wr_window=10, wr_floor_pct=50.0)
        mon, captured = _make(t)
        # 3 wins / 10 = 30%, below 50% floor
        for i in range(3):
            mon.on_trade_closed(_ts(i), pnl=1.0, hold_seconds=180, spread_at_entry_points=2.0, slippage_points=0.0)
        for i in range(7):
            mon.on_trade_closed(_ts(10 + i), pnl=-1.0, hold_seconds=180, spread_at_entry_points=2.0, slippage_points=0.0)
        assert any(a.signal == HealthSignal.WR_DEGRADATION for a in captured)

    def test_no_fire_above_floor(self) -> None:
        t = HealthThresholds(wr_window=10, wr_floor_pct=40.0)
        mon, captured = _make(t)
        for i in range(6):
            mon.on_trade_closed(_ts(i), pnl=1.0, hold_seconds=180, spread_at_entry_points=2.0, slippage_points=0.0)
        for i in range(4):
            mon.on_trade_closed(_ts(10 + i), pnl=-1.0, hold_seconds=180, spread_at_entry_points=2.0, slippage_points=0.0)
        assert all(a.signal != HealthSignal.WR_DEGRADATION for a in captured)


# ----- Hold-time floor -----


class TestHoldTimeFloor:
    def test_fires_when_too_many_quick_exits(self) -> None:
        t = HealthThresholds(hold_time_window=10, hold_time_floor_seconds=120, hold_time_breach_pct_max=10.0)
        mon, captured = _make(t)
        # 2 of 10 trades hold < 120s = 20%, breach 10% threshold
        for i in range(2):
            mon.on_trade_closed(_ts(i), pnl=1.0, hold_seconds=60, spread_at_entry_points=2.0, slippage_points=0.0)
        for i in range(8):
            mon.on_trade_closed(_ts(10 + i), pnl=1.0, hold_seconds=300, spread_at_entry_points=2.0, slippage_points=0.0)
        assert any(a.signal == HealthSignal.HOLD_TIME_FLOOR for a in captured)

    def test_no_fire_when_holds_pass_floor(self) -> None:
        t = HealthThresholds(hold_time_window=10, hold_time_floor_seconds=120)
        mon, captured = _make(t)
        for i in range(10):
            mon.on_trade_closed(_ts(i), pnl=1.0, hold_seconds=300, spread_at_entry_points=2.0, slippage_points=0.0)
        assert all(a.signal != HealthSignal.HOLD_TIME_FLOOR for a in captured)


# ----- Daily DD proximity -----


class TestDdProximity:
    def test_fires_at_threshold(self) -> None:
        t = HealthThresholds(dd_proximity_pct_of_limit=60.0)
        mon, captured = _make(t, dd_limit=250.0)
        # $150 dd = 60% of $250 limit → fires
        alert = mon.on_floating_dd(150.0)
        assert alert is not None
        assert alert.signal == HealthSignal.DD_PROXIMITY
        assert alert.action == HealthAction.AUTO_FLAT

    def test_no_fire_below_threshold(self) -> None:
        mon, captured = _make(dd_limit=250.0)
        assert mon.on_floating_dd(100.0) is None  # 40% of limit

    def test_no_fire_when_no_limit_configured(self) -> None:
        mon, captured = _make(dd_limit=0.0)
        assert mon.on_floating_dd(500.0) is None


# ----- Trade frequency -----


class TestTradeFrequency:
    def test_fires_on_burst(self) -> None:
        t = HealthThresholds(trade_frequency_window_minutes=15, trade_frequency_max_entries=3)
        mon, captured = _make(t)
        for i in range(4):
            mon.on_entry("XAUUSD", _ts(i * 60))  # 4 entries in 4 minutes
        assert any(a.signal == HealthSignal.TRADE_FREQUENCY for a in captured)

    def test_no_fire_when_window_expired(self) -> None:
        t = HealthThresholds(trade_frequency_window_minutes=5, trade_frequency_max_entries=3)
        mon, captured = _make(t)
        # 3 entries spread out beyond window
        mon.on_entry("XAUUSD", _ts(0))
        mon.on_entry("XAUUSD", _ts(400))   # 6.7 min later — first dropped
        mon.on_entry("XAUUSD", _ts(800))
        mon.on_entry("XAUUSD", _ts(1200))
        assert all(a.signal != HealthSignal.TRADE_FREQUENCY for a in captured)

    def test_per_symbol_isolation(self) -> None:
        t = HealthThresholds(trade_frequency_window_minutes=15, trade_frequency_max_entries=3)
        mon, captured = _make(t)
        for i in range(4):
            mon.on_entry("XAUUSD", _ts(i * 60))
        # Reset captured before firing on another symbol — we already know XAUUSD fires
        captured.clear()
        for i in range(2):
            mon.on_entry("US30", _ts(i * 60))
        assert all(a.signal != HealthSignal.TRADE_FREQUENCY for a in captured)


# ----- Latching + clearing -----


class TestLatching:
    def test_suspend_does_not_re_emit(self) -> None:
        t = HealthThresholds(slippage_window=3, slippage_avg_points_max=5.0)
        mon, captured = _make(t)
        # First trip
        for i in range(3):
            mon.on_trade_closed(_ts(i), pnl=1.0, hold_seconds=180, spread_at_entry_points=2.0, slippage_points=10.0)
        first_count = sum(1 for a in captured if a.signal == HealthSignal.SLIPPAGE_DRIFT)
        # Second trip (still bad)
        for i in range(3):
            mon.on_trade_closed(_ts(10 + i), pnl=1.0, hold_seconds=180, spread_at_entry_points=2.0, slippage_points=10.0)
        second_count = sum(1 for a in captured if a.signal == HealthSignal.SLIPPAGE_DRIFT)
        assert second_count == first_count == 1

    def test_clear_latched_resets(self) -> None:
        mon, _ = _make(dd_limit=250.0)
        mon.on_floating_dd(200.0)  # fires
        assert mon.is_entries_suspended()
        mon.clear_latched()
        assert not mon.is_entries_suspended()


# ----- Stats -----


def test_stats_reports_state() -> None:
    mon, _ = _make()
    mon.on_trade_closed(_ts(0), pnl=1.0, hold_seconds=180, spread_at_entry_points=2.0, slippage_points=0.0)
    mon.on_modify_attempt(success=True)
    mon.on_atr_snapshot(0.5)
    mon.on_entry("XAUUSD", _ts(0))
    s = mon.stats
    assert s["trades_seen"] == 1
    assert s["modify_attempts_seen"] == 1
    assert s["atr_snapshots"] == 1
    assert s["entry_windows"] == {"XAUUSD": 1}
