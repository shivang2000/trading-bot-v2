"""Backtest engine — synchronous bar-by-bar replay for V2 strategies.

Replays M15 bars one at a time, calls strategy scan() methods with the
exact same interface as live trading, and simulates position management
(open, SL/TP, trailing stop, close).

No asyncio dependency — strategies' async scan() methods are called via
asyncio.run() for simplicity and speed.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import pandas as pd
import pandas_ta as ta

from src.analysis.regime import MarketRegime, RegimeDetector, detect_regime_from_ohlcv
from src.analysis.strategies.ema_pullback import EmaPullbackStrategy
from src.analysis.strategies.london_breakout import LondonBreakoutStrategy
from src.backtesting.account import BacktestAccountManager
from src.backtesting.result import BacktestResult, calculate_metrics
from src.config.schema import AppConfig, EmaPullbackConfig, LondonBreakoutConfig
from src.core.enums import OrderSide
from src.risk.trailing_stop import TrailingStopManager

logger = logging.getLogger(__name__)

WARMUP_BARS = 100  # Enough for EMA(50) + regime detection lookback


class BacktestEngine:
    """Synchronous bar-by-bar replay engine for V2 strategies."""

    def __init__(
        self,
        symbol: str,
        strategy: str = "both",  # "ema_pullback" | "london_breakout" | "both"
        initial_capital: float = 10_000.0,
        volume: float = 0.01,
        config: AppConfig | None = None,
        point_size: float = 0.01,
    ) -> None:
        self._symbol = symbol
        self._strategy_name = strategy
        self._initial_capital = initial_capital
        self._volume = volume
        self._point_size = point_size

        # Config
        ema_cfg = config.strategies.ema_pullback if config else EmaPullbackConfig()
        lb_cfg = config.strategies.london_breakout if config else LondonBreakoutConfig()

        # Strategies
        self._ema_pullback = EmaPullbackStrategy(ema_cfg) if strategy in ("ema_pullback", "both") else None
        self._london_breakout = LondonBreakoutStrategy(lb_cfg) if strategy in ("london_breakout", "both") else None

        # Regime detector
        self._regime_detector = RegimeDetector()

        # Trailing stop
        self._trailing_mgr = TrailingStopManager(atr_multiplier=1.5, activation_pct=0.5)

    def run(
        self,
        m15_data: pd.DataFrame,
        h1_data: pd.DataFrame,
    ) -> BacktestResult:
        """Run backtest over provided data. Returns BacktestResult with all metrics."""
        account = BacktestAccountManager(self._initial_capital)

        total_bars = len(m15_data)
        if total_bars < WARMUP_BARS + 10:
            logger.warning("Not enough M15 data (%d bars, need %d+)", total_bars, WARMUP_BARS)
            return self._empty_result(m15_data)

        logger.info(
            "Starting backtest: %s on %s (%d M15 bars, %d H1 bars)",
            self._strategy_name, self._symbol, total_bars, len(h1_data),
        )

        signals_generated = 0
        loop = asyncio.new_event_loop()

        try:
            for i in range(WARMUP_BARS, total_bars):
                bar = m15_data.iloc[i]
                bar_time = pd.Timestamp(bar["time"]).to_pydatetime()
                if bar_time.tzinfo is None:
                    bar_time = bar_time.replace(tzinfo=timezone.utc)

                bar_high = float(bar["high"])
                bar_low = float(bar["low"])
                bar_close = float(bar["close"])

                # 1. Check SL/TP on open positions FIRST
                account.check_sl_tp(self._symbol, bar_high, bar_low, bar_time)

                # 2. Update trailing stops
                self._update_trailing(account, m15_data, i, bar_close)

                # 3. Get regime from H1 data (no lookahead)
                regime = self._get_regime(h1_data, bar_time, bar_close)

                # 4. Run strategies (only if no open position)
                if not account.has_position(self._symbol):
                    m15_window = m15_data.iloc[max(0, i - 99): i + 1].copy()

                    signal = self._run_strategies(loop, m15_window, bar_time, regime)

                    if signal is not None:
                        side = OrderSide.BUY if signal.action == "BUY" else OrderSide.SELL
                        account.open_position(
                            symbol=self._symbol,
                            side=side,
                            volume=self._volume,
                            entry_price=signal.entry_price,
                            stop_loss=signal.stop_loss,
                            take_profit=signal.take_profit,
                            open_time=bar_time,
                            comment=signal.reason,
                        )
                        signals_generated += 1

                # 5. Update equity snapshot
                account.update_prices(self._symbol, bar_close, bar_time)

                # Progress logging every 5000 bars
                if i % 5000 == 0:
                    logger.info("  Bar %d/%d (%.0f%%)", i, total_bars, i / total_bars * 100)

        finally:
            loop.close()

        # Close remaining open positions at last bar
        last_bar = m15_data.iloc[-1]
        last_close = float(last_bar["close"])
        last_time = pd.Timestamp(last_bar["time"]).to_pydatetime()
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=timezone.utc)

        for pos in list(account.get_positions(self._symbol)):
            account.close_position(pos.ticket, last_close, last_time, "END_OF_BACKTEST")

        # Calculate metrics
        trades = account.get_trades()
        equity_curve = account.get_equity_curve()
        final_equity = equity_curve.iloc[-1] if not equity_curve.empty else self._initial_capital

        metrics = calculate_metrics(trades, equity_curve, self._initial_capital)

        first_time = pd.Timestamp(m15_data["time"].iloc[WARMUP_BARS]).to_pydatetime()
        last_time_dt = pd.Timestamp(m15_data["time"].iloc[-1]).to_pydatetime()

        result = BacktestResult(
            strategy_name=self._strategy_name,
            symbol=self._symbol,
            start_date=first_time,
            end_date=last_time_dt,
            initial_capital=self._initial_capital,
            final_equity=final_equity,
            trades=trades,
            equity_curve=equity_curve,
            **metrics,
        )

        logger.info(
            "Backtest complete: %d trades, %.2f%% return, %.2f%% max DD, %.1f%% WR",
            result.total_trades, result.total_return_pct,
            result.max_drawdown_pct, result.win_rate,
        )

        return result

    def _get_regime(
        self, h1_data: pd.DataFrame, current_time: datetime, current_price: float
    ) -> MarketRegime:
        """Get H1 regime using only bars <= current_time (no lookahead)."""
        h1_visible = h1_data[h1_data["time"] <= current_time]
        if len(h1_visible) < 60:
            return MarketRegime.CHOPPY  # Not enough data → skip

        return detect_regime_from_ohlcv(h1_visible, self._regime_detector, current_price)

    def _run_strategies(
        self,
        loop: asyncio.AbstractEventLoop,
        m15_window: pd.DataFrame,
        bar_time: datetime,
        regime: MarketRegime,
    ):
        """Run enabled strategies and return the first signal (or None)."""
        is_up = regime == MarketRegime.TRENDING_UP
        is_down = regime == MarketRegime.TRENDING_DOWN
        is_choppy = regime == MarketRegime.CHOPPY
        is_ranging = regime == MarketRegime.RANGING

        # EMA Pullback
        if self._ema_pullback and not is_choppy:
            sig = loop.run_until_complete(
                self._ema_pullback.scan(
                    symbol=self._symbol,
                    m15_bars=m15_window,
                    h1_regime_is_trending_up=is_up,
                    h1_regime_is_trending_down=is_down,
                    h1_regime_is_choppy=is_choppy,
                )
            )
            if sig:
                return sig

        # London Breakout
        if self._london_breakout and not is_choppy:
            sig = loop.run_until_complete(
                self._london_breakout.scan(
                    symbol=self._symbol,
                    m15_bars=m15_window,
                    point_size=self._point_size,
                    regime_is_choppy=is_choppy,
                    regime_is_ranging=is_ranging,
                    as_of=bar_time,
                )
            )
            if sig:
                return sig

        return None

    def _update_trailing(
        self,
        account: BacktestAccountManager,
        m15_data: pd.DataFrame,
        current_idx: int,
        current_price: float,
    ) -> None:
        """Update trailing stops for open positions using ATR."""
        positions = account.get_positions(self._symbol)
        if not positions:
            return

        # Calculate ATR from visible M15 data
        window = m15_data.iloc[max(0, current_idx - 20): current_idx + 1]
        if len(window) < 14:
            return

        atr_series = ta.atr(window["high"], window["low"], window["close"], length=14)
        if atr_series is None or atr_series.empty:
            return

        atr_val = float(atr_series.iloc[-1])
        if atr_val <= 0:
            return

        for pos in positions:
            new_sl = self._trailing_mgr.update(
                ticket=pos.ticket,
                side=pos.side,
                current_price=current_price,
                atr=atr_val,
                initial_sl=pos.stop_loss,
                take_profit=pos.take_profit,
                open_price=pos.open_price,
            )
            if new_sl is not None:
                pos.stop_loss = round(new_sl, 5)

    def _empty_result(self, m15_data: pd.DataFrame) -> BacktestResult:
        """Return an empty result when insufficient data."""
        return BacktestResult(
            strategy_name=self._strategy_name,
            symbol=self._symbol,
            start_date=datetime.now(timezone.utc),
            end_date=datetime.now(timezone.utc),
            initial_capital=self._initial_capital,
            final_equity=self._initial_capital,
        )
