"""Tests for tick-driven partial profit evaluation.

`evaluate_on_tick()` is a thin alias around `check()` — these tests pin
that contract so future refactors of either method don't desync them.
The shared state (`_tracked`) means a tick-driven hit must be visible
to the poll-driven `check()` call (idempotence across triggers).
"""

from src.core.enums import OrderSide
from src.monitoring.partial_profit_manager import PartialProfitManager


def _new_manager() -> PartialProfitManager:
    return PartialProfitManager(breakeven_buffer=1.0)


def test_evaluate_on_tick_returns_same_actions_as_check():
    """Same inputs → same outputs."""
    pm_a = _new_manager()
    pm_b = _new_manager()
    pm_a.register(
        ticket=1, side=OrderSide.BUY, volume=0.30,
        entry_price=4000.0, tp_levels=[4010.0, 4020.0, 4030.0],
    )
    pm_b.register(
        ticket=1, side=OrderSide.BUY, volume=0.30,
        entry_price=4000.0, tp_levels=[4010.0, 4020.0, 4030.0],
    )

    poll_actions = pm_a.check(1, 4015.0, "XAUUSD")
    tick_actions = pm_b.evaluate_on_tick(1, 4015.0, "XAUUSD")

    assert len(poll_actions) == len(tick_actions) == 1
    a, b = poll_actions[0], tick_actions[0]
    assert (a.ticket, a.close_volume, a.new_sl, a.level_idx) == \
           (b.ticket, b.close_volume, b.new_sl, b.level_idx)


def test_tick_hit_is_visible_to_subsequent_poll_check():
    """If tick engine fires TP1, the next poll must NOT re-fire it.

    This is the regression we'd see if `_tracked` got separated between
    the two entry points — TP1 would close twice (ouch).
    """
    pm = _new_manager()
    pm.register(
        ticket=42, side=OrderSide.BUY, volume=0.30,
        entry_price=4000.0, tp_levels=[4010.0, 4020.0, 4030.0],
    )

    first = pm.evaluate_on_tick(42, 4015.0, "XAUUSD")
    assert len(first) == 1
    assert first[0].level_idx == 0  # TP1 hit

    # Same price arrives via poll path — must be a no-op
    second = pm.check(42, 4015.0, "XAUUSD")
    assert second == []


def test_evaluate_on_tick_handles_gap_through_multiple_levels():
    """Gold sometimes spikes through TP1 + TP2 in one tick — both fire."""
    pm = _new_manager()
    pm.register(
        ticket=7, side=OrderSide.BUY, volume=0.30,
        entry_price=4000.0, tp_levels=[4010.0, 4020.0, 4030.0],
    )

    actions = pm.evaluate_on_tick(7, 4025.0, "XAUUSD")
    # TP1 + TP2 both hit; TP3 (final) is left for MT5's own TP to handle
    assert [a.level_idx for a in actions] == [0, 1]


def test_evaluate_on_tick_skip_when_not_tracked():
    """Tick for an unregistered ticket is a cheap no-op."""
    pm = _new_manager()
    assert pm.evaluate_on_tick(999, 4000.0, "XAUUSD") == []


def test_sell_side_partial_on_tick():
    """SELL positions fire when current_price <= TP."""
    pm = _new_manager()
    pm.register(
        ticket=11, side=OrderSide.SELL, volume=0.30,
        entry_price=4000.0, tp_levels=[3990.0, 3980.0, 3970.0],
    )

    actions = pm.evaluate_on_tick(11, 3985.0, "XAUUSD")
    assert len(actions) == 1
    assert actions[0].level_idx == 0
    # New SL should be entry+buffer (breakeven for SELL = 4000 - 1.0 = 3999.0)
    assert actions[0].new_sl == 3999.0
