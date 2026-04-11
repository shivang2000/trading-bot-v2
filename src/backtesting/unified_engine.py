"""Unified backtest engine -- runs M15 + M5 strategies on ONE shared account.

Drives on M5 bars as the clock. M15 strategies fire when a new M15 bar
completes (every 3rd M5 bar). All strategies share the same BacktestAccountManager
so equity, positions, and risk are unified -- simulating real-world trading.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, timezone

import pandas as pd

from src.analysis.regime import MarketRegime, RegimeDetector, detect_regime_from_ohlcv
from src.backtesting.account import BacktestAccountManager
from src.backtesting.cost_model import CostModel
from src.backtesting.result import BacktestResult, calculate_metrics
from src.core.enums import OrderSide
from src.risk.trailing_stop import TrailingStopManager

logger = logging.getLogger(__name__)

WARMUP_BARS = 200


class UnifiedBacktestEngine:
    """Runs M15 + M5 strategies on a shared account with M5 bar clock."""

    def __init__(
        self,
        symbol: str,
        m15_strategies: list | None = None,
        m5_strategies: list | None = None,
        initial_capital: float = 100.0,
        cost_model: CostModel | None = None,
        profit_growth_factor: float = 0.50,
        max_total_positions: int = 10,
        max_per_strategy: int = 1,
        point_size: float = 0.01,
        tick_value: float = 0.01,
        risk_pct: float = 1.0,
        max_lot: float = 0.50,
        max_daily_trades: int = 200,
        daily_loss_limit_pct: float = 5.0,
    ) -> None:
        self._symbol = symbol
        self._m15_strategies = m15_strategies or []
        self._m5_strategies = m5_strategies or []
        self._initial_capital = initial_capital
        self._cost_model = cost_model
        self._profit_growth_factor = profit_growth_factor
        self._max_total_positions = max_total_positions
        self._max_per_strategy = max_per_strategy
        self._point_size = point_size
        self._tick_value = tick_value
        self._risk_pct = risk_pct
        self._max_lot = max_lot
        self._max_daily_trades = max_daily_trades
        self._daily_loss_limit = daily_loss_limit_pct
        self._regime_detector = RegimeDetector()
        self._trailing_manager = TrailingStopManager(
            atr_multiplier=1.5, activation_pct=0.2,
            giveback_pct=0.10, max_giveback=10.0, activation_profit=5.0,
        )
        self._current_date = ""
        self._daily_trade_count = 0
        self._daily_start_equity = 0.0

    def run(
        self, m5_data: pd.DataFrame,
        m15_data: pd.DataFrame | None = None,
        h1_data: pd.DataFrame | None = None,
    ) -> BacktestResult:
        """Run unified backtest. M5 bars drive the main loop clock."""
        if m15_data is None or m15_data.empty:
            m15_data = self._resample(m5_data, "15min")
        if h1_data is None or h1_data.empty:
            h1_data = self._resample(m5_data, "1h")

        logger.info(
            "Unified backtest: %s (M5=%d, M15=%d, H1=%d, %d M15 strats, %d M5 strats)",
            self._symbol, len(m5_data), len(m15_data), len(h1_data),
            len(self._m15_strategies), len(self._m5_strategies),
        )

        account = BacktestAccountManager(
            self._initial_capital, tick_value=self._tick_value,
            point_size=self._point_size,
        )
        self._daily_start_equity = self._initial_capital
        total_bars = len(m5_data)
        if total_bars < WARMUP_BARS + 10:
            return self._empty_result()

        signals_generated = 0
        last_m15_minute = -1
        loop = asyncio.new_event_loop()

        try:
            for i in range(WARMUP_BARS, total_bars):
                bar = m5_data.iloc[i]
                bar_time = pd.Timestamp(bar["time"]).to_pydatetime()
                if bar_time.tzinfo is None:
                    bar_time = bar_time.replace(tzinfo=timezone.utc)
                bar_high, bar_low = float(bar["high"]), float(bar["low"])
                bar_close = float(bar["close"])

                # 1. Check SL/TP then trailing stops on ALL open positions
                account.check_sl_tp(self._symbol, bar_high, bar_low, bar_time)
                self._update_trailing(account, bar_close)

                # 2. Check daily limits
                if not self._check_daily_limits(bar_time, account):
                    account.update_prices(self._symbol, bar_close, bar_time)
                    continue

                # 3. Regime + data windows (no lookahead)
                regime = self._get_regime(h1_data, bar_time, bar_close)
                m5_window = m5_data.iloc[max(0, i - 199):i + 1].copy()
                m15_window = m15_data[m15_data["time"] <= bar_time].tail(100).copy()
                h1_window = h1_data[h1_data["time"] <= bar_time].tail(60).copy()

                # 4. Run M5 scalping strategies (every bar)
                for strat in self._m5_strategies:
                    sig = self._invoke(
                        loop, strat, m5_bars=m5_window, m15_bars=m15_window,
                        h1_bars=h1_window, bar_time=bar_time, regime=regime,
                    )
                    if sig is not None:
                        # Check position limit using signal.strategy_name (matches comment prefix)
                        if not self._can_open(account, sig.strategy_name):
                            continue
                        self._open_trade(account, sig, bar, bar_time)
                        signals_generated += 1

                # 5. Run M15 strategies (only on new M15 candle)
                cur_m15 = (bar_time.hour * 60 + bar_time.minute) // 15
                is_new_m15 = cur_m15 != last_m15_minute
                last_m15_minute = cur_m15

                if is_new_m15 and len(m15_window) >= 30:
                    for strat in self._m15_strategies:
                        sig = self._invoke(
                            loop, strat, m15_bars=m15_window, h1_bars=h1_window,
                            bar_time=bar_time, regime=regime, m5_bars=m5_window,
                        )
                        if sig is not None:
                            sname = sig.strategy_name or strat.__class__.__name__
                            if not self._can_open(account, sname):
                                continue
                            self._open_trade(account, sig, bar, bar_time)
                            signals_generated += 1

                # 6. Update equity
                account.update_prices(self._symbol, bar_close, bar_time)
                if i % 10_000 == 0:
                    logger.info("  Bar %d/%d (%.0f%%)", i, total_bars, i / total_bars * 100)
        finally:
            loop.close()

        # Close remaining positions at last bar
        last_bar = m5_data.iloc[-1]
        last_close = float(last_bar["close"])
        last_time = pd.Timestamp(last_bar["time"]).to_pydatetime()
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=timezone.utc)
        for pos in list(account.get_positions(self._symbol)):
            account.close_position(pos.ticket, last_close, last_time, "END_OF_BACKTEST")

        # Build result
        trades = account.get_trades()
        eq = account.get_equity_curve()
        final_equity = eq.iloc[-1] if not eq.empty else self._initial_capital
        metrics = calculate_metrics(trades, eq, self._initial_capital)
        all_names = ([s.__class__.__name__ for s in self._m15_strategies]
                     + [s.__class__.__name__ for s in self._m5_strategies])
        result = BacktestResult(
            strategy_name="unified:" + "+".join(all_names), symbol=self._symbol,
            start_date=pd.Timestamp(m5_data["time"].iloc[WARMUP_BARS]).to_pydatetime(),
            end_date=last_time, initial_capital=self._initial_capital,
            final_equity=final_equity, trades=trades, equity_curve=eq, **metrics,
        )
        logger.info(
            "Unified done: %d trades, %.2f%% ret, %.2f%% DD, %.1f%% WR",
            result.total_trades, result.total_return_pct,
            result.max_drawdown_pct, result.win_rate,
        )
        return result

    # -- Helpers ------------------------------------------------------------

    def _can_open(self, account: BacktestAccountManager, strategy_name: str) -> bool:
        positions = account.get_positions(self._symbol)
        if len(positions) >= self._max_total_positions:
            return False
        strat_pos = [p for p in positions if p.comment.startswith(strategy_name + ":")]
        return len(strat_pos) < self._max_per_strategy

    def _open_trade(self, account: BacktestAccountManager, signal, bar, bar_time: datetime) -> None:
        side = OrderSide.BUY if signal.action == "BUY" else OrderSide.SELL
        entry = signal.entry_price
        if self._cost_model is not None:
            bar_spread = float(bar.get("spread", 0)) if hasattr(bar, "get") else 0
            if bar_spread > 0 and self._cost_model._spread_from_data:
                if self._cost_model.should_skip_trade(bar_spread):
                    return
                spread = bar_spread * self._point_size
            else:
                spread = self._cost_model.get_spread(bar_time)
            entry = self._cost_model.adjust_entry_for_spread(
                entry, signal.action, spread, self._point_size,
            )
        volume = self._calc_volume(account, entry, signal.stop_loss)
        comment = (f"{signal.strategy_name}:{signal.reason}"
                   if signal.strategy_name else signal.reason)
        account.open_position(
            symbol=self._symbol, side=side, volume=volume, entry_price=entry,
            stop_loss=signal.stop_loss, take_profit=signal.take_profit,
            open_time=bar_time, comment=comment,
        )
        self._daily_trade_count += 1

    def _invoke(self, loop: asyncio.AbstractEventLoop, strategy, **data_kwargs):
        """Invoke strategy scan() with kwargs matched via introspection."""
        try:
            kwargs: dict = {"symbol": self._symbol, "point_size": self._point_size}
            kwargs["as_of"] = data_kwargs.pop("bar_time", None)
            regime = data_kwargs.pop("regime", None)

            sig = inspect.signature(strategy.scan)
            params = sig.parameters
            has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

            for key, value in data_kwargs.items():
                if key in params or has_varkw:
                    kwargs[key] = value

            if "regime" in params and regime is not None:
                kwargs["regime"] = regime
            if "h1_regime_is_trending_up" in params and regime is not None:
                kwargs["h1_regime_is_trending_up"] = regime in (
                    MarketRegime.TRENDING_UP, MarketRegime.VOLATILE_TREND)
                kwargs["h1_regime_is_trending_down"] = regime == MarketRegime.TRENDING_DOWN
                kwargs["h1_regime_is_choppy"] = regime == MarketRegime.CHOPPY
            if "regime_is_choppy" in params:
                kwargs["regime_is_choppy"] = regime == MarketRegime.CHOPPY if regime else False
            if "regime_is_ranging" in params:
                kwargs["regime_is_ranging"] = regime == MarketRegime.RANGING if regime else False

            if not has_varkw:
                kwargs = {k: v for k, v in kwargs.items() if k in params}
            return loop.run_until_complete(strategy.scan(**kwargs))
        except Exception as e:
            logger.debug("Strategy %s error: %s", strategy.__class__.__name__, e)
            return None

    def _calc_volume(self, account: BacktestAccountManager, entry: float, sl: float) -> float:
        equity = account._get_equity()
        profit = max(0, equity - self._initial_capital)
        effective_eq = self._initial_capital + (profit * self._profit_growth_factor)
        risk_dollars = effective_eq * self._risk_pct / 100.0
        sl_dist = abs(entry - sl)
        if sl_dist <= 0:
            return 0.01
        sl_pts = sl_dist / self._point_size if self._point_size > 0 else sl_dist
        cost_per_lot = sl_pts * self._tick_value
        vol = risk_dollars / cost_per_lot if cost_per_lot > 0 else 0.01
        return round(max(0.01, min(vol, self._max_lot)), 2)

    def _update_trailing(self, account: BacktestAccountManager, price: float) -> None:
        for pos in account.get_positions(self._symbol):
            profit_sl = self._trailing_manager.update_profit_trail(
                ticket=pos.ticket, side=pos.side,
                current_price=price, open_price=pos.open_price,
            )
            if profit_sl is not None:
                if pos.side == OrderSide.BUY:
                    pos.stop_loss = max(pos.stop_loss or 0, round(profit_sl, 5))
                else:
                    pos.stop_loss = min(pos.stop_loss or 999999, round(profit_sl, 5))

    def _check_daily_limits(self, bar_time: datetime, account: BacktestAccountManager) -> bool:
        today = bar_time.strftime("%Y-%m-%d")
        if today != self._current_date:
            self._current_date = today
            self._daily_trade_count = 0
            self._daily_start_equity = account._get_equity()
        if self._daily_trade_count >= self._max_daily_trades:
            return False
        current_eq = account._get_equity()
        if self._daily_start_equity > 0:
            daily_loss = ((self._daily_start_equity - current_eq) / self._daily_start_equity) * 100
            if daily_loss >= self._daily_loss_limit:
                return False
        return True

    def _get_regime(self, h1_data: pd.DataFrame | None,
                    current_time: datetime, current_price: float) -> MarketRegime:
        if h1_data is None or h1_data.empty:
            return MarketRegime.CHOPPY
        h1_visible = h1_data[h1_data["time"] <= current_time]
        if len(h1_visible) < 20:
            return MarketRegime.CHOPPY
        return detect_regime_from_ohlcv(h1_visible, self._regime_detector, current_price)

    @staticmethod
    def _resample(df: pd.DataFrame, freq: str) -> pd.DataFrame:
        tmp = df.set_index("time")
        r = tmp.resample(freq).agg({
            "open": "first", "high": "max", "low": "min", "close": "last",
            "tick_volume": "sum", "real_volume": "sum", "spread": "mean",
        }).dropna(subset=["open"]).reset_index()
        return r

    def _empty_result(self) -> BacktestResult:
        return BacktestResult(
            strategy_name="unified", symbol=self._symbol,
            start_date=datetime.now(timezone.utc), end_date=datetime.now(timezone.utc),
            initial_capital=self._initial_capital, final_equity=self._initial_capital,
        )
