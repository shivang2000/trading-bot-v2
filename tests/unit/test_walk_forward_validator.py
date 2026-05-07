"""Tests for walk-forward validator + Bailey & López de Prado DSR."""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from src.backtesting.walk_forward_validator import (
    TradeRecord,
    WalkForwardValidator,
    deflated_sharpe_ratio,
    _excess_kurtosis,
    _max_drawdown_pct,
    _profit_factor,
    _sharpe,
    _skewness,
    _normal_cdf,
    _normal_quantile,
)


# ---------- Statistical helpers ----------


class TestProfitFactor:
    def test_basic(self) -> None:
        assert _profit_factor([10.0, -5.0, 8.0, -3.0]) == pytest.approx((10 + 8) / (5 + 3))

    def test_no_losses(self) -> None:
        assert math.isinf(_profit_factor([1.0, 2.0]))

    def test_no_wins(self) -> None:
        assert _profit_factor([-1.0, -2.0]) == 0.0

    def test_empty(self) -> None:
        assert _profit_factor([]) == 0.0


class TestMaxDrawdown:
    def test_uptrend(self) -> None:
        # Strictly increasing equity → no drawdown
        assert _max_drawdown_pct([1.0, 2.0, 3.0]) == 0.0

    def test_drawdown(self) -> None:
        # Equity: +10 → +5 → -5
        # Peak after first trade = 10. Min equity after =-5? Actually:
        #   trade 1: +10 → equity 10, peak 10
        #   trade 2: -5 → equity 5, peak 10. dd = 50%
        #   trade 3: -10 → equity -5, peak 10. dd = 150% — but we cap at the
        # drawdown-of-peak so 150% is correct.
        result = _max_drawdown_pct([10.0, -5.0, -10.0])
        assert result == pytest.approx(150.0)

    def test_empty(self) -> None:
        assert _max_drawdown_pct([]) == 0.0


class TestSharpe:
    def test_returns_zero_when_stdev_zero(self) -> None:
        sr, sr_a = _sharpe([1.0, 1.0, 1.0])
        assert sr == 0.0
        assert sr_a == 0.0

    def test_basic(self) -> None:
        # Mean 1, stdev sqrt(0.5) → sharpe = sqrt(2)
        sr, sr_a = _sharpe([1.0, 0.0, 2.0, 1.0])
        assert sr > 0


class TestMoments:
    def test_normal_skew_near_zero(self) -> None:
        random.seed(0)
        vals = [random.gauss(0, 1) for _ in range(500)]
        assert abs(_skewness(vals)) < 0.4

    def test_positive_skew(self) -> None:
        # Right-skewed distribution
        vals = [0.1] * 50 + [10.0] * 1
        assert _skewness(vals) > 0

    def test_normal_kurt_near_zero(self) -> None:
        random.seed(1)
        vals = [random.gauss(0, 1) for _ in range(2000)]
        assert abs(_excess_kurtosis(vals)) < 0.5


class TestNormalDist:
    def test_cdf_at_zero(self) -> None:
        assert _normal_cdf(0.0) == pytest.approx(0.5)

    def test_cdf_at_two_sigma(self) -> None:
        assert _normal_cdf(1.96) == pytest.approx(0.975, abs=1e-3)

    def test_quantile_round_trip(self) -> None:
        for p in [0.1, 0.25, 0.5, 0.75, 0.9]:
            x = _normal_quantile(p)
            assert _normal_cdf(x) == pytest.approx(p, abs=1e-3)


# ---------- DSR ----------


class TestDeflatedSharpe:
    def test_low_sample_returns_zero(self) -> None:
        assert deflated_sharpe_ratio([1.0, 2.0]) == 0.0

    def test_strong_signal_high_dsr(self) -> None:
        # Almost-deterministic profitable strategy: 100 wins of +1.0
        pnls = [1.0 + 0.01 * i for i in range(100)]  # tiny variance, all positive
        dsr = deflated_sharpe_ratio(pnls, num_trials=1)
        assert dsr > 0.99

    def test_no_signal_dsr_around_half(self) -> None:
        # Many noise trials → average DSR converges to 0.5. We sample a few
        # seeds and assert the *mean* is near 0.5; any single seed can drift
        # because finite sample mean is non-zero by chance.
        dsrs: list[float] = []
        for seed in range(20):
            random.seed(seed)
            pnls = [random.gauss(0, 1) for _ in range(500)]
            dsrs.append(deflated_sharpe_ratio(pnls, num_trials=1))
        avg = sum(dsrs) / len(dsrs)
        assert 0.35 < avg < 0.65

    def test_more_trials_lower_dsr(self) -> None:
        # Same observed Sharpe; more trials → harder to clear the bar
        random.seed(7)
        pnls = [random.gauss(0.05, 1) for _ in range(500)]
        dsr_1 = deflated_sharpe_ratio(pnls, num_trials=1)
        dsr_50 = deflated_sharpe_ratio(pnls, num_trials=50)
        assert dsr_50 < dsr_1


# ---------- WalkForwardValidator ----------


def _utc(year=2026, month=1, day=1) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


def _build_trades(start: datetime, count: int, pnl_fn) -> list[TradeRecord]:
    return [TradeRecord(closed_at=start + timedelta(days=i), pnl=pnl_fn(i)) for i in range(count)]


class TestWalkForwardValidator:
    def test_empty_input_returns_empty_result(self) -> None:
        v = WalkForwardValidator(train_months=6, test_months=3)
        result = v.validate([], _utc(2018), _utc(2020))
        assert result.windows == []
        assert result.aggregate_trade_count == 0

    def test_single_window_aggregates_test_trades(self) -> None:
        # Train Jan-Jun 2018, Test Jul-Sep 2018, advance 3mo, etc.
        v = WalkForwardValidator(train_months=6, test_months=3)
        trades = _build_trades(_utc(2018, 1, 1), 365, lambda i: 1.0 if i % 2 == 0 else -0.5)
        result = v.validate(trades, _utc(2018, 1, 1), _utc(2019, 6, 30))
        assert result.windows
        assert result.aggregate_trade_count > 0
        assert 0.0 <= result.aggregate_win_rate <= 1.0
        assert 0.0 <= result.deflated_sharpe_ratio <= 1.0

    def test_passes_gate_with_strong_signal(self) -> None:
        v = WalkForwardValidator(train_months=6, test_months=3)
        # 1000 trades: 70% wins of +2, 30% losses of -1 → PF ~4.7, strong WR
        random.seed(0)
        trades: list[TradeRecord] = []
        start = _utc(2018, 1, 1)
        for i in range(1000):
            pnl = 2.0 if random.random() < 0.7 else -1.0
            trades.append(TradeRecord(closed_at=start + timedelta(hours=12 * i), pnl=pnl))
        result = v.validate(trades, start, start + timedelta(hours=12 * 1001))
        # With 70/30 hit rate, randomness produces multi-trade losing streaks
        # so DD can run high — relax DD threshold for this synthetic case.
        passed, fails = result.passes_gate(min_dsr=0.5, min_trades=300, min_pf=1.3, max_dd_pct=80.0)
        assert passed, f"Expected gate to pass, but: {fails}"

    def test_fails_gate_with_no_edge(self) -> None:
        v = WalkForwardValidator(train_months=6, test_months=3)
        random.seed(0)
        # Symmetric noise = no edge
        trades = [
            TradeRecord(closed_at=_utc(2018, 1, 1) + timedelta(hours=12 * i),
                       pnl=random.gauss(0, 1))
            for i in range(800)
        ]
        result = v.validate(trades, _utc(2018, 1, 1), _utc(2018, 1, 1) + timedelta(hours=12 * 801))
        passed, fails = result.passes_gate()
        assert not passed
        assert any("PF" in f or "DSR" in f for f in fails)

    def test_dsr_returned_in_result(self) -> None:
        v = WalkForwardValidator(train_months=6, test_months=3)
        random.seed(1)
        trades = [
            TradeRecord(closed_at=_utc(2018, 1, 1) + timedelta(hours=12 * i),
                       pnl=random.gauss(0.5, 1))
            for i in range(800)
        ]
        result = v.validate(trades, _utc(2018, 1, 1), _utc(2018, 1, 1) + timedelta(hours=12 * 801))
        assert 0.0 <= result.deflated_sharpe_ratio <= 1.0

    def test_sample_sufficient_flag(self) -> None:
        v = WalkForwardValidator(train_months=3, test_months=3)
        # Many trades to clear 385 threshold
        trades = _build_trades(_utc(2018, 1, 1), 500, lambda i: 1.0)
        result = v.validate(trades, _utc(2018, 1, 1), _utc(2019, 6, 30))
        assert result.aggregate_trade_count >= 385
        assert result.sample_sufficient is True

        # Few trades — flag should be False
        v2 = WalkForwardValidator(train_months=6, test_months=3)
        trades2 = _build_trades(_utc(2018, 1, 1), 50, lambda i: 1.0)
        result2 = v2.validate(trades2, _utc(2018, 1, 1), _utc(2019, 6, 30))
        assert result2.sample_sufficient is False

    def test_num_trials_penalises_more(self) -> None:
        v = WalkForwardValidator(train_months=3, test_months=3)
        random.seed(99)
        trades = [
            TradeRecord(closed_at=_utc(2018, 1, 1) + timedelta(hours=12 * i),
                       pnl=random.gauss(0.1, 1))
            for i in range(800)
        ]
        r_one = v.validate(trades, _utc(2018, 1, 1), _utc(2018, 1, 1) + timedelta(hours=12 * 801), num_trials=1)
        r_many = v.validate(trades, _utc(2018, 1, 1), _utc(2018, 1, 1) + timedelta(hours=12 * 801), num_trials=100)
        assert r_many.deflated_sharpe_ratio <= r_one.deflated_sharpe_ratio
