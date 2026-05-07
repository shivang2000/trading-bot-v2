"""Pullback window state-machine tick-replay adapter.

Mirror of `ny_orb_replay_adapter.py` for the EMA pullback window strategy.
Only differences:
  - Bar lifecycle: M5 only (no M1, no D1)
  - Strategy class: XauusdPullbackWindowStateMachine

Produces same JSONL trade output for use by `scripts/backtest_evidence_gated.py`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from src.analysis.strategies.xauusd_pullback_window_state_machine import (
    XauusdPullbackWindowConfig,
    XauusdPullbackWindowStateMachine,
)
from src.backtesting.ny_orb_replay_adapter import (
    ClosedTrade,
    _FillSimulator,
    _MultiTimeframeAggregator,
    _stream_tick_files,
)
from src.core.enums import OrderSide
from src.core.events import EventBus, OrderEvent
from src.core.models import Order, Tick

logger = logging.getLogger(__name__)


@dataclass
class PullbackReplayResult:
    ticks_processed: int = 0
    orders_received: int = 0
    closed_trades: list[ClosedTrade] = field(default_factory=list)
    final_pnl: float = 0.0
    days_covered: int = 0

    def write_jsonl(self, path: Path) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as fh:
            for t in self.closed_trades:
                fh.write(t.to_jsonl())
                fh.write("\n")
        return len(self.closed_trades)


async def run_pullback_replay(
    tick_files: list[Path],
    config: XauusdPullbackWindowConfig | None = None,
    risk_per_trade_pct: float = 1.0,
    starting_balance: float = 10000.0,
    log_every_ticks: int = 1_000_000,
) -> PullbackReplayResult:
    """Run pullback window strategy over tick CSVs and return aggregated trades."""
    cfg = config or XauusdPullbackWindowConfig(enabled=True)
    bus = EventBus()
    pending_orders: list[Order] = []

    async def _capture(event):
        if isinstance(event, OrderEvent) and event.order is not None:
            pending_orders.append(event.order)

    bus.subscribe("ORDER", _capture)

    def position_sizer(_mid: float, sl_distance: float) -> float:
        risk_usd = starting_balance * (risk_per_trade_pct / 100.0)
        lot = risk_usd / (sl_distance * 100.0) if sl_distance > 0 else 0.0
        return max(0.01, round(lot, 2))

    strat = XauusdPullbackWindowStateMachine(
        symbol="XAUUSD",
        config=cfg,
        event_bus=bus,
        position_sizer=position_sizer,
        is_news_blackout=lambda _ts: False,
        is_health_suspended=lambda: False,
    )

    aggregator = _MultiTimeframeAggregator(history_bars=300)

    def _on_m5(rows: list[dict], ts: datetime) -> None:
        if rows:
            strat.on_m5_close(pd.DataFrame(rows), ts)

    # Pullback strategy ignores M1 + D1; pass no-ops for those callbacks
    aggregator.set_callbacks(
        on_m1_close=lambda rows, ts: None,
        on_m5_close=_on_m5,
        on_d1_close=lambda rows, ts: None,
    )

    sim = _FillSimulator()
    result = PullbackReplayResult()
    days_seen: set = set()
    last_price = 0.0
    last_ts: datetime | None = None

    for ts, bid, ask in _stream_tick_files(tick_files):
        mid = (bid + ask) / 2.0
        last_price = mid
        last_ts = ts
        days_seen.add(ts.date())

        aggregator.push(ts, mid, volume=0.0)
        await strat.on_tick(Tick(symbol="XAUUSD", timestamp=ts, bid=bid, ask=ask, last=mid, volume=0.0))
        await bus.drain()

        for order in pending_orders:
            entry = ask if order.side == OrderSide.BUY else bid
            sim.open(order, ts, entry)
            result.orders_received += 1
        pending_orders.clear()

        sim.update(ts, bid, ask)
        result.ticks_processed += 1
        if result.ticks_processed % log_every_ticks == 0:
            logger.info(
                "Pullback replay: %d ticks, %d orders, %d closed, last=%s",
                result.ticks_processed, result.orders_received,
                len(sim.closed_trades), ts.isoformat(),
            )

    if last_ts is not None:
        sim.force_close_all(last_ts, last_price)

    result.closed_trades = sim.closed_trades
    result.final_pnl = sum(t.pnl for t in result.closed_trades)
    result.days_covered = len(days_seen)
    return result
