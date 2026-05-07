"""NY ORB tick-replay adapter.

Streams ticks from gzipped Dukascopy CSVs, reconstructs M1/M5/D1 bars
on the fly, drives the XauusdNyOrbTickBreakout strategy, simulates fills
on a synthetic account, and emits one JSONL trade per closed position.

Output format (per line):
    {"closed_at": "2026-02-15T14:23:11.500Z", "pnl": 12.5,
     "side": "BUY", "entry": 4012.5, "exit": 4025.0,
     "lot": 0.10, "reason": "TAKE_PROFIT"}

Used by `scripts/backtest_evidence_gated.py` via `--trades-jsonl`.

Architecture:
    Tick CSV (gz)
       │
       v
    [BarAggregator]  →  M1 bars  →  strategy.on_m1_close
                    →  M5 bars  →  strategy.on_m5_close
                    →  D1 bars  →  strategy.on_d1_close
       │
       v
    [Strategy]      →  OrderEvent (via EventBus)
       │
       v
    [FillSimulator] →  open_position(...) on first matching tick
                    →  per-tick SL/TP check
                    →  close_position(...) on hit
       │
       v
    [JSONL writer]  →  one line per closed trade
"""

from __future__ import annotations

import asyncio
import csv
import gzip
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Iterator

from src.analysis.strategies.xauusd_ny_orb_tick_breakout import (
    XauusdNyOrbConfig,
    XauusdNyOrbTickBreakout,
)
from src.core.enums import OrderSide
from src.core.events import EventBus, OrderEvent
from src.core.models import Order, Tick

logger = logging.getLogger(__name__)


# ---------- Bar aggregation ----------


@dataclass
class _BarAccumulator:
    """Aggregates ticks into a single OHLC bar."""

    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def update(self, price: float, volume: float = 0.0) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += volume

    def to_row(self) -> dict:
        return {
            "time": self.start,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "tick_volume": int(self.volume) if self.volume else 0,
            "spread": 0,
        }


class _MultiTimeframeAggregator:
    """Builds M1, M5, D1 bars on demand from a tick stream.

    Fires `on_*_close` callbacks when a bar boundary is crossed. Keeps a
    rolling history of the last N bars per timeframe (DataFrame-friendly).
    """

    def __init__(self, history_bars: int = 250) -> None:
        self._history = history_bars
        self._m1: list[dict] = []
        self._m5: list[dict] = []
        self._d1: list[dict] = []
        self._cur_m1: _BarAccumulator | None = None
        self._cur_m5: _BarAccumulator | None = None
        self._cur_d1: _BarAccumulator | None = None
        self._on_m1_close = lambda bars, ts: None
        self._on_m5_close = lambda bars, ts: None
        self._on_d1_close = lambda bars, ts: None

    def set_callbacks(self, on_m1_close, on_m5_close, on_d1_close) -> None:
        self._on_m1_close = on_m1_close
        self._on_m5_close = on_m5_close
        self._on_d1_close = on_d1_close

    @staticmethod
    def _bucket(ts: datetime, minutes: int) -> tuple[datetime, datetime]:
        floored = ts.replace(second=0, microsecond=0)
        floored = floored.replace(minute=(floored.minute // minutes) * minutes)
        return floored, floored + timedelta(minutes=minutes)

    @staticmethod
    def _day_bucket(ts: datetime) -> tuple[datetime, datetime]:
        start = ts.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)

    def push(self, ts: datetime, price: float, volume: float = 0.0) -> None:
        # M1
        m1_start, m1_end = self._bucket(ts, 1)
        if self._cur_m1 is None or m1_start != self._cur_m1.start:
            if self._cur_m1 is not None:
                self._m1.append(self._cur_m1.to_row())
                if len(self._m1) > self._history:
                    self._m1 = self._m1[-self._history:]
                self._on_m1_close(self._m1, self._cur_m1.start)
            self._cur_m1 = _BarAccumulator(
                start=m1_start, end=m1_end, open=price, high=price, low=price, close=price, volume=volume,
            )
        else:
            self._cur_m1.update(price, volume)

        # M5
        m5_start, m5_end = self._bucket(ts, 5)
        if self._cur_m5 is None or m5_start != self._cur_m5.start:
            if self._cur_m5 is not None:
                self._m5.append(self._cur_m5.to_row())
                if len(self._m5) > self._history:
                    self._m5 = self._m5[-self._history:]
                self._on_m5_close(self._m5, self._cur_m5.start)
            self._cur_m5 = _BarAccumulator(
                start=m5_start, end=m5_end, open=price, high=price, low=price, close=price, volume=volume,
            )
        else:
            self._cur_m5.update(price, volume)

        # D1
        d1_start, d1_end = self._day_bucket(ts)
        if self._cur_d1 is None or d1_start != self._cur_d1.start:
            if self._cur_d1 is not None:
                self._d1.append(self._cur_d1.to_row())
                if len(self._d1) > self._history:
                    self._d1 = self._d1[-self._history:]
                self._on_d1_close(self._d1, self._cur_d1.start)
            self._cur_d1 = _BarAccumulator(
                start=d1_start, end=d1_end, open=price, high=price, low=price, close=price, volume=volume,
            )
        else:
            self._cur_d1.update(price, volume)


# ---------- Fill simulation ----------


@dataclass
class _OpenPosition:
    ticket: int
    side: OrderSide
    entry: float
    sl: float
    tp: float
    lot: float
    opened_at: datetime
    comment: str = ""


@dataclass
class ClosedTrade:
    closed_at: datetime
    opened_at: datetime
    side: OrderSide
    entry: float
    exit: float
    lot: float
    pnl: float
    reason: str  # TAKE_PROFIT | STOP_LOSS | END_OF_DATA
    comment: str

    def to_jsonl(self) -> str:
        return json.dumps({
            "closed_at": self.closed_at.isoformat().replace("+00:00", "Z"),
            "opened_at": self.opened_at.isoformat().replace("+00:00", "Z"),
            "side": self.side.value,
            "entry": round(self.entry, 5),
            "exit": round(self.exit, 5),
            "lot": self.lot,
            "pnl": round(self.pnl, 4),
            "reason": self.reason,
            "comment": self.comment,
        })


class _FillSimulator:
    """Holds open positions and checks each tick for SL/TP hits.

    XAUUSD P&L: (exit - entry) * lot * 100 (contract size 100 oz, $1/point/lot).
    """

    XAUUSD_CONTRACT_SIZE = 100.0

    def __init__(self) -> None:
        self._open: list[_OpenPosition] = []
        self._next_ticket = 1
        self._closed: list[ClosedTrade] = []

    def open(self, order: Order, entry_ts: datetime, entry_price: float) -> None:
        self._open.append(_OpenPosition(
            ticket=self._next_ticket,
            side=order.side,
            entry=entry_price,
            sl=order.stop_loss or 0.0,
            tp=order.take_profit or 0.0,
            lot=order.volume,
            opened_at=entry_ts,
            comment=order.comment,
        ))
        self._next_ticket += 1

    def update(self, ts: datetime, bid: float, ask: float) -> None:
        """Check each open position for SL/TP hit. Close on first hit."""
        still_open: list[_OpenPosition] = []
        for pos in self._open:
            # For BUY positions, exit at bid; for SELL, exit at ask
            cur = bid if pos.side == OrderSide.BUY else ask
            if pos.side == OrderSide.BUY:
                if pos.sl > 0 and cur <= pos.sl:
                    self._close(pos, ts, pos.sl, "STOP_LOSS")
                    continue
                if pos.tp > 0 and cur >= pos.tp:
                    self._close(pos, ts, pos.tp, "TAKE_PROFIT")
                    continue
            else:
                if pos.sl > 0 and cur >= pos.sl:
                    self._close(pos, ts, pos.sl, "STOP_LOSS")
                    continue
                if pos.tp > 0 and cur <= pos.tp:
                    self._close(pos, ts, pos.tp, "TAKE_PROFIT")
                    continue
            still_open.append(pos)
        self._open = still_open

    def force_close_all(self, ts: datetime, last_price: float) -> None:
        for pos in self._open:
            self._close(pos, ts, last_price, "END_OF_DATA")
        self._open = []

    def _close(self, pos: _OpenPosition, ts: datetime, exit_price: float, reason: str) -> None:
        if pos.side == OrderSide.BUY:
            pnl = (exit_price - pos.entry) * pos.lot * self.XAUUSD_CONTRACT_SIZE
        else:
            pnl = (pos.entry - exit_price) * pos.lot * self.XAUUSD_CONTRACT_SIZE
        self._closed.append(ClosedTrade(
            closed_at=ts, opened_at=pos.opened_at, side=pos.side,
            entry=pos.entry, exit=exit_price, lot=pos.lot, pnl=pnl,
            reason=reason, comment=pos.comment,
        ))

    @property
    def closed_trades(self) -> list[ClosedTrade]:
        return self._closed


# ---------- Tick stream ----------


def _read_tick_csv_gz(path: Path) -> Iterator[tuple[datetime, float, float]]:
    """Yield (ts, bid, ask) tuples from a gzipped Dukascopy-format CSV."""
    with gzip.open(path, "rt") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            ts_raw = row["timestamp"]
            if ts_raw.endswith("Z"):
                ts_raw = ts_raw[:-1] + "+00:00"
            try:
                ts = datetime.fromisoformat(ts_raw)
                bid = float(row["bid"])
                ask = float(row["ask"])
                yield ts, bid, ask
            except (KeyError, ValueError):
                continue


def _stream_tick_files(files: Iterable[Path]) -> Iterator[tuple[datetime, float, float]]:
    for f in sorted(files):
        yield from _read_tick_csv_gz(f)


# ---------- Adapter ----------


@dataclass
class ReplayResult:
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


async def run_ny_orb_replay(
    tick_files: list[Path],
    config: XauusdNyOrbConfig | None = None,
    risk_per_trade_pct: float = 1.0,
    starting_balance: float = 10000.0,
    log_every_ticks: int = 1_000_000,
) -> ReplayResult:
    """Run NY ORB strategy over a set of tick CSVs and return aggregated trades."""
    cfg = config or XauusdNyOrbConfig(enabled=True)
    bus = EventBus()

    # Capture orders synchronously so we can fire them on the next tick
    pending_orders: list[Order] = []

    async def _capture(event):
        if isinstance(event, OrderEvent) and event.order is not None:
            pending_orders.append(event.order)

    bus.subscribe("ORDER", _capture)

    # PositionSizer: simple risk-pct sizing based on starting balance.
    # XAUUSD pip value at 0.01 lot = $0.01/point. risk_per_trade_pct of
    # starting_balance / sl_distance gives the lot. We don't compound here
    # because the validator rolls equity off discrete trade results.
    def position_sizer(_mid: float, sl_distance: float) -> float:
        risk_usd = starting_balance * (risk_per_trade_pct / 100.0)
        # contract_size 100 oz; $1 per point per lot
        lot = risk_usd / (sl_distance * 100.0) if sl_distance > 0 else 0.0
        return max(0.01, round(lot, 2))

    strat = XauusdNyOrbTickBreakout(
        symbol="XAUUSD",
        config=cfg,
        event_bus=bus,
        position_sizer=position_sizer,
        is_news_blackout=lambda _ts: False,
        is_health_suspended=lambda: False,
    )

    aggregator = _MultiTimeframeAggregator(history_bars=300)

    # On each bar close, hand the dataframe to the strategy
    import pandas as pd

    def _on_m1(rows: list[dict], ts: datetime) -> None:
        if not rows:
            return
        strat.on_m1_close(pd.DataFrame(rows), ts)

    def _on_m5(rows: list[dict], ts: datetime) -> None:
        if not rows:
            return
        strat.on_m5_close(pd.DataFrame(rows), ts)

    def _on_d1(rows: list[dict], ts: datetime) -> None:
        if not rows:
            return
        strat.on_d1_close(pd.DataFrame(rows), ts)

    aggregator.set_callbacks(_on_m1, _on_m5, _on_d1)

    sim = _FillSimulator()
    result = ReplayResult()

    # On each tick: drive aggregator, run strategy, drain orders, simulate fills
    days_seen: set = set()
    last_price = 0.0
    last_ts: datetime | None = None
    for ts, bid, ask in _stream_tick_files(tick_files):
        mid = (bid + ask) / 2.0
        last_price = mid
        last_ts = ts
        days_seen.add(ts.date())

        # Bar aggregation may fire bar-close callbacks → strategy re-evaluates
        aggregator.push(ts, mid, volume=0.0)

        # Tick → strategy
        await strat.on_tick(Tick(symbol="XAUUSD", timestamp=ts, bid=bid, ask=ask, last=mid, volume=0.0))

        # Drain bus to surface pending OrderEvents
        await bus.drain()

        # Fire any new orders at the current tick's prices
        for order in pending_orders:
            entry = ask if order.side == OrderSide.BUY else bid
            sim.open(order, ts, entry)
            result.orders_received += 1
        pending_orders.clear()

        # Update open positions (SL/TP check)
        sim.update(ts, bid, ask)

        result.ticks_processed += 1
        if result.ticks_processed % log_every_ticks == 0:
            logger.info(
                "Replay progress: %d ticks, %d orders, %d closed trades, last=%s",
                result.ticks_processed, result.orders_received,
                len(sim.closed_trades), ts.isoformat(),
            )

    # Force-close any remaining open positions at end of data
    if last_ts is not None:
        sim.force_close_all(last_ts, last_price)

    result.closed_trades = sim.closed_trades
    result.final_pnl = sum(t.pnl for t in result.closed_trades)
    result.days_covered = len(days_seen)
    return result
