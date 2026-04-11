"""Tests for PropFirmGuard — payout reset and directional exposure."""

from datetime import datetime, timezone

from src.risk.prop_firm_guard import PropFirmConfig, PropFirmGuard


def test_reset_after_payout_recalculates_limits():
    guard = PropFirmGuard(PropFirmConfig(account_size=5000, phase="master"))
    guard.reset_after_payout(4600)

    assert guard._config.account_size == 4600
    assert guard.dd_floor == 4600 * 0.90  # $4,140
    assert guard.daily_limit == 4600 * 0.05  # $230
    assert guard._dd_tiers[0] == (4600 * 1.04, 1.0)
    assert guard._dd_tiers[1] == (4600, 0.8)
    assert guard._dd_tiers[2] == (4600 * 0.96, 0.5)
    assert guard._dd_tiers[3] == (4600 * 0.92, 0.3)
    assert guard._daily_pnl == 0.0
    assert guard._daily_start_equity == 4600
    assert len(guard._recent_trades) == 0


def test_reset_after_payout_with_usd_buffers():
    guard = PropFirmGuard(PropFirmConfig(
        account_size=5000, phase="master",
        safety_buffer_daily_usd=7.0, safety_buffer_dd_usd=7.0,
    ))
    guard.reset_after_payout(4600)

    # Floor = 4600 * 0.90 = 4140, buffer = 4140 + 7 = 4147
    assert guard._dd_floor_with_buffer == 4600 * 0.90 + 7.0
    # Daily = 4600 * 0.05 = 230, buffer = 230 - 7 = 223
    assert guard._daily_limit_with_buffer == 4600 * 0.05 - 7.0


def test_directional_exposure_blocks_excess():
    guard = PropFirmGuard(PropFirmConfig(
        account_size=5000, max_directional_positions=2,
    ))

    class FakePos:
        def __init__(self, side_val):
            self.side = type("Side", (), {"value": side_val})()

    positions = [FakePos("BUY"), FakePos("BUY")]
    assert guard.check_directional_exposure(positions, "BUY") is False
    assert guard.check_directional_exposure(positions, "SELL") is True


def test_directional_exposure_allows_within_limit():
    guard = PropFirmGuard(PropFirmConfig(
        account_size=5000, max_directional_positions=2,
    ))

    class FakePos:
        def __init__(self, side_val):
            self.side = type("Side", (), {"value": side_val})()

    positions = [FakePos("BUY")]
    assert guard.check_directional_exposure(positions, "BUY") is True
    assert guard.check_directional_exposure(positions, "SELL") is True
