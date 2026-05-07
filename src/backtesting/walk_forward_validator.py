"""Walk-forward validator with Bailey & López de Prado Deflated Sharpe Ratio.

Strategy backtests look great in-sample because parameter space is huge and
we can always find a window where they shine. Walk-forward + DSR catches
this:

1. Walk-forward — slide a (train_months, test_months) window through the
   full history, recording test-window metrics independently. The aggregate
   test-window distribution tells you how the strategy performs on data it
   never saw during fitting.
2. Deflated Sharpe Ratio — adjusts the observed Sharpe for: (a) skewness
   and kurtosis of returns, and (b) the implicit "trials" of the parameter
   sweep. References:
       Bailey, D. & López de Prado, M. (2014).
       "The Deflated Sharpe Ratio: Correcting for Selection Bias,
        Backtest Overfitting, and Non-Normality."
       Journal of Portfolio Management.

Output: rolling list of WindowMetric objects + aggregate WalkForwardResult
including DSR probability that the observed Sharpe is real.

The validator is data-source agnostic: caller supplies a sequence of
TradeRecord (closed_at, pnl, ...). This keeps it usable for both tick-
replay backtests and live-trading audits.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TradeRecord:
    """One closed trade for the validator."""

    closed_at: datetime
    pnl: float


@dataclass
class WindowMetric:
    """Metrics for a single test window."""

    train_start: datetime
    train_end: datetime
    test_start: datetime
    test_end: datetime
    trade_count: int
    win_rate: float           # 0.0-1.0
    profit_factor: float      # gross_wins / gross_losses, inf if no losses
    sharpe: float             # daily-equivalent Sharpe (sqrt(252) annualised version below)
    sharpe_annual: float
    max_drawdown_pct: float   # 0.0-100.0
    total_pnl: float


@dataclass
class WalkForwardResult:
    """Aggregate result across all walk-forward windows."""

    windows: list[WindowMetric] = field(default_factory=list)
    aggregate_trade_count: int = 0
    aggregate_win_rate: float = 0.0
    aggregate_profit_factor: float = 0.0
    aggregate_sharpe: float = 0.0
    aggregate_sharpe_annual: float = 0.0
    aggregate_max_dd_pct: float = 0.0
    deflated_sharpe_ratio: float = 0.0      # probability ∈ [0, 1]
    aggregate_total_pnl: float = 0.0
    sample_sufficient: bool = False         # ≥385 aggregate trades for 95% confidence

    def passes_gate(
        self,
        min_dsr: float = 0.5,
        min_trades: int = 385,
        min_pf: float = 1.3,
        max_dd_pct: float = 8.0,
    ) -> tuple[bool, list[str]]:
        """Evidence-gated ship/no-ship decision.

        Returns (passed, reasons-it-failed). Defaults match plan spec:
        DSR > 0.5, ≥ 385 aggregate trades, PF > 1.3, max DD ≤ 8%.
        """
        failures: list[str] = []
        if self.deflated_sharpe_ratio < min_dsr:
            failures.append(f"DSR {self.deflated_sharpe_ratio:.3f} < {min_dsr}")
        if self.aggregate_trade_count < min_trades:
            failures.append(f"trade_count {self.aggregate_trade_count} < {min_trades}")
        if self.aggregate_profit_factor < min_pf:
            failures.append(f"PF {self.aggregate_profit_factor:.2f} < {min_pf}")
        if self.aggregate_max_dd_pct > max_dd_pct:
            failures.append(f"max_dd {self.aggregate_max_dd_pct:.2f}% > {max_dd_pct}%")
        return (len(failures) == 0, failures)


# ---- Statistical helpers ----


def _profit_factor(pnls: list[float]) -> float:
    wins = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    if losses <= 0:
        return float("inf") if wins > 0 else 0.0
    return wins / losses


def _max_drawdown_pct(pnls: list[float]) -> float:
    """Max equity-curve drawdown as percent of cumulative peak.

    Conservative: starting equity = 0, so the drawdown is computed off the
    running peak. If the equity goes negative on the first trade, this
    yields 100% drawdown which is the right intuition.
    """
    if not pnls:
        return 0.0
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        if peak > 0:
            dd_pct = 100.0 * (peak - equity) / peak
            max_dd = max(max_dd, dd_pct)
    return max_dd


def _sharpe(pnls: list[float], periods_per_year: int = 252) -> tuple[float, float]:
    """(per-trade Sharpe, annualised Sharpe). Stdev=0 → 0.0."""
    if len(pnls) < 2:
        return 0.0, 0.0
    mean = statistics.mean(pnls)
    sd = statistics.stdev(pnls)
    if sd == 0:
        return 0.0, 0.0
    sharpe = mean / sd
    return sharpe, sharpe * math.sqrt(periods_per_year)


def _skewness(values: list[float]) -> float:
    """Sample skewness (Fisher-Pearson). 0 for symmetric returns."""
    n = len(values)
    if n < 3:
        return 0.0
    mean = statistics.mean(values)
    sd = statistics.stdev(values)
    if sd == 0:
        return 0.0
    cube = sum(((v - mean) / sd) ** 3 for v in values) / n
    return cube * (n / ((n - 1) * (n - 2))) * (n - 1)


def _excess_kurtosis(values: list[float]) -> float:
    """Sample excess kurtosis. 0 for normal distribution."""
    n = len(values)
    if n < 4:
        return 0.0
    mean = statistics.mean(values)
    sd = statistics.stdev(values)
    if sd == 0:
        return 0.0
    fourth = sum(((v - mean) / sd) ** 4 for v in values) / n
    return fourth - 3.0


def _normal_cdf(x: float) -> float:
    """Standard normal CDF via math.erf — no scipy dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _normal_quantile(p: float) -> float:
    """Inverse standard-normal CDF (probit). Beasley-Springer-Moro approx.

    Sufficient for DSR's expected-max-of-N computation; we never need extreme
    tails in this codebase.
    """
    if p <= 0.0 or p >= 1.0:
        # DSR uses p ∈ (0.5, 1) so we should never hit this in practice.
        return 0.0 if p <= 0 else 8.0
    a = (
        -3.969683028665376e1, 2.209460984245205e2,
        -2.759285104469687e2, 1.383577518672690e2,
        -3.066479806614716e1, 2.506628277459239,
    )
    b = (
        -5.447609879822406e1, 1.615858368580409e2,
        -1.556989798598866e2, 6.680131188771972e1,
        -1.328068155288572e1,
    )
    c = (
        -7.784894002430293e-3, -3.223964580411365e-1,
        -2.400758277161838, -2.549732539343734,
        4.374664141464968, 2.938163982698783,
    )
    d = (
        7.784695709041462e-3, 3.224671290700398e-1,
        2.445134137142996, 3.754408661907416,
    )
    plow = 0.02425
    phigh = 1 - plow
    if p < plow:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    if p > phigh:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1
        )
    q = p - 0.5
    r = q * q
    return (
        (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q
    ) / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def deflated_sharpe_ratio(
    pnls: list[float],
    num_trials: int = 1,
    benchmark_sharpe: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """DSR per Bailey & López de Prado (2014).

    Returns the *probability* (∈ [0, 1]) that the observed Sharpe is real
    given:
      - sample size T
      - higher-moment shape (skew, excess kurtosis)
      - implicit trial count from parameter sweeping

    Higher num_trials → higher bar to cross. num_trials=1 reduces to the
    Probabilistic Sharpe Ratio (PSR).

    Returns 0.0 for sample sizes < 5 (not enough to compute moments).
    """
    n = len(pnls)
    if n < 5:
        return 0.0
    sr, sr_annual = _sharpe(pnls, periods_per_year=periods_per_year)
    if sr == 0:
        return 0.5  # no signal, no anti-signal
    skew = _skewness(pnls)
    kurt = _excess_kurtosis(pnls)

    # Expected max Sharpe under null — Euler-Mascheroni approximation per
    # Bailey & López de Prado eq.(7).
    if num_trials <= 1:
        sr0 = benchmark_sharpe
    else:
        em = 0.5772156649015329  # Euler-Mascheroni constant
        z1 = _normal_quantile(1 - 1.0 / num_trials)
        z2 = _normal_quantile(1 - 1.0 / (num_trials * math.e))
        sr0 = benchmark_sharpe + (1 - em) * z1 + em * z2

    # Bailey & López de Prado variance: σ²(SR) = (1 - γ3·SR + (γ4−1)/4·SR²) / (T−1)
    # where γ4 is RAW kurtosis. _excess_kurtosis returns γ4 − 3, so we need
    # (γ4 − 1)/4 = (excess + 2)/4.
    denom_sq = 1 - skew * sr + ((kurt + 2.0) / 4.0) * sr * sr
    if denom_sq <= 0:
        # Heavy-tail or skew can drive variance estimate negative on small
        # samples — fall back to neutral DSR rather than spurious certainty.
        return 0.5
    sigma_sr = math.sqrt(denom_sq) / math.sqrt(n - 1)
    if sigma_sr == 0:
        return 0.5

    z = (sr - sr0) / sigma_sr
    return _normal_cdf(z)


# ---- Walk-forward orchestration ----


def _bucket_by_window(
    trades: list[TradeRecord],
    start: datetime,
    end: datetime,
) -> list[TradeRecord]:
    return [t for t in trades if start <= t.closed_at < end]


def _summarize(trades: list[TradeRecord], periods_per_year: int) -> tuple[int, float, float, float, float, float, float]:
    if not trades:
        return 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
    pnls = [t.pnl for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    wr = wins / len(pnls)
    pf = _profit_factor(pnls)
    sr, sr_a = _sharpe(pnls, periods_per_year=periods_per_year)
    dd = _max_drawdown_pct(pnls)
    return len(trades), wr, pf, sr, sr_a, dd, sum(pnls)


class WalkForwardValidator:
    """Rolling-window validator.

    Args:
        train_months: in-sample window length
        test_months: out-of-sample window length per step
        step_months: how far to advance between windows. Defaults to test_months
                     (non-overlapping test windows = each trade tested at most once).
        periods_per_year: 252 for daily, 365 for crypto, etc. Affects annualised
                          Sharpe computation only.
    """

    def __init__(
        self,
        train_months: int = 12,
        test_months: int = 3,
        step_months: int | None = None,
        periods_per_year: int = 252,
    ) -> None:
        self._train = train_months
        self._test = test_months
        self._step = step_months or test_months
        self._periods_per_year = periods_per_year

    def validate(
        self,
        trades: Iterable[TradeRecord],
        history_start: datetime,
        history_end: datetime,
        num_trials: int = 1,
    ) -> WalkForwardResult:
        """Run walk-forward. Returns aggregated WalkForwardResult.

        `num_trials` is the implicit parameter-sweep count. Bake your
        knowledge of how many strategy variants were tried in here (e.g.
        if you grid-searched 50 combinations of EMA periods + ATR mults,
        pass 50). Default 1 = PSR.
        """
        trade_list = sorted(trades, key=lambda t: t.closed_at)
        if not trade_list:
            return WalkForwardResult()

        # Walk windows
        windows: list[WindowMetric] = []
        cur = history_start
        while True:
            train_start = cur
            train_end = train_start + timedelta(days=30 * self._train)
            test_start = train_end
            test_end = test_start + timedelta(days=30 * self._test)
            if test_end > history_end + timedelta(days=1):
                break

            test_trades = _bucket_by_window(trade_list, test_start, test_end)
            tcount, wr, pf, sr, sr_a, dd, total = _summarize(test_trades, self._periods_per_year)
            windows.append(WindowMetric(
                train_start=train_start,
                train_end=train_end,
                test_start=test_start,
                test_end=test_end,
                trade_count=tcount,
                win_rate=wr,
                profit_factor=pf if pf != float("inf") else 0.0,
                sharpe=sr,
                sharpe_annual=sr_a,
                max_drawdown_pct=dd,
                total_pnl=total,
            ))
            cur = cur + timedelta(days=30 * self._step)

        # Aggregate over union of test windows (no double-counting)
        all_test_trades: list[TradeRecord] = []
        seen_at: set[datetime] = set()
        for w in windows:
            for t in trade_list:
                if w.test_start <= t.closed_at < w.test_end and t.closed_at not in seen_at:
                    all_test_trades.append(t)
                    seen_at.add(t.closed_at)

        agg_count, agg_wr, agg_pf, agg_sr, agg_sr_a, agg_dd, agg_total = _summarize(
            all_test_trades, self._periods_per_year
        )
        dsr = deflated_sharpe_ratio(
            [t.pnl for t in all_test_trades],
            num_trials=num_trials,
            periods_per_year=self._periods_per_year,
        )

        return WalkForwardResult(
            windows=windows,
            aggregate_trade_count=agg_count,
            aggregate_win_rate=agg_wr,
            aggregate_profit_factor=agg_pf if agg_pf != float("inf") else 0.0,
            aggregate_sharpe=agg_sr,
            aggregate_sharpe_annual=agg_sr_a,
            aggregate_max_dd_pct=agg_dd,
            deflated_sharpe_ratio=dsr,
            aggregate_total_pnl=agg_total,
            sample_sufficient=(agg_count >= 385),
        )
