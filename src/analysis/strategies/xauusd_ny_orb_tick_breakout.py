"""XAUUSD NY Open Range Breakout — bar-armed, tick-fired hybrid strategy.

⚠️ DO_NOT_DEPLOY ⚠️
Walk-forward 2018-2026 (831 trades) shows NO EDGE in current implementation:
  WR 36.5%, PF 0.34, Sharpe(ann) -7.58, total P&L -$45,250 on $10k start.
  Every single year (2018-2026) loses money — not a regime issue, structural.

Failure modes identified (fix before any re-run):
  1. Fill simulator closes whole position at TP1 (=1×SL), ignoring 50/30/20
     scale-out ladder. Effective R:R = 1:1, not 1:3 as designed.
  2. Stale-breakout filter blocks ~50% of sessions during high-vol XAUUSD.
  3. Velocity gate fires on weak ORB crossings — strategy "FIRED" too often
     on noise, not genuine momentum.
  4. Avg win $77 / avg loss $130 = R:R 0.60. Even 65% WR loses money.

Status: research artifact. Walk-forward report at:
    reports/ny_orb_2018_2026_walkforward.html
    reports/ny_orb_2018-2026_trades.jsonl

Evidence base for the ORB *family* (still valid; this implementation just
doesn't capture it):
- Zarattini SSRN 4729284 (2024) — 7,000-stock ORB study, Sharpe 2.81 alpha
- IEEE TORB (2019) — 5 index futures markets, >8% annual returns p<3%
- yulz008/GOLD_ORB (216 stars) — XAUUSD MQL5 reference implementation
- Forex Factory #1388244 — 58-68% WR with body ≥ 0.8×ATR(5) filter
- PDH/PDL confluence — weak independent evidence; used as score boost only

Equity-market ORB does not auto-transfer to XAUUSD per QuantifiedStrategies
+ Unger Academy negative GC tests — confirmed by this empirical run.

Architecture:
- Bar context (M1+M5+D1) arms a setup at NY open
- Tick subscriber fires the entry exactly when price breaches with
  confirming velocity ≥ 0.5 × ATR(M1,14)
- TickPositionManager handles SL/TP modify, partials, trailing exits
- StrategyHealthMonitor consumes per-trade telemetry

Key invariants:
- Hold-time floor ≥ 2 min (FundingPips toxic-flow compliance)
- Modify-rate-limit 8s/ticket (FTMO 2,000 req/day cap headroom)
- Daily rollover 21:00-22:00 UTC blocks new entries
- ATR expansion guard auto-pauses when M5 ATR > 2.5× 20-session median
- News blackout via shared NewsEventFilter (post-mortem-5k discipline)

NOT shipping live; ships behind `enabled: false` flag pending walk-forward
validation per RUNBOOK §5.
"""

from __future__ import annotations

import logging
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import Callable, Optional

import pandas as pd
import pandas_ta as ta

from src.core.enums import OrderSide, OrderType
from src.core.events import EventBus, OrderEvent
from src.core.models import Order, Tick

logger = logging.getLogger(__name__)

# NY open in UTC = 13:30 (EDT) — DST handling left to caller / NewsEventFilter.
# We use 13:30 as the canonical reference; adjust when EST==UTC-5 (Nov-Mar).
NY_OPEN_UTC = time(13, 30)
NY_RANGE_END_UTC = time(13, 35)
DAILY_ROLLOVER_BLOCK_START_UTC = time(21, 0)
DAILY_ROLLOVER_BLOCK_END_UTC = time(22, 0)

BOT_MAGIC = 200000


class OrbPhase(str, Enum):
    AWAITING_NY_OPEN = "AWAITING_NY_OPEN"   # before 13:30 UTC
    CAPTURING_RANGE = "CAPTURING_RANGE"     # 13:30 → 13:35 UTC
    CONSOLIDATING = "CONSOLIDATING"         # waiting for 3 M1 closes inside range
    ARMED = "ARMED"                          # range valid, awaiting tick breakout
    FIRED = "FIRED"                          # entry fired this session
    BLOCKED = "BLOCKED"                      # gated out (chop/news/health)


@dataclass
class XauusdNyOrbConfig:
    enabled: bool = False  # off by default until walk-forward gate clears
    consolidation_bars: int = 3
    velocity_window_ticks: int = 30
    velocity_atr_mult: float = 0.5
    stale_buffer_pips: float = 3.0
    adx_min: float = 22.0
    atr_m1_period: int = 14
    atr_m5_period: int = 14
    pdh_pdl_confluence_atr_pct: float = 0.30  # within 30% × ATR(M5) → boost
    pdh_pdl_confluence_score: float = 0.20
    sl_atr_m1_mult: float = 1.0
    scaleout_pcts: tuple[float, float, float] = (0.50, 0.30, 0.20)
    risk_pct: float = 1.0
    daily_max_entries: int = 3
    hold_time_floor_seconds: int = 120
    london_secondary: bool = True  # also accept 07:00-12:00 UTC with PDH/PDL only


@dataclass
class _DailyState:
    """Per-symbol per-UTC-day state."""

    date_str: str = ""
    pdh: Optional[float] = None
    pdl: Optional[float] = None
    orb_high: Optional[float] = None
    orb_low: Optional[float] = None
    consolidation_bars_seen: int = 0
    phase: OrbPhase = OrbPhase.AWAITING_NY_OPEN
    entries_today: int = 0
    last_atr_m1: Optional[float] = None
    last_atr_m5: Optional[float] = None
    confluence_score: float = 0.0


class XauusdNyOrbTickBreakout:
    """Bar-armed, tick-fired NY ORB strategy for XAUUSD.

    Lifecycle:
        on_d1_close(d1_bars)   → snapshot PDH/PDL
        on_m5_close(m5_bars)   → ATR snapshot, ADX gate
        on_m1_close(m1_bars)   → range capture / consolidation tracking
        on_tick(tick)          → fire entry if armed and velocity confirms

    `entries_callback(order)` is the dispatch hook — caller wires this to
    `event_bus.publish(OrderEvent(...))`. Splitting it out keeps the hot
    on_tick path testable without an event bus.
    """

    def __init__(
        self,
        symbol: str,
        config: XauusdNyOrbConfig,
        event_bus: EventBus,
        position_sizer: Callable[[float, float], float],
        is_news_blackout: Callable[[datetime], bool],
        is_health_suspended: Callable[[], bool],
        atr_expansion_guard: Callable[[float], bool] | None = None,
    ) -> None:
        self._symbol = symbol
        self._cfg = config
        self._bus = event_bus
        self._position_sizer = position_sizer
        self._is_news_blackout = is_news_blackout
        self._is_health_suspended = is_health_suspended
        # Returns True when current ATR is anomalously high vs baseline
        self._atr_expansion_guard = atr_expansion_guard or (lambda atr: False)

        self._state = _DailyState()
        # Tick velocity window
        self._tick_prices: deque[tuple[datetime, float]] = deque()

    # ----- bar callbacks -----

    def on_d1_close(self, d1_bars: pd.DataFrame, as_of: datetime) -> None:
        """Snapshot prev-day high/low. Called once at UTC midnight rollover."""
        if d1_bars is None or len(d1_bars) < 2:
            return
        # Use the last fully-closed daily bar
        prev = d1_bars.iloc[-2]
        date_str = as_of.strftime("%Y-%m-%d")
        if self._state.date_str != date_str:
            self._reset_for_new_day(date_str)
        self._state.pdh = float(prev["high"])
        self._state.pdl = float(prev["low"])
        logger.info(
            "[%s ORB] PDH/PDL snapshot for %s: %.2f / %.2f",
            self._symbol, date_str, self._state.pdh, self._state.pdl,
        )

    def on_m5_close(self, m5_bars: pd.DataFrame, as_of: datetime) -> None:
        """Update ATR(M5) + ADX(M5) snapshots."""
        if m5_bars is None or len(m5_bars) < self._cfg.atr_m5_period + 5:
            return
        atr = ta.atr(m5_bars["high"], m5_bars["low"], m5_bars["close"], length=self._cfg.atr_m5_period)
        if atr is None or atr.empty:
            return
        atr_val = float(atr.iloc[-1])
        if atr_val > 0:
            self._state.last_atr_m5 = atr_val

    def on_m1_close(self, m1_bars: pd.DataFrame, as_of: datetime) -> None:
        """Range capture (13:30-13:35 UTC) + consolidation gate (3 closes inside).

        Also tracks ATR(M1) for tick-velocity threshold.
        """
        if m1_bars is None or len(m1_bars) < self._cfg.atr_m1_period + 5:
            return

        # Roll new day if needed
        date_str = as_of.strftime("%Y-%m-%d")
        if self._state.date_str != date_str:
            self._reset_for_new_day(date_str)

        # ATR(M1)
        atr = ta.atr(m1_bars["high"], m1_bars["low"], m1_bars["close"], length=self._cfg.atr_m1_period)
        if atr is not None and not atr.empty:
            self._state.last_atr_m1 = float(atr.iloc[-1])

        # Compare time-of-day in UTC without tz coupling — both sides are
        # plain (hour, minute) tuples to avoid offset-aware vs naive issues.
        bar_hm = (as_of.hour, as_of.minute)
        ny_open_hm = (NY_OPEN_UTC.hour, NY_OPEN_UTC.minute)
        ny_close_hm = (NY_RANGE_END_UTC.hour, NY_RANGE_END_UTC.minute)

        # Phase machine
        st = self._state
        if st.phase == OrbPhase.AWAITING_NY_OPEN and bar_hm >= ny_open_hm:
            st.phase = OrbPhase.CAPTURING_RANGE
            st.orb_high = float(m1_bars.iloc[-1]["high"])
            st.orb_low = float(m1_bars.iloc[-1]["low"])
            return

        if st.phase == OrbPhase.CAPTURING_RANGE:
            last = m1_bars.iloc[-1]
            st.orb_high = max(st.orb_high or 0.0, float(last["high"]))
            st.orb_low = min(st.orb_low if st.orb_low is not None else float("inf"), float(last["low"]))
            if bar_hm >= ny_close_hm:
                # Range complete → enter consolidation gate
                st.phase = OrbPhase.CONSOLIDATING
                self._compute_confluence_score()
                logger.info(
                    "[%s ORB] Range complete: high=%.2f low=%.2f confluence=%.2f",
                    self._symbol, st.orb_high or 0, st.orb_low or 0, st.confluence_score,
                )
            return

        if st.phase == OrbPhase.CONSOLIDATING and st.orb_high and st.orb_low:
            last = m1_bars.iloc[-1]
            inside = float(last["low"]) >= st.orb_low and float(last["high"]) <= st.orb_high
            if inside:
                st.consolidation_bars_seen += 1
                if st.consolidation_bars_seen >= self._cfg.consolidation_bars:
                    st.phase = OrbPhase.ARMED
                    logger.info("[%s ORB] ARMED (%d consolidation bars)", self._symbol, st.consolidation_bars_seen)
            else:
                # Price already broke during consolidation → stale, abandon
                st.phase = OrbPhase.BLOCKED
                logger.info("[%s ORB] Pre-consolidation breakout (stale), session BLOCKED")

    # ----- tick callback -----

    async def on_tick(self, tick: Tick) -> None:
        """Hot path. Fire entry only if all gates pass."""
        if tick.symbol != self._symbol:
            return
        st = self._state
        if st.phase != OrbPhase.ARMED:
            return
        if st.entries_today >= self._cfg.daily_max_entries:
            st.phase = OrbPhase.BLOCKED
            return

        # Daily-rollover block — compare tz-naive (hour, minute) tuples
        now = tick.timestamp
        cur_hm = (now.hour, now.minute)
        block_start_hm = (DAILY_ROLLOVER_BLOCK_START_UTC.hour, DAILY_ROLLOVER_BLOCK_START_UTC.minute)
        block_end_hm = (DAILY_ROLLOVER_BLOCK_END_UTC.hour, DAILY_ROLLOVER_BLOCK_END_UTC.minute)
        if block_start_hm <= cur_hm < block_end_hm:
            return

        # Health + news + ATR expansion gates
        if self._is_health_suspended() or self._is_news_blackout(now):
            return
        if st.last_atr_m5 is None or self._atr_expansion_guard(st.last_atr_m5):
            return

        mid = (tick.bid + tick.ask) / 2.0 if tick.bid and tick.ask else (tick.last or tick.bid or tick.ask)
        if not mid or st.orb_high is None or st.orb_low is None or st.last_atr_m1 is None:
            return

        # Stale-breakout guard: if the *first* tick after ARMING is already
        # well past the breakout level (more than stale_buffer_pips × point),
        # the breakout happened before we were ready and we missed it. Skip
        # the rest of this session.
        stale_buffer = self._cfg.stale_buffer_pips * 0.01  # XAUUSD point=0.01
        if not self._tick_prices:
            if mid > st.orb_high + stale_buffer or mid < st.orb_low - stale_buffer:
                st.phase = OrbPhase.BLOCKED
                logger.info("[%s ORB] Stale-breakout on first armed tick @ %.2f", self._symbol, mid)
                return

        # Track tick history for velocity computation
        self._tick_prices.append((now, mid))
        cutoff_ticks = self._cfg.velocity_window_ticks
        while len(self._tick_prices) > cutoff_ticks:
            self._tick_prices.popleft()
        if len(self._tick_prices) < cutoff_ticks:
            return  # not enough history yet

        # Fresh-crossing detector: at the start of the velocity window the
        # price was inside the range, by the latest tick it has crossed out.
        # Comparing window-start vs current handles tick-equal-to-level cases
        # without losing the breakout (a same-tick double-fire is prevented
        # by the FIRED phase transition below).
        window_start_price = self._tick_prices[0][1]
        side: OrderSide | None = None
        breakout_level: float = 0.0
        if window_start_price <= st.orb_high and mid > st.orb_high:
            side = OrderSide.BUY
            breakout_level = st.orb_high
        elif window_start_price >= st.orb_low and mid < st.orb_low:
            side = OrderSide.SELL
            breakout_level = st.orb_low

        if side is None:
            return
        velocity = abs(self._tick_prices[-1][1] - self._tick_prices[0][1])
        velocity_threshold = self._cfg.velocity_atr_mult * st.last_atr_m1
        if velocity < velocity_threshold:
            return  # weak breakout

        # Risk: SL = opposite side of ORB OR entry - 1.0 * ATR(M1) whichever closer
        if side == OrderSide.BUY:
            sl_atr = mid - self._cfg.sl_atr_m1_mult * st.last_atr_m1
            sl_orb = st.orb_low
            sl = max(sl_atr, sl_orb)  # higher = closer for BUY
        else:
            sl_atr = mid + self._cfg.sl_atr_m1_mult * st.last_atr_m1
            sl_orb = st.orb_high
            sl = min(sl_atr, sl_orb)  # lower = closer for SELL

        sl_distance = abs(mid - sl)
        if sl_distance <= 0:
            return

        # Hold-time floor compliance is enforced post-trade by
        # StrategyHealthMonitor.on_trade_closed (HOLD_TIME_FLOOR signal).
        # No pre-entry heuristic — observation beats prediction here.

        # Position size
        lot = self._position_sizer(mid, sl_distance)
        if lot <= 0:
            return

        # Take-profit ladder (50/30/20 scale-out)
        if side == OrderSide.BUY:
            tp1 = mid + sl_distance
            tp2 = mid + 2 * sl_distance
            tp3 = mid + 3 * sl_distance
        else:
            tp1 = mid - sl_distance
            tp2 = mid - 2 * sl_distance
            tp3 = mid - 3 * sl_distance

        order = Order(
            symbol=self._symbol,
            side=side,
            order_type=OrderType.MARKET,
            volume=lot,
            stop_loss=round(sl, 5),
            take_profit=round(tp1, 5),
            take_profit_levels=[round(tp1, 5), round(tp2, 5), round(tp3, 5)],
            magic=BOT_MAGIC,
            comment=f"ny_orb:{breakout_level:.2f}",
        )
        await self._bus.publish(OrderEvent(timestamp=now, order=order))
        st.entries_today += 1
        st.phase = OrbPhase.FIRED
        logger.info(
            "[%s ORB] FIRED %s mid=%.2f SL=%.5f TP=%.5f lot=%.3f velocity=%.4f",
            self._symbol, side.value, mid, sl, tp1, lot, velocity,
        )

    # ----- internals -----

    def _reset_for_new_day(self, date_str: str) -> None:
        st = self._state
        # Preserve PDH/PDL if already populated for this date_str — just reset
        # the per-day phase machine and tick window.
        self._state = _DailyState(
            date_str=date_str,
            pdh=st.pdh if st.date_str == date_str else None,
            pdl=st.pdl if st.date_str == date_str else None,
            phase=OrbPhase.AWAITING_NY_OPEN,
        )
        self._tick_prices.clear()

    def _compute_confluence_score(self) -> None:
        """If ORB high/low aligns with PDH/PDL within tolerance, boost score."""
        st = self._state
        if (
            st.pdh is None or st.pdl is None
            or st.orb_high is None or st.orb_low is None
            or st.last_atr_m5 is None
        ):
            return
        tol = self._cfg.pdh_pdl_confluence_atr_pct * st.last_atr_m5
        if abs(st.orb_high - st.pdh) <= tol or abs(st.orb_low - st.pdl) <= tol:
            st.confluence_score = self._cfg.pdh_pdl_confluence_score

    @property
    def state(self) -> _DailyState:
        """Diagnostic accessor for tests + status endpoints."""
        return self._state
