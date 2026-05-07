"""XAUUSD EMA Pullback Window state machine — 4-phase strategy.

DO_NOT_DEPLOY until walk-forward + DSR > 0.5 gate passes.

Port of `github.com/ilahuerta-IA/backtrader-pullback-window-xauusd`.
The published 5-year backtest shows Sharpe 0.892, PF 1.64, WR 55.43%,
max DD 5.81%, +44.75% return on XAUUSD M5. The same author's USDJPY
companion repo is **explicitly an overfitting case study** — extending
the date window dropped PF from 2.04 to 1.15 (−44%). Treat the headline
metrics as upper-bound and re-validate before any live exposure.

Phase machine:
    SCANNING       — fast EMA(1) crosses up over EMAs (14,18,24); slope
                     filter checks momentum direction
    ARMED          — wait 1-3 counter-trend pullback bars; abort after max
    WINDOW_OPEN    — fix volatility channel from ATR; mark breakout level
    ENTRY          — execute on bar close above breakout (or via tick if
                     wired to TickStream)

The on_tick path mirrors NY ORB: window-start vs current price detects
fresh crossings; SL = entry − 2.5 × ATR(M5,14); TP = 12 × ATR (rarely
hit, trailing-stop closes most trades — known per-author behaviour).

Validation gate (RUNBOOK §5):
1. In-sample replication on 2020-2025 ticks: Sharpe ≥ 0.85, PF ≥ 1.5, DD ≤ 7%
2. OOS test on 2018-2020 ticks: metrics within 70% of in-sample
3. Deflated Sharpe Ratio > 0.5 adjusting for ≥5 free parameters
4. Walk-forward 12-train/3-test rolling: avg test-window PF > 1.3
5. ≥ 385 aggregate trades for 95% confidence
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Optional

import pandas as pd
import pandas_ta as ta

from src.core.enums import OrderSide, OrderType
from src.core.events import EventBus, OrderEvent
from src.core.models import Order, Tick

logger = logging.getLogger(__name__)

BOT_MAGIC = 200000


class PullbackPhase(str, Enum):
    SCANNING = "SCANNING"
    ARMED = "ARMED"
    WINDOW_OPEN = "WINDOW_OPEN"
    ENTRY = "ENTRY"


@dataclass
class XauusdPullbackWindowConfig:
    """Pydantic-mirrored config (see src/config/schema.py).

    DO_NOT_DEPLOY: stay disabled until validation gate passes.
    """

    enabled: bool = False
    fast_ema: int = 1
    medium_ema: int = 14
    confirm_ema: int = 18
    slow_ema: int = 24
    pullback_max_bars: int = 3
    entry_window_bars: int = 2
    sl_atr_mult: float = 2.5
    tp_atr_mult: float = 12.0
    risk_pct: float = 1.0
    timeframe: str = "M5"
    atr_period: int = 14


@dataclass
class _State:
    phase: PullbackPhase = PullbackPhase.SCANNING
    side: Optional[OrderSide] = None
    pullback_bars_seen: int = 0
    entry_window_bars_seen: int = 0
    breakout_level: Optional[float] = None
    last_atr: Optional[float] = None
    last_bar_close: Optional[float] = None
    last_fast_above_slow: bool = False
    armed_at: Optional[datetime] = None


class XauusdPullbackWindowStateMachine:
    """4-phase EMA pullback / volatility-channel breakout strategy.

    Lifecycle:
        on_m5_close(m5_bars, as_of)  → drive phase machine on every M5 close
        on_tick(tick)                → fire entry when WINDOW_OPEN and tick
                                       crosses breakout_level
    """

    def __init__(
        self,
        symbol: str,
        config: XauusdPullbackWindowConfig,
        event_bus: EventBus,
        position_sizer: Callable[[float, float], float],
        is_news_blackout: Callable[[datetime], bool],
        is_health_suspended: Callable[[], bool],
    ) -> None:
        self._symbol = symbol
        self._cfg = config
        self._bus = event_bus
        self._position_sizer = position_sizer
        self._is_news_blackout = is_news_blackout
        self._is_health_suspended = is_health_suspended
        self._state = _State()
        # Two-tick buffer for fresh-crossing detection in tick path
        self._last_tick_price: Optional[float] = None

    # ----- bar callback -----

    def on_m5_close(self, m5_bars: pd.DataFrame, as_of: datetime) -> None:
        cfg = self._cfg
        min_bars = max(cfg.slow_ema, cfg.atr_period) + 5
        if m5_bars is None or len(m5_bars) < min_bars:
            return

        st = self._state
        close_series = m5_bars["close"]
        fast = ta.ema(close_series, length=cfg.fast_ema)
        medium = ta.ema(close_series, length=cfg.medium_ema)
        confirm = ta.ema(close_series, length=cfg.confirm_ema)
        slow = ta.ema(close_series, length=cfg.slow_ema)
        atr = ta.atr(m5_bars["high"], m5_bars["low"], m5_bars["close"], length=cfg.atr_period)
        if any(s is None or s.empty for s in (fast, medium, confirm, slow, atr)):
            return

        f = float(fast.iloc[-1])
        m = float(medium.iloc[-1])
        co = float(confirm.iloc[-1])
        sl = float(slow.iloc[-1])
        a = float(atr.iloc[-1])
        if a <= 0:
            return
        st.last_atr = a

        c_now = float(close_series.iloc[-1])
        c_prev = float(close_series.iloc[-2])
        st.last_bar_close = c_now

        # EMA-stack alignment + slope (momentum gate)
        bull_stack = f > m > co > sl
        bear_stack = f < m < co < sl
        slope_up = float(slow.iloc[-1]) > float(slow.iloc[-3]) if len(slow) >= 3 else False
        slope_down = float(slow.iloc[-1]) < float(slow.iloc[-3]) if len(slow) >= 3 else False

        # Phase transitions
        if st.phase == PullbackPhase.SCANNING:
            if bull_stack and slope_up:
                st.phase = PullbackPhase.ARMED
                st.side = OrderSide.BUY
                st.pullback_bars_seen = 0
                st.armed_at = as_of
                logger.debug("[%s pullback] ARMED long @ %.2f", self._symbol, c_now)
            elif bear_stack and slope_down:
                st.phase = PullbackPhase.ARMED
                st.side = OrderSide.SELL
                st.pullback_bars_seen = 0
                st.armed_at = as_of
                logger.debug("[%s pullback] ARMED short @ %.2f", self._symbol, c_now)
            return

        if st.phase == PullbackPhase.ARMED:
            # Counter-trend pullback bar?
            counter = (
                (st.side == OrderSide.BUY and c_now < c_prev)
                or (st.side == OrderSide.SELL and c_now > c_prev)
            )
            if counter:
                st.pullback_bars_seen += 1
                if st.pullback_bars_seen > cfg.pullback_max_bars:
                    # Pullback ran too long; abandon setup, restart scan
                    st.phase = PullbackPhase.SCANNING
                    st.side = None
                    st.pullback_bars_seen = 0
                    return
            else:
                # Continuation bar after at least one pullback → open window
                if st.pullback_bars_seen >= 1:
                    if st.side == OrderSide.BUY:
                        st.breakout_level = float(m5_bars["high"].iloc[-1]) + 0.10
                    else:
                        st.breakout_level = float(m5_bars["low"].iloc[-1]) - 0.10
                    st.phase = PullbackPhase.WINDOW_OPEN
                    st.entry_window_bars_seen = 0
                    logger.debug(
                        "[%s pullback] WINDOW_OPEN %s breakout=%.2f",
                        self._symbol, st.side.value if st.side else "?", st.breakout_level,
                    )
            return

        if st.phase == PullbackPhase.WINDOW_OPEN:
            st.entry_window_bars_seen += 1
            if st.entry_window_bars_seen > cfg.entry_window_bars:
                # Entry window expired without breakout — restart scan
                st.phase = PullbackPhase.SCANNING
                st.side = None
                st.breakout_level = None
                return

    # ----- tick callback -----

    async def on_tick(self, tick: Tick) -> None:
        if tick.symbol != self._symbol:
            return
        st = self._state
        if st.phase != PullbackPhase.WINDOW_OPEN:
            return
        if st.breakout_level is None or st.last_atr is None or st.side is None:
            return

        if self._is_health_suspended() or self._is_news_blackout(tick.timestamp):
            return

        mid = (tick.bid + tick.ask) / 2.0 if tick.bid and tick.ask else (tick.last or tick.bid or tick.ask)
        if not mid:
            return

        if self._last_tick_price is None:
            self._last_tick_price = mid
            return

        # Fresh crossing detection
        prev = self._last_tick_price
        crossed_up = st.side == OrderSide.BUY and prev <= st.breakout_level and mid > st.breakout_level
        crossed_down = st.side == OrderSide.SELL and prev >= st.breakout_level and mid < st.breakout_level
        self._last_tick_price = mid
        if not (crossed_up or crossed_down):
            return

        # Compute SL/TP
        sl_dist = self._cfg.sl_atr_mult * st.last_atr
        tp_dist = self._cfg.tp_atr_mult * st.last_atr
        if st.side == OrderSide.BUY:
            sl = mid - sl_dist
            tp = mid + tp_dist
        else:
            sl = mid + sl_dist
            tp = mid - tp_dist

        lot = self._position_sizer(mid, sl_dist)
        if lot <= 0:
            return

        order = Order(
            symbol=self._symbol,
            side=st.side,
            order_type=OrderType.MARKET,
            volume=lot,
            stop_loss=round(sl, 5),
            take_profit=round(tp, 5),
            magic=BOT_MAGIC,
            comment=f"pullback_window:{st.breakout_level:.2f}",
        )
        await self._bus.publish(OrderEvent(timestamp=tick.timestamp, order=order))
        st.phase = PullbackPhase.ENTRY
        logger.info(
            "[%s pullback] FIRED %s mid=%.2f SL=%.2f TP=%.2f lot=%.3f",
            self._symbol, st.side.value, mid, sl, tp, lot,
        )

    @property
    def state(self) -> _State:
        return self._state
