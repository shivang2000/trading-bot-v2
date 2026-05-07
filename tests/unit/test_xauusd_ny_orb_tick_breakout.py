"""Tests for XauusdNyOrbTickBreakout — bar-armed, tick-fired strategy."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import numpy as np
import pandas as pd
import pytest

from src.analysis.strategies.xauusd_ny_orb_tick_breakout import (
    OrbPhase,
    XauusdNyOrbConfig,
    XauusdNyOrbTickBreakout,
)
from src.core.enums import OrderSide, OrderType
from src.core.events import EventBus, OrderEvent
from src.core.models import Order, Tick


# ---------- helpers ----------


def _utc(year=2026, month=5, day=7, hour=13, minute=30, second=0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _m1_bars_ending_at(end_ts: datetime, count: int = 30, low: float = 3990.0, high: float = 4000.0, step: float = 0.5) -> pd.DataFrame:
    """Build a synthetic M1 OHLCV DataFrame ending at end_ts.

    Defaults produce bars that all stay inside [low, high] — useful for
    consolidation tests. Override `low`/`high` to push the range during
    capture.
    """
    rows = []
    for i in range(count):
        ts = end_ts - timedelta(minutes=count - 1 - i)
        # Tight bars near the midpoint, varying slightly so ATR > 0.
        mid = (low + high) / 2
        o = mid + ((-1) ** i) * step
        c = mid + ((-1) ** (i + 1)) * step
        h = max(o, c) + step
        l = min(o, c) - step
        rows.append((ts, o, h, l, c, 1.0, 100, 0))
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume", "tick_volume", "spread"])


def _m5_bars_ending_at(end_ts: datetime, count: int = 30, atr_target: float = 1.0) -> pd.DataFrame:
    rows = []
    base = 4000.0
    for i in range(count):
        ts = end_ts - timedelta(minutes=5 * (count - 1 - i))
        o = base + (i % 3) * 0.1
        c = base + ((i + 1) % 3) * 0.1
        h = max(o, c) + atr_target / 2.0
        l = min(o, c) - atr_target / 2.0
        rows.append((ts, o, h, l, c, 1.0, 100, 0))
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume", "tick_volume", "spread"])


def _d1_bars(prev_high: float = 4010.0, prev_low: float = 3985.0) -> pd.DataFrame:
    """2 daily bars; second-to-last is the 'previous day' the strategy snaps."""
    return pd.DataFrame(
        [
            (_utc(day=6), 3990.0, prev_high, prev_low, 4000.0, 1.0, 100, 0),
            (_utc(day=7), 4000.0, 4005.0, 3995.0, 4002.0, 1.0, 100, 0),
        ],
        columns=["time", "open", "high", "low", "close", "volume", "tick_volume", "spread"],
    )


def _make_strategy(
    config: XauusdNyOrbConfig | None = None,
    is_news_blackout: Callable[[datetime], bool] = lambda _ts: False,
    is_health_suspended: Callable[[], bool] = lambda: False,
    atr_expansion: Callable[[float], bool] = lambda atr: False,
):
    bus = EventBus()
    captured: list[OrderEvent] = []

    async def _capture(event):
        captured.append(event)

    bus.subscribe("ORDER", _capture)

    cfg = config or XauusdNyOrbConfig(enabled=True)
    strat = XauusdNyOrbTickBreakout(
        symbol="XAUUSD",
        config=cfg,
        event_bus=bus,
        position_sizer=lambda mid, sl_dist: 0.10,
        is_news_blackout=is_news_blackout,
        is_health_suspended=is_health_suspended,
        atr_expansion_guard=atr_expansion,
    )
    return strat, captured, bus


async def _drain(bus: EventBus) -> None:
    """Run any queued events through registered handlers."""
    await bus.drain()


# ---------- tests ----------


class TestPdhPdlSnapshot:
    def test_d1_close_populates_pdh_pdl(self) -> None:
        strat, _, _bus = _make_strategy()
        d1 = _d1_bars(prev_high=4011.5, prev_low=3984.25)
        strat.on_d1_close(d1, _utc(hour=0))
        assert strat.state.pdh == pytest.approx(4011.5)
        assert strat.state.pdl == pytest.approx(3984.25)
        assert strat.state.date_str == "2026-05-07"

    def test_short_d1_history_skipped(self) -> None:
        strat, _, _bus = _make_strategy()
        d1 = _d1_bars().iloc[:1]  # only 1 bar
        strat.on_d1_close(d1, _utc(hour=0))
        assert strat.state.pdh is None


class TestM1RangeCapture:
    def test_capturing_after_ny_open_then_consolidating(self) -> None:
        strat, _, _bus = _make_strategy()
        strat.on_m5_close(_m5_bars_ending_at(_utc(hour=13, minute=29)), _utc(hour=13, minute=29))

        # 13:30 UTC bar — phase transitions AWAITING_NY_OPEN → CAPTURING_RANGE
        bars_at_open = _m1_bars_ending_at(_utc(hour=13, minute=30))
        strat.on_m1_close(bars_at_open, _utc(hour=13, minute=30))
        assert strat.state.phase == OrbPhase.CAPTURING_RANGE

        # 13:35 UTC bar — phase transitions CAPTURING_RANGE → CONSOLIDATING
        bars_at_close = _m1_bars_ending_at(_utc(hour=13, minute=35))
        strat.on_m1_close(bars_at_close, _utc(hour=13, minute=35))
        assert strat.state.phase == OrbPhase.CONSOLIDATING
        assert strat.state.orb_high is not None
        assert strat.state.orb_low is not None
        assert strat.state.orb_high > strat.state.orb_low

    def test_consolidation_arms_after_3_inside_bars(self) -> None:
        cfg = XauusdNyOrbConfig(enabled=True, consolidation_bars=3)
        strat, _, _bus = _make_strategy(cfg)

        strat.on_m5_close(_m5_bars_ending_at(_utc(hour=13, minute=29)), _utc(hour=13, minute=29))
        strat.on_m1_close(_m1_bars_ending_at(_utc(hour=13, minute=30)), _utc(hour=13, minute=30))
        strat.on_m1_close(_m1_bars_ending_at(_utc(hour=13, minute=35)), _utc(hour=13, minute=35))

        # Now in CONSOLIDATING. Force orb_low/high to known values so we can
        # craft 3 inside bars.
        strat.state.orb_high = 4010.0
        strat.state.orb_low = 3995.0
        # 3 bars where last bar is fully inside [3995, 4010]
        for i, m in enumerate([36, 37, 38]):
            bars = _m1_bars_ending_at(_utc(hour=13, minute=m), low=3996.0, high=4009.0, step=0.5)
            strat.on_m1_close(bars, _utc(hour=13, minute=m))
        assert strat.state.phase == OrbPhase.ARMED
        assert strat.state.consolidation_bars_seen == 3

    def test_pre_consolidation_breakout_blocks(self) -> None:
        cfg = XauusdNyOrbConfig(enabled=True, consolidation_bars=3)
        strat, _, _bus = _make_strategy(cfg)

        strat.on_m5_close(_m5_bars_ending_at(_utc(hour=13, minute=29)), _utc(hour=13, minute=29))
        strat.on_m1_close(_m1_bars_ending_at(_utc(hour=13, minute=30)), _utc(hour=13, minute=30))
        strat.on_m1_close(_m1_bars_ending_at(_utc(hour=13, minute=35)), _utc(hour=13, minute=35))
        strat.state.orb_high = 4010.0
        strat.state.orb_low = 3995.0

        # Outside-range bar during consolidation
        bars_outside = _m1_bars_ending_at(_utc(hour=13, minute=36), low=4011.0, high=4020.0, step=0.5)
        strat.on_m1_close(bars_outside, _utc(hour=13, minute=36))
        assert strat.state.phase == OrbPhase.BLOCKED


class TestPdhPdlConfluence:
    def test_orb_high_near_pdh_boosts_score(self) -> None:
        strat, _, _bus = _make_strategy()
        # PDH/PDL snapshot
        strat.on_d1_close(_d1_bars(prev_high=4010.0, prev_low=3990.0), _utc(hour=0))
        # M5 ATR snapshot first so confluence math works
        strat.on_m5_close(_m5_bars_ending_at(_utc(hour=13, minute=29), atr_target=2.0), _utc(hour=13, minute=29))
        # Range capture with orb_high = 4011 (within 30% × 2.0 = 0.6 of PDH=4010)
        strat.on_m1_close(_m1_bars_ending_at(_utc(hour=13, minute=30)), _utc(hour=13, minute=30))
        strat.state.orb_high = 4010.5
        strat.state.orb_low = 3990.0
        strat.on_m1_close(_m1_bars_ending_at(_utc(hour=13, minute=35)), _utc(hour=13, minute=35))
        assert strat.state.confluence_score > 0


class TestTickFiresEntry:
    @pytest.mark.asyncio
    async def test_breaks_above_orb_high_with_velocity_fires(self) -> None:
        cfg = XauusdNyOrbConfig(enabled=True, velocity_window_ticks=3, velocity_atr_mult=0.01)
        strat, captured, bus = _make_strategy(cfg)

        # Manually arm the strategy
        strat.state.phase = OrbPhase.ARMED
        strat.state.orb_high = 4010.0
        strat.state.orb_low = 3990.0
        strat.state.last_atr_m1 = 1.0
        strat.state.last_atr_m5 = 1.0
        strat.state.date_str = "2026-05-07"

        base = _utc(hour=14, minute=0, second=0)
        # Build 3 ticks where last crosses ORB high with strong velocity
        for i, price in enumerate([4009.0, 4010.0, 4012.0]):
            tick = Tick(symbol="XAUUSD", timestamp=base + timedelta(seconds=i),
                       bid=price - 0.01, ask=price + 0.01, last=price, volume=1.0)
            await strat.on_tick(tick)

        # Wait for async dispatch
        await _drain(bus)
        assert any(c.order is not None and c.order.side == OrderSide.BUY for c in captured)
        assert strat.state.phase == OrbPhase.FIRED
        assert strat.state.entries_today == 1

    @pytest.mark.asyncio
    async def test_breaks_below_orb_low_with_velocity_fires_short(self) -> None:
        cfg = XauusdNyOrbConfig(enabled=True, velocity_window_ticks=3, velocity_atr_mult=0.01)
        strat, captured, bus = _make_strategy(cfg)

        strat.state.phase = OrbPhase.ARMED
        strat.state.orb_high = 4010.0
        strat.state.orb_low = 3990.0
        strat.state.last_atr_m1 = 1.0
        strat.state.last_atr_m5 = 1.0
        strat.state.date_str = "2026-05-07"

        base = _utc(hour=14, minute=0)
        for i, price in enumerate([3991.0, 3990.0, 3988.0]):
            tick = Tick(symbol="XAUUSD", timestamp=base + timedelta(seconds=i),
                       bid=price - 0.01, ask=price + 0.01, last=price, volume=1.0)
            await strat.on_tick(tick)

        await _drain(bus)
        assert any(c.order is not None and c.order.side == OrderSide.SELL for c in captured)

    @pytest.mark.asyncio
    async def test_low_velocity_does_not_fire(self) -> None:
        cfg = XauusdNyOrbConfig(enabled=True, velocity_window_ticks=3, velocity_atr_mult=10.0)
        strat, captured, bus = _make_strategy(cfg)

        strat.state.phase = OrbPhase.ARMED
        strat.state.orb_high = 4010.0
        strat.state.orb_low = 3990.0
        strat.state.last_atr_m1 = 1.0
        strat.state.last_atr_m5 = 1.0
        strat.state.date_str = "2026-05-07"

        base = _utc(hour=14, minute=0)
        for i, price in enumerate([4010.0, 4010.005, 4010.01]):  # tiny velocity
            tick = Tick(symbol="XAUUSD", timestamp=base + timedelta(seconds=i),
                       bid=price - 0.01, ask=price + 0.01, last=price, volume=1.0)
            await strat.on_tick(tick)

        await _drain(bus)
        assert captured == []
        assert strat.state.phase == OrbPhase.ARMED  # still armed

    @pytest.mark.asyncio
    async def test_health_suspended_blocks_entry(self) -> None:
        cfg = XauusdNyOrbConfig(enabled=True, velocity_window_ticks=3, velocity_atr_mult=0.01)
        strat, captured, bus = _make_strategy(cfg, is_health_suspended=lambda: True)

        strat.state.phase = OrbPhase.ARMED
        strat.state.orb_high = 4010.0
        strat.state.orb_low = 3990.0
        strat.state.last_atr_m1 = 1.0
        strat.state.last_atr_m5 = 1.0
        strat.state.date_str = "2026-05-07"

        base = _utc(hour=14, minute=0)
        for i, price in enumerate([4009.0, 4010.0, 4012.0]):
            tick = Tick(symbol="XAUUSD", timestamp=base + timedelta(seconds=i),
                       bid=price - 0.01, ask=price + 0.01, last=price, volume=1.0)
            await strat.on_tick(tick)
        assert captured == []

    @pytest.mark.asyncio
    async def test_news_blackout_blocks_entry(self) -> None:
        cfg = XauusdNyOrbConfig(enabled=True, velocity_window_ticks=3, velocity_atr_mult=0.01)
        strat, captured, bus = _make_strategy(cfg, is_news_blackout=lambda _ts: True)

        strat.state.phase = OrbPhase.ARMED
        strat.state.orb_high = 4010.0
        strat.state.orb_low = 3990.0
        strat.state.last_atr_m1 = 1.0
        strat.state.last_atr_m5 = 1.0
        strat.state.date_str = "2026-05-07"

        base = _utc(hour=14, minute=0)
        for i, price in enumerate([4009.0, 4010.0, 4012.0]):
            tick = Tick(symbol="XAUUSD", timestamp=base + timedelta(seconds=i),
                       bid=price - 0.01, ask=price + 0.01, last=price, volume=1.0)
            await strat.on_tick(tick)
        assert captured == []

    @pytest.mark.asyncio
    async def test_atr_expansion_blocks_entry(self) -> None:
        cfg = XauusdNyOrbConfig(enabled=True, velocity_window_ticks=3, velocity_atr_mult=0.01)
        strat, captured, bus = _make_strategy(cfg, atr_expansion=lambda atr: True)

        strat.state.phase = OrbPhase.ARMED
        strat.state.orb_high = 4010.0
        strat.state.orb_low = 3990.0
        strat.state.last_atr_m1 = 1.0
        strat.state.last_atr_m5 = 1.0
        strat.state.date_str = "2026-05-07"

        base = _utc(hour=14, minute=0)
        for i, price in enumerate([4009.0, 4010.0, 4012.0]):
            tick = Tick(symbol="XAUUSD", timestamp=base + timedelta(seconds=i),
                       bid=price - 0.01, ask=price + 0.01, last=price, volume=1.0)
            await strat.on_tick(tick)
        assert captured == []

    @pytest.mark.asyncio
    async def test_daily_rollover_window_blocks(self) -> None:
        cfg = XauusdNyOrbConfig(enabled=True, velocity_window_ticks=3, velocity_atr_mult=0.01)
        strat, captured, bus = _make_strategy(cfg)

        strat.state.phase = OrbPhase.ARMED
        strat.state.orb_high = 4010.0
        strat.state.orb_low = 3990.0
        strat.state.last_atr_m1 = 1.0
        strat.state.last_atr_m5 = 1.0
        strat.state.date_str = "2026-05-07"

        base = _utc(hour=21, minute=15)  # inside 21:00-22:00 rollover block
        for i, price in enumerate([4009.0, 4010.0, 4012.0]):
            tick = Tick(symbol="XAUUSD", timestamp=base + timedelta(seconds=i),
                       bid=price - 0.01, ask=price + 0.01, last=price, volume=1.0)
            await strat.on_tick(tick)
        assert captured == []
        assert strat.state.phase == OrbPhase.ARMED  # still armed, just blocked this window

    @pytest.mark.asyncio
    async def test_stale_breakout_already_past_buffer_skipped(self) -> None:
        cfg = XauusdNyOrbConfig(enabled=True, velocity_window_ticks=3, velocity_atr_mult=0.01, stale_buffer_pips=3.0)
        strat, captured, bus = _make_strategy(cfg)

        strat.state.phase = OrbPhase.ARMED
        strat.state.orb_high = 4010.0
        strat.state.orb_low = 3990.0
        strat.state.last_atr_m1 = 1.0
        strat.state.last_atr_m5 = 1.0
        strat.state.date_str = "2026-05-07"

        # 3-pip stale buffer at point=0.01 ⇒ price > 4010.03 is "stale"
        base = _utc(hour=14, minute=0)
        for i, price in enumerate([4015.0, 4015.5, 4016.0]):  # already way past ORB high + buffer
            tick = Tick(symbol="XAUUSD", timestamp=base + timedelta(seconds=i),
                       bid=price - 0.01, ask=price + 0.01, last=price, volume=1.0)
            await strat.on_tick(tick)
        assert captured == []


class TestSlPlacement:
    @pytest.mark.asyncio
    async def test_buy_sl_uses_closer_of_orb_or_atr(self) -> None:
        cfg = XauusdNyOrbConfig(
            enabled=True, velocity_window_ticks=3, velocity_atr_mult=0.01,
            sl_atr_m1_mult=1.0,
        )
        strat, captured, bus = _make_strategy(cfg)

        strat.state.phase = OrbPhase.ARMED
        strat.state.orb_high = 4010.0
        strat.state.orb_low = 3990.0   # 20 below entry
        strat.state.last_atr_m1 = 5.0  # ATR-based SL = entry - 5
        strat.state.last_atr_m5 = 1.0
        strat.state.date_str = "2026-05-07"

        base = _utc(hour=14, minute=0)
        for i, price in enumerate([4009.0, 4010.0, 4011.0]):
            tick = Tick(symbol="XAUUSD", timestamp=base + timedelta(seconds=i),
                       bid=price - 0.01, ask=price + 0.01, last=price, volume=1.0)
            await strat.on_tick(tick)
        await _drain(bus)

        assert len(captured) == 1
        order = captured[0].order
        # entry mid ≈ 4011; ATR-SL = 4006; ORB-SL = 3990; closer (higher) is 4006
        assert order.stop_loss == pytest.approx(4006.0, abs=0.5)
