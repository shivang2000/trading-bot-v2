"""Strategy health monitor — 8 early-warning signals.

Watches live-account telemetry for the patterns that historically precede
strategy failure on retail XAUUSD EAs:

| Signal                  | Threshold                                    | Action            |
|-------------------------|----------------------------------------------|-------------------|
| Spread regime shift     | rolling-20-trade avg > 1.5x baseline (3 in a row) | Suspend entries |
| Slippage drift          | avg slippage > 10 points                     | Alert + suspend   |
| Modify-rejection rate   | > 5% of modifies rejected                    | Alert (broker throttle precursor) |
| ATR expansion           | M5 ATR > 2.5x 20-session median              | Pause entries     |
| WR degradation          | rolling-20-trade WR < 40%                    | Manual review     |
| Hold-time floor breach  | > 10% of trades close < 2 min                | Filter inadequate |
| Daily DD proximity      | floating DD >= 60% of FundingPips daily limit| Auto-flat + halt  |
| Trade frequency spike   | > 3 entries / 15-min window / symbol         | HFT-adjacency log |

Pure-Python state machine. Caller (PositionMonitor / RiskManager) feeds in
trade-close events, modify outcomes, ATR snapshots; monitor emits
HealthAlert objects via callback. No MT5 calls — keeps this testable
without RPyC.
"""

from __future__ import annotations

import logging
import statistics
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Iterable

logger = logging.getLogger(__name__)


class HealthSignal(str, Enum):
    SPREAD_REGIME = "SPREAD_REGIME"
    SLIPPAGE_DRIFT = "SLIPPAGE_DRIFT"
    MODIFY_REJECTION = "MODIFY_REJECTION"
    ATR_EXPANSION = "ATR_EXPANSION"
    WR_DEGRADATION = "WR_DEGRADATION"
    HOLD_TIME_FLOOR = "HOLD_TIME_FLOOR"
    DD_PROXIMITY = "DD_PROXIMITY"
    TRADE_FREQUENCY = "TRADE_FREQUENCY"


class HealthAction(str, Enum):
    ALERT_ONLY = "ALERT_ONLY"  # log + notify, no trading change
    SUSPEND_ENTRIES = "SUSPEND_ENTRIES"  # block new entries; existing positions managed
    AUTO_FLAT = "AUTO_FLAT"  # close all + halt session


@dataclass
class HealthAlert:
    """Single firing of a health signal."""

    signal: HealthSignal
    action: HealthAction
    message: str
    fired_at: datetime
    metric_value: float = 0.0
    threshold: float = 0.0


@dataclass
class HealthThresholds:
    """All 8 signal thresholds. Defaults from research findings."""

    spread_baseline_window: int = 20
    spread_multiplier: float = 1.5
    spread_consecutive_breach: int = 3

    slippage_avg_points_max: float = 10.0
    slippage_window: int = 20

    modify_rejection_pct_max: float = 5.0
    modify_window: int = 100

    atr_expansion_multiplier: float = 2.5
    atr_session_window: int = 20

    wr_window: int = 20
    wr_floor_pct: float = 40.0

    hold_time_floor_seconds: int = 120  # 2 minutes
    hold_time_breach_pct_max: float = 10.0
    hold_time_window: int = 20

    dd_proximity_pct_of_limit: float = 60.0  # 60% of FundingPips daily limit

    trade_frequency_window_minutes: int = 15
    trade_frequency_max_entries: int = 3


@dataclass
class _TradeRecord:
    closed_at: datetime
    pnl: float
    hold_seconds: float
    spread_at_entry_points: float
    slippage_points: float


class StrategyHealthMonitor:
    """Stateful monitor that consumes live-account telemetry.

    Caller wires PositionMonitor / RiskManager / fill-handler to feed in:
    - on_trade_closed(...)        per closed trade
    - on_modify_attempt(success)  per modify request
    - on_atr_snapshot(...)        per session-close M5 ATR
    - on_floating_dd(...)         per poll cycle
    - on_entry(symbol, ts)        per new bot entry

    Monitor calls `alert_callback(alert)` whenever a signal fires.
    """

    def __init__(
        self,
        thresholds: HealthThresholds | None = None,
        alert_callback: Callable[[HealthAlert], None] | None = None,
        daily_dd_limit_usd: float = 0.0,
    ) -> None:
        self._t = thresholds or HealthThresholds()
        self._alert_cb = alert_callback or (lambda a: None)
        self._daily_dd_limit_usd = daily_dd_limit_usd

        self._trades: deque[_TradeRecord] = deque(maxlen=max(
            self._t.spread_baseline_window,
            self._t.slippage_window,
            self._t.wr_window,
            self._t.hold_time_window,
        ))
        self._modify_outcomes: deque[bool] = deque(maxlen=self._t.modify_window)
        self._atrs: deque[float] = deque(maxlen=self._t.atr_session_window)
        self._entries_by_symbol: dict[str, deque[datetime]] = {}

        # Latched state — once a "suspend" alert fires we don't keep yelling
        # every poll. Callers call `clear_suspend(signal)` to reset.
        self._latched: dict[HealthSignal, HealthAlert] = {}

        # Spread breach streak counter
        self._spread_consecutive_breaches = 0

    # ----- ingestion -----

    def on_trade_closed(
        self,
        closed_at: datetime,
        pnl: float,
        hold_seconds: float,
        spread_at_entry_points: float,
        slippage_points: float,
    ) -> list[HealthAlert]:
        rec = _TradeRecord(
            closed_at=closed_at,
            pnl=pnl,
            hold_seconds=hold_seconds,
            spread_at_entry_points=spread_at_entry_points,
            slippage_points=slippage_points,
        )
        self._trades.append(rec)

        alerts: list[HealthAlert] = []
        for check in (
            self._check_spread_regime,
            self._check_slippage_drift,
            self._check_wr_degradation,
            self._check_hold_time_floor,
        ):
            a = check(closed_at)
            if a is not None:
                alerts.append(a)
                self._fire(a)
        return alerts

    def on_modify_attempt(self, success: bool, now: datetime | None = None) -> HealthAlert | None:
        self._modify_outcomes.append(success)
        return self._check_modify_rejection(now or datetime.now(timezone.utc))

    def on_atr_snapshot(self, m5_atr: float, now: datetime | None = None) -> HealthAlert | None:
        if m5_atr <= 0:
            return None
        # Run the check BEFORE appending so a brand-new spike compares against
        # the prior 20-session baseline rather than including itself.
        alert = self._check_atr_expansion(m5_atr, now or datetime.now(timezone.utc))
        self._atrs.append(m5_atr)
        return alert

    def on_floating_dd(self, dd_usd: float, now: datetime | None = None) -> HealthAlert | None:
        return self._check_dd_proximity(dd_usd, now or datetime.now(timezone.utc))

    def on_entry(self, symbol: str, ts: datetime) -> HealthAlert | None:
        dq = self._entries_by_symbol.setdefault(symbol, deque())
        dq.append(ts)
        cutoff = ts - timedelta(minutes=self._t.trade_frequency_window_minutes)
        while dq and dq[0] < cutoff:
            dq.popleft()
        return self._check_trade_frequency(symbol, ts)

    # ----- checks -----

    def _check_spread_regime(self, now: datetime) -> HealthAlert | None:
        n = self._t.spread_baseline_window
        if len(self._trades) < n + 1:
            return None
        baseline = statistics.mean(t.spread_at_entry_points for t in list(self._trades)[-(n + 1):-1])
        latest = self._trades[-1].spread_at_entry_points
        if baseline <= 0:
            return None
        ratio = latest / baseline
        if ratio >= self._t.spread_multiplier:
            self._spread_consecutive_breaches += 1
        else:
            self._spread_consecutive_breaches = 0

        if self._spread_consecutive_breaches >= self._t.spread_consecutive_breach:
            return HealthAlert(
                signal=HealthSignal.SPREAD_REGIME,
                action=HealthAction.SUSPEND_ENTRIES,
                message=(
                    f"Spread regime shift: latest spread {latest:.1f} pts vs baseline "
                    f"{baseline:.1f} (ratio {ratio:.2f}x), {self._spread_consecutive_breaches} "
                    f"consecutive breaches"
                ),
                fired_at=now,
                metric_value=ratio,
                threshold=self._t.spread_multiplier,
            )
        return None

    def _check_slippage_drift(self, now: datetime) -> HealthAlert | None:
        n = self._t.slippage_window
        if len(self._trades) < n:
            return None
        recent = list(self._trades)[-n:]
        avg = statistics.mean(t.slippage_points for t in recent)
        if avg > self._t.slippage_avg_points_max:
            return HealthAlert(
                signal=HealthSignal.SLIPPAGE_DRIFT,
                action=HealthAction.SUSPEND_ENTRIES,
                message=f"Slippage drift: avg {avg:.1f} pts over last {n} trades > {self._t.slippage_avg_points_max} pts",
                fired_at=now,
                metric_value=avg,
                threshold=self._t.slippage_avg_points_max,
            )
        return None

    def _check_modify_rejection(self, now: datetime) -> HealthAlert | None:
        if len(self._modify_outcomes) < self._t.modify_window:
            return None
        rejections = sum(1 for ok in self._modify_outcomes if not ok)
        pct = 100.0 * rejections / len(self._modify_outcomes)
        if pct > self._t.modify_rejection_pct_max:
            alert = HealthAlert(
                signal=HealthSignal.MODIFY_REJECTION,
                action=HealthAction.ALERT_ONLY,
                message=f"Modify-rejection rate {pct:.1f}% over last {len(self._modify_outcomes)} > {self._t.modify_rejection_pct_max}%",
                fired_at=now,
                metric_value=pct,
                threshold=self._t.modify_rejection_pct_max,
            )
            self._fire(alert)
            return alert
        return None

    def _check_atr_expansion(self, m5_atr: float, now: datetime) -> HealthAlert | None:
        if len(self._atrs) < self._t.atr_session_window:
            return None
        median = statistics.median(self._atrs)
        if median <= 0:
            return None
        ratio = m5_atr / median
        if ratio > self._t.atr_expansion_multiplier:
            alert = HealthAlert(
                signal=HealthSignal.ATR_EXPANSION,
                action=HealthAction.SUSPEND_ENTRIES,
                message=f"ATR expansion: current {m5_atr:.4f} vs 20-session median {median:.4f} (ratio {ratio:.2f}x)",
                fired_at=now,
                metric_value=ratio,
                threshold=self._t.atr_expansion_multiplier,
            )
            self._fire(alert)
            return alert
        return None

    def _check_wr_degradation(self, now: datetime) -> HealthAlert | None:
        n = self._t.wr_window
        if len(self._trades) < n:
            return None
        recent = list(self._trades)[-n:]
        wins = sum(1 for t in recent if t.pnl > 0)
        wr = 100.0 * wins / len(recent)
        if wr < self._t.wr_floor_pct:
            return HealthAlert(
                signal=HealthSignal.WR_DEGRADATION,
                action=HealthAction.ALERT_ONLY,
                message=f"WR degradation: rolling-{n} WR {wr:.1f}% < floor {self._t.wr_floor_pct}%",
                fired_at=now,
                metric_value=wr,
                threshold=self._t.wr_floor_pct,
            )
        return None

    def _check_hold_time_floor(self, now: datetime) -> HealthAlert | None:
        n = self._t.hold_time_window
        if len(self._trades) < n:
            return None
        recent = list(self._trades)[-n:]
        breaches = sum(1 for t in recent if t.hold_seconds < self._t.hold_time_floor_seconds)
        pct = 100.0 * breaches / len(recent)
        if pct > self._t.hold_time_breach_pct_max:
            return HealthAlert(
                signal=HealthSignal.HOLD_TIME_FLOOR,
                action=HealthAction.ALERT_ONLY,
                message=(
                    f"Hold-time floor breach: {pct:.1f}% of last {n} trades closed in "
                    f"<{self._t.hold_time_floor_seconds}s — FundingPips toxic-flow risk"
                ),
                fired_at=now,
                metric_value=pct,
                threshold=self._t.hold_time_breach_pct_max,
            )
        return None

    def _check_dd_proximity(self, dd_usd: float, now: datetime) -> HealthAlert | None:
        if self._daily_dd_limit_usd <= 0:
            return None
        pct_of_limit = 100.0 * dd_usd / self._daily_dd_limit_usd
        if pct_of_limit >= self._t.dd_proximity_pct_of_limit:
            alert = HealthAlert(
                signal=HealthSignal.DD_PROXIMITY,
                action=HealthAction.AUTO_FLAT,
                message=(
                    f"Daily DD proximity: floating DD ${dd_usd:.2f} = {pct_of_limit:.1f}% "
                    f"of daily limit ${self._daily_dd_limit_usd:.2f} (>= {self._t.dd_proximity_pct_of_limit}%)"
                ),
                fired_at=now,
                metric_value=pct_of_limit,
                threshold=self._t.dd_proximity_pct_of_limit,
            )
            self._fire(alert)
            return alert
        return None

    def _check_trade_frequency(self, symbol: str, now: datetime) -> HealthAlert | None:
        dq = self._entries_by_symbol.get(symbol)
        if not dq:
            return None
        if len(dq) > self._t.trade_frequency_max_entries:
            alert = HealthAlert(
                signal=HealthSignal.TRADE_FREQUENCY,
                action=HealthAction.ALERT_ONLY,
                message=(
                    f"Trade frequency spike: {len(dq)} entries on {symbol} in last "
                    f"{self._t.trade_frequency_window_minutes}min (cap {self._t.trade_frequency_max_entries})"
                ),
                fired_at=now,
                metric_value=float(len(dq)),
                threshold=float(self._t.trade_frequency_max_entries),
            )
            self._fire(alert)
            return alert
        return None

    # ----- dispatch + state -----

    def _fire(self, alert: HealthAlert) -> None:
        # Latch suspend/auto-flat actions to avoid spamming the alert callback
        prev = self._latched.get(alert.signal)
        if prev is not None and alert.action == prev.action:
            # Same signal already in latched state — don't re-emit
            return
        if alert.action in (HealthAction.SUSPEND_ENTRIES, HealthAction.AUTO_FLAT):
            self._latched[alert.signal] = alert
        try:
            self._alert_cb(alert)
        except Exception:
            logger.warning("StrategyHealthMonitor alert callback raised", exc_info=True)

    def is_entries_suspended(self) -> bool:
        """True iff any latched alert demands SUSPEND_ENTRIES or AUTO_FLAT."""
        return any(
            a.action in (HealthAction.SUSPEND_ENTRIES, HealthAction.AUTO_FLAT)
            for a in self._latched.values()
        )

    def latched_alerts(self) -> list[HealthAlert]:
        return list(self._latched.values())

    def clear_latched(self, signal: HealthSignal | None = None) -> None:
        """Clear one signal (or all if None) from the latched set.

        Used by the operator after manually verifying conditions returned
        to normal — we deliberately do not auto-clear because the WHOLE
        point of latching is to prevent the bot from oscillating in a
        degraded regime.
        """
        if signal is None:
            self._latched.clear()
            self._spread_consecutive_breaches = 0
        else:
            self._latched.pop(signal, None)
            if signal == HealthSignal.SPREAD_REGIME:
                self._spread_consecutive_breaches = 0

    # ----- diagnostics -----

    @property
    def stats(self) -> dict:
        return {
            "trades_seen": len(self._trades),
            "modify_attempts_seen": len(self._modify_outcomes),
            "atr_snapshots": len(self._atrs),
            "latched_alerts": [a.signal.value for a in self._latched.values()],
            "entry_windows": {sym: len(dq) for sym, dq in self._entries_by_symbol.items()},
        }
