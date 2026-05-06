"""Tick-replay backtester for tick-driven strategies.

Bar-based backtester (`scripts/backtest_scalping.py`) cannot validate
tick-level entry/exit logic — by the time a 5-minute candle closes the
tick velocity signal has long passed. This engine replays a tick CSV at
recorded timestamps and dispatches each tick to the strategy's `on_tick`,
identical to how live `TickStream` would.

CSV format (Dukascopy / MT5 export):
    timestamp,bid,ask,last,volume
    2026-04-01T08:00:00.123Z,3998.45,3998.55,3998.50,1.0
    ...

Status: SCAFFOLD. Pairs with `tick_velocity_breakout.py`. Once tick
history is collected (2 weeks demo recording per plan Track B.1.2),
extend `replay()` with simulated fills + slippage modeling.
"""

from __future__ import annotations

import asyncio
import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from src.core.models import Tick

logger = logging.getLogger(__name__)


@dataclass
class TickReplayResult:
    """Aggregated metrics from a tick replay."""

    ticks_processed: int = 0
    signals_fired: int = 0
    trades_executed: int = 0
    winning_trades: int = 0
    total_pnl: float = 0.0
    max_drawdown_pct: float = 0.0
    duration_seconds: float = 0.0
    metadata: dict = field(default_factory=dict)


class TickReplayEngine:
    """Replays a tick CSV file at speed-of-light into a strategy's on_tick.

    Args:
        csv_path: tick history file (gzip-friendly via `.gz` extension)
        symbol: symbol label assigned to each yielded Tick
        on_tick: async callback the strategy registers — same shape as
                 `TickStream.on_tick`
        speed: 1.0 = real-time, 0.0 = as-fast-as-possible (default)

    Returns TickReplayResult after replay completes.
    """

    def __init__(
        self,
        csv_path: str | Path,
        symbol: str,
        on_tick: Callable[[Tick], Awaitable[None]],
        speed: float = 0.0,
    ) -> None:
        self._csv_path = Path(csv_path)
        self._symbol = symbol
        self._on_tick = on_tick
        self._speed = speed

    async def replay(self, max_ticks: int | None = None) -> TickReplayResult:
        """Run the replay. Returns aggregated metrics."""
        if not self._csv_path.exists():
            raise FileNotFoundError(self._csv_path)

        result = TickReplayResult()
        prev_ts: datetime | None = None
        opener = self._open_csv()

        with opener as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                tick = self._parse_row(row)
                if tick is None:
                    continue

                # Real-time pacing if speed > 0
                if self._speed > 0 and prev_ts is not None:
                    delta = (tick.timestamp - prev_ts).total_seconds()
                    if delta > 0:
                        await asyncio.sleep(delta / self._speed)
                prev_ts = tick.timestamp

                try:
                    await self._on_tick(tick)
                except Exception:
                    logger.warning("Strategy on_tick error", exc_info=True)

                result.ticks_processed += 1
                if max_ticks and result.ticks_processed >= max_ticks:
                    break

        return result

    def _open_csv(self):
        if str(self._csv_path).endswith(".gz"):
            import gzip
            return gzip.open(self._csv_path, mode="rt")
        return open(self._csv_path, mode="r")

    def _parse_row(self, row: dict) -> Tick | None:
        try:
            ts_raw = row["timestamp"]
            # Strip Z and parse ISO8601
            if ts_raw.endswith("Z"):
                ts_raw = ts_raw[:-1] + "+00:00"
            ts = datetime.fromisoformat(ts_raw)
            return Tick(
                symbol=self._symbol,
                timestamp=ts,
                bid=float(row.get("bid", 0) or 0),
                ask=float(row.get("ask", 0) or 0),
                last=float(row.get("last", 0) or 0),
                volume=float(row.get("volume", 0) or 0),
            )
        except (KeyError, ValueError, TypeError):
            return None
