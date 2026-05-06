"""Tick-velocity breakout — experimental tick-driven entry strategy.

Thesis: gold tick velocity > 2σ over a rolling 30-second window suggests
a momentum follow-through over the next 60-120 seconds. Net-new strategy
that subscribes to TickStream.on_tick directly (NOT bar-based like the
other M1/M5 scalpers).

Status: SCAFFOLD — disabled by default. Validation gate (per plan):
  1. ≥3 months tick history replay shows positive expectancy, DD < 8%
  2. 2 weeks live demo paper-trade matches replay within 20%
  3. Slippage p95 ≤ 0.5 pips on XAUUSD

Wire-up later (when validated):
    strategy = TickVelocityBreakout(symbol="XAUUSD", config=...)
    tick_stream.on_tick(strategy.on_tick)
    # strategy.on_tick publishes SignalEvent / OrderEvent on the bus
"""

from __future__ import annotations

import logging
import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from src.core.enums import OrderSide, OrderType
from src.core.events import EventBus, OrderEvent
from src.core.models import Order, Tick

logger = logging.getLogger(__name__)


@dataclass
class TickVelocityConfig:
    """Strategy parameters. Conservative defaults pending validation."""

    velocity_window_seconds: int = 30
    sigma_window_seconds: int = 600  # 10 min for σ baseline
    sigma_threshold: float = 2.0
    sl_pct: float = 0.15  # 0.15% of price
    tp_pct: float = 0.25  # 0.25% of price (1.67 R:R)
    max_daily_trades: int = 50
    min_seconds_between_signals: int = 60
    cooldown_after_loss_seconds: int = 300


class TickVelocityBreakout:
    """Rolling-window velocity z-score breakout on ticks.

    Maintains two deques per symbol:
      - prices: (timestamp, price) pairs over `velocity_window_seconds`
        — used to compute current velocity (price[t] - price[t-window]) / window
      - velocities: scalar floats over `sigma_window_seconds`
        — used to compute σ for z-score gating

    On each tick:
      1. Append to prices, evict expired
      2. Compute velocity. Append to velocities, evict expired.
      3. If |velocity| > sigma_threshold * σ AND not in news blackout
         AND daily-trade quota not exhausted AND past min-seconds-between
         → publish OrderEvent
    """

    def __init__(
        self,
        symbol: str,
        event_bus: EventBus,
        config: TickVelocityConfig,
        position_sizer: Callable[[float, float], float],  # (entry, sl) → lot
        is_news_blackout: Callable[[datetime], bool],
    ) -> None:
        self._symbol = symbol
        self._bus = event_bus
        self._cfg = config
        self._position_sizer = position_sizer
        self._is_news_blackout = is_news_blackout

        self._prices: deque[tuple[datetime, float]] = deque()
        self._velocities: deque[tuple[datetime, float]] = deque()
        self._daily_count = 0
        self._daily_date = ""
        self._last_signal_at: Optional[datetime] = None
        self._cooldown_until: Optional[datetime] = None

    async def on_tick(self, tick: Tick) -> None:
        """Tick callback. Hot path — keep cheap."""
        if tick.symbol != self._symbol:
            return

        mid = (tick.bid + tick.ask) / 2.0 if tick.bid and tick.ask else (tick.last or tick.bid or tick.ask)
        if not mid:
            return

        now = tick.timestamp
        self._evict_old(now)
        self._prices.append((now, mid))

        velocity = self._compute_velocity(now)
        if velocity is None:
            return
        self._velocities.append((now, velocity))

        if not self._can_signal(now):
            return

        sigma = self._velocity_sigma()
        if sigma is None or sigma <= 0:
            return

        if abs(velocity) < self._cfg.sigma_threshold * sigma:
            return

        # Strong move detected — fire entry
        side = OrderSide.BUY if velocity > 0 else OrderSide.SELL
        await self._emit_entry(side, mid, now)

    def on_close(self, ticket: int, pnl: float) -> None:
        """Signal-handler hook — call from PositionClosedEvent subscriber to
        engage cooldown after a losing trade. Reduces serial losing
        breakouts when the velocity regime is whipsawing."""
        if pnl < 0:
            self._cooldown_until = datetime.now(timezone.utc) + timedelta(
                seconds=self._cfg.cooldown_after_loss_seconds
            )
            logger.info(
                "TickVelocity cooldown until %s after losing trade ticket=%d",
                self._cooldown_until.isoformat(), ticket,
            )

    # --- internals ---

    def _evict_old(self, now: datetime) -> None:
        cutoff_v = now - timedelta(seconds=self._cfg.velocity_window_seconds)
        while self._prices and self._prices[0][0] < cutoff_v:
            self._prices.popleft()

        cutoff_s = now - timedelta(seconds=self._cfg.sigma_window_seconds)
        while self._velocities and self._velocities[0][0] < cutoff_s:
            self._velocities.popleft()

    def _compute_velocity(self, now: datetime) -> Optional[float]:
        if not self._prices:
            return None
        oldest_t, oldest_p = self._prices[0]
        elapsed = (now - oldest_t).total_seconds()
        if elapsed < self._cfg.velocity_window_seconds * 0.8:
            return None  # window not yet full
        latest_p = self._prices[-1][1]
        return (latest_p - oldest_p) / max(elapsed, 1.0)

    def _velocity_sigma(self) -> Optional[float]:
        if len(self._velocities) < 30:
            return None
        try:
            return statistics.stdev(v for _, v in self._velocities)
        except statistics.StatisticsError:
            return None

    def _can_signal(self, now: datetime) -> bool:
        # Daily quota
        today = now.strftime("%Y-%m-%d")
        if today != self._daily_date:
            self._daily_count = 0
            self._daily_date = today
        if self._daily_count >= self._cfg.max_daily_trades:
            return False

        # Cooldown after loss
        if self._cooldown_until and now < self._cooldown_until:
            return False

        # Min spacing between signals
        if self._last_signal_at and (now - self._last_signal_at).total_seconds() < \
                self._cfg.min_seconds_between_signals:
            return False

        # News blackout
        if self._is_news_blackout(now):
            return False

        return True

    async def _emit_entry(self, side: OrderSide, price: float, now: datetime) -> None:
        sl_dist = price * self._cfg.sl_pct / 100.0
        tp_dist = price * self._cfg.tp_pct / 100.0
        if side == OrderSide.BUY:
            sl, tp = price - sl_dist, price + tp_dist
        else:
            sl, tp = price + sl_dist, price - tp_dist

        volume = self._position_sizer(price, sl)
        if volume <= 0:
            return

        order = Order(
            symbol=self._symbol,
            side=side,
            order_type=OrderType.MARKET,
            volume=volume,
            stop_loss=round(sl, 2),
            take_profit=round(tp, 2),
            magic=200000,
            comment="tick_vel_breakout",
        )
        await self._bus.publish(OrderEvent(timestamp=now, order=order))

        self._daily_count += 1
        self._last_signal_at = now
        logger.info(
            "TickVelocity ENTRY: %s %s vol=%.2f price=%.2f SL=%.2f TP=%.2f (daily %d/%d)",
            side.value, self._symbol, volume, price, sl, tp,
            self._daily_count, self._cfg.max_daily_trades,
        )
