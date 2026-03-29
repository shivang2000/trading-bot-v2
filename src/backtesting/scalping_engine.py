"""Scalping backtest engine -- native M1/M5 bar-by-bar replay.

Unlike the M15 BacktestEngine, this engine iterates over actual M1 or M5
bars and provides multi-timeframe views (M5, M15, H1) pre-computed at init.
Includes realistic cost modeling via CostModel.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import datetime, timezone

import pandas as pd

from src.analysis.news_filter import NewsEventFilter
from src.analysis.regime import MarketRegime, RegimeDetector, detect_regime_from_ohlcv
from src.backtesting.account import BacktestAccountManager
from src.backtesting.cost_model import CostModel
from src.backtesting.result import BacktestResult, calculate_metrics
from src.config.schema import AppConfig
from src.core.enums import OrderSide
from src.risk.prop_firm_guard import PropFirmGuard, PropFirmConfig
from src.risk.position_sizer import calculate_lot_size_prop_firm
from src.analysis.smc_confluence import adjust_confidence as smc_adjust
from src.config.schema import SmcConfluenceConfig

logger = logging.getLogger(__name__)

WARMUP_BARS = 200  # Need enough for EMA(200) on resampled data

DEFAULT_LOT_TIERS = [
    (0, 0.50),
    (500, 1.00),
    (2000, 2.00),
    (5000, 5.00),
    (10000, 10.00),
]


class _StrategyEquityTracker:
    """Pauses strategies whose equity drops below 20-trade MA."""

    def __init__(self, ma_period: int = 20) -> None:
        self._pnls: dict[str, list[float]] = {}
        self._ma = ma_period

    def add_trade(self, name: str, pnl: float) -> None:
        self._pnls.setdefault(name, []).append(pnl)

    def is_paused(self, name: str) -> bool:
        pnls = self._pnls.get(name, [])
        if len(pnls) < self._ma:
            return False
        cumulative: list[float] = []
        s = 0.0
        for p in pnls:
            s += p
            cumulative.append(s)
        recent = cumulative[-self._ma:]
        ma_val = sum(recent) / len(recent)
        return cumulative[-1] < ma_val


class ScalpingBacktestEngine:
    """Native M1/M5 bar-by-bar replay engine with MTF data pipeline."""

    def __init__(
        self,
        symbol: str,
        primary_timeframe: str = "M5",
        strategies: list | None = None,
        initial_capital: float = 10_000.0,
        config: AppConfig | None = None,
        cost_model: CostModel | None = None,
        point_size: float = 0.01,
        tick_value: float = 0.01,
        max_daily_trades: int = 50,
        daily_loss_limit_pct: float = 5.0,
        risk_pct: float = 1.0,
        max_lot: float = 100.0,
        max_per_strategy: int = 1,
        max_total_positions: int = 10,
        profit_growth_factor: float = 0.50,
        use_tiered_caps: bool = False,
        lot_tiers: list[tuple[float, float]] | None = None,
        prop_firm_config: PropFirmConfig | None = None,
        smc_config: SmcConfluenceConfig | None = None,
        min_confidence: float = 0.45,
    ) -> None:
        self._symbol = symbol
        self._primary_tf = primary_timeframe
        self._strategies = strategies or []
        self._initial_capital = initial_capital
        self._cost_model = cost_model
        self._point_size = point_size
        self._tick_value = tick_value
        self._max_daily_trades = max_daily_trades
        self._daily_loss_limit = daily_loss_limit_pct
        self._max_per_strategy = max_per_strategy
        self._max_total_positions = max_total_positions
        self._profit_growth_factor = profit_growth_factor
        self._use_tiered_caps = use_tiered_caps
        self._lot_tiers = lot_tiers or DEFAULT_LOT_TIERS
        self._regime_detector = RegimeDetector()
        self._news_filter = NewsEventFilter()

        if config is not None:
            self._risk_pct = config.account.risk_per_trade_pct
            self._max_lot = config.account.max_lot_per_trade
            for inst in config.instruments:
                if inst.symbol == symbol:
                    self._tick_value = inst.tick_value
                    self._point_size = inst.point_size
                    break
        else:
            self._risk_pct = risk_pct
            self._max_lot = max_lot

        # DD recovery mode
        self._peak_equity = initial_capital
        self._in_dd_recovery = False
        self._dd_trigger = 30.0      # enter recovery at 30% DD
        self._dd_recovery = 15.0     # exit recovery at 15% DD
        self._dd_position_reduction = 0.50
        self._dd_min_confidence = 0.70

        # Equity curve trading
        self._equity_tracker = _StrategyEquityTracker()

        # SMC confluence filter
        self._smc_config = smc_config or SmcConfluenceConfig()
        self._min_confidence = min_confidence

        # Prop firm guard
        self._prop_firm: PropFirmGuard | None = None
        if prop_firm_config:
            self._prop_firm = PropFirmGuard(prop_firm_config)

        self._current_date = ""
        self._daily_trade_count = 0
        self._daily_start_equity = 0.0

    def run(
        self, primary_data: pd.DataFrame, h1_data: pd.DataFrame | None = None,
    ) -> BacktestResult:
        """Run backtest. *primary_data* is M1 or M5 bars."""
        m5_data, m15_data, h1_computed = self._precompute_mtf(primary_data, h1_data)

        account = BacktestAccountManager(
            self._initial_capital, tick_value=self._tick_value,
            point_size=self._point_size,
        )
        self._daily_start_equity = self._initial_capital

        total_bars = len(primary_data)
        if total_bars < WARMUP_BARS + 10:
            logger.warning("Not enough data (%d bars, need %d+)", total_bars, WARMUP_BARS)
            return self._empty_result()

        logger.info(
            "Scalping backtest: %s on %s (%d %s bars, %d strategies)",
            self._symbol, self._primary_tf, total_bars,
            self._primary_tf, len(self._strategies),
        )

        signals_generated = 0
        loop = asyncio.new_event_loop()

        try:
            for i in range(WARMUP_BARS, total_bars):
                bar = primary_data.iloc[i]
                bar_time = pd.Timestamp(bar["time"]).to_pydatetime()
                if bar_time.tzinfo is None:
                    bar_time = bar_time.replace(tzinfo=timezone.utc)

                bar_high = float(bar["high"])
                bar_low = float(bar["low"])
                bar_close = float(bar["close"])

                # 0. Check news filter
                blocked, reason = self._news_filter.is_blocked(bar_time)
                if blocked:
                    account.update_prices(self._symbol, bar_close, bar_time)
                    continue

                # 0b. Prop firm checks
                if self._prop_firm:
                    can_trade, reason = self._prop_firm.can_trade(account._get_equity(), bar_time)
                    if not can_trade:
                        # Check if profit target reached — terminate backtest successfully
                        reached, profit = self._prop_firm.check_profit_target(account._get_equity())
                        if reached:
                            logger.info("PROFIT TARGET REACHED! Profit: $%.2f — stopping backtest", profit)
                            break
                        # Otherwise just skip this bar (daily limit or DD warning)
                        account.update_prices(self._symbol, bar_close, bar_time)
                        continue

                    # Friday auto-close
                    if self._prop_firm.should_friday_close(bar_time):
                        for pos in list(account.get_positions(self._symbol)):
                            account.close_position(pos.ticket, bar_close, bar_time, "FRIDAY_CLOSE")
                        account.update_prices(self._symbol, bar_close, bar_time)
                        continue

                    # Don't open new trades near Friday close
                    if self._prop_firm.should_stop_new_trades_friday(bar_time):
                        account.update_prices(self._symbol, bar_close, bar_time)
                        continue

                # 1. Check SL/TP on open positions FIRST
                # Capture position comments before SL/TP check for equity tracker
                pre_comments = {p.ticket: p.comment for p in account.get_positions(self._symbol)}
                closed_trades = account.check_sl_tp(self._symbol, bar_high, bar_low, bar_time)

                # Feed closed trades to equity curve tracker
                for ct in closed_trades:
                    orig_comment = pre_comments.get(ct.ticket, "")
                    ct_sname = orig_comment.split(":")[0] if orig_comment else ""
                    self._equity_tracker.add_trade(ct_sname, ct.profit)

                # 2. Check daily limits (reset on new day)
                if not self._check_daily_limits(bar_time, account):
                    account.update_prices(self._symbol, bar_close, bar_time)
                    continue

                # 3. Get regime from H1 (no lookahead)
                regime = self._get_regime(h1_computed, bar_time, bar_close)

                # 4. Run each strategy independently — each can hold its own position
                primary_window = primary_data.iloc[max(0, i - 199):i + 1].copy()
                m5_win = self._safe_window(m5_data, bar_time, 200)
                m15_win = self._safe_window(m15_data, bar_time, 100)
                h1_win = self._safe_window(h1_computed, bar_time, 60)

                for strategy in self._strategies:
                    sname = strategy.__class__.__name__

                    # Equity curve trading: skip strategies below their MA
                    if self._equity_tracker.is_paused(sname):
                        continue

                    try:
                        signal = self._invoke_strategy(
                            loop, strategy, primary_window,
                            m5_win, m15_win, h1_win, bar_time, regime,
                        )
                    except Exception as exc:
                        logger.warning("Strategy %s error: %s", sname, exc)
                        continue

                    if signal is not None:
                        # SMC confluence adjustment
                        try:
                            data_for_smc = primary_window if primary_window is not None and len(primary_window) >= 20 else m5_win
                            if data_for_smc is not None and len(data_for_smc) >= 20:
                                signal.confidence = smc_adjust(
                                    action=signal.action,
                                    symbol=self._symbol,
                                    m15_bars=data_for_smc,
                                    base_confidence=signal.confidence,
                                    config=self._smc_config,
                                )
                        except Exception:
                            pass  # SMC failure should not block trades

                        # Min confidence filter
                        if signal.confidence < self._min_confidence:
                            continue

                        # Prop firm: directional exposure check
                        if self._prop_firm:
                            positions = account.get_positions(self._symbol)
                            if not self._prop_firm.check_directional_exposure(positions, signal.action):
                                continue  # too many positions in same direction

                        # DD recovery: skip low-confidence signals
                        if self._in_dd_recovery and signal.confidence < self._dd_min_confidence:
                            continue

                        # Check position limit using signal.strategy_name (matches comment prefix)
                        sname = signal.strategy_name or sname
                        if not self._can_open_position(account, sname):
                            continue
                        side = OrderSide.BUY if signal.action == "BUY" else OrderSide.SELL
                        entry = signal.entry_price
                        if self._cost_model is not None:
                            # Use actual bar spread if available and configured
                            bar_spread = float(bar.get("spread", 0)) if hasattr(bar, 'get') else 0
                            if bar_spread > 0 and self._cost_model._spread_from_data:
                                if self._cost_model.should_skip_trade(bar_spread):
                                    continue  # skip trade — spread too wide
                                spread = bar_spread * self._point_size  # convert points to price
                            else:
                                spread = self._cost_model.get_spread(bar_time)
                            entry = self._cost_model.adjust_entry_for_spread(
                                entry, signal.action, spread, self._point_size,
                            )
                        volume = self._calculate_volume(account, entry, signal.stop_loss)
                        comment = (
                            f"{signal.strategy_name}:{signal.reason}"
                            if signal.strategy_name else signal.reason
                        )
                        account.open_position(
                            symbol=self._symbol, side=side, volume=volume,
                            entry_price=entry, stop_loss=signal.stop_loss,
                            take_profit=signal.take_profit, open_time=bar_time,
                            comment=comment,
                        )
                        signals_generated += 1
                        self._daily_trade_count += 1

                # 5. Update equity snapshot
                account.update_prices(self._symbol, bar_close, bar_time)

                if i % 10_000 == 0:
                    logger.info("  Bar %d/%d (%.0f%%)", i, total_bars, i / total_bars * 100)
        finally:
            loop.close()

        # Close remaining open positions at last bar
        last_bar = primary_data.iloc[-1]
        last_close = float(last_bar["close"])
        last_time = pd.Timestamp(last_bar["time"]).to_pydatetime()
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=timezone.utc)
        for pos in list(account.get_positions(self._symbol)):
            account.close_position(pos.ticket, last_close, last_time, "END_OF_BACKTEST")

        # Calculate metrics and build result
        trades = account.get_trades()
        equity_curve = account.get_equity_curve()
        final_equity = equity_curve.iloc[-1] if not equity_curve.empty else self._initial_capital
        metrics = calculate_metrics(trades, equity_curve, self._initial_capital)

        first_time = pd.Timestamp(primary_data["time"].iloc[WARMUP_BARS]).to_pydatetime()
        last_time_dt = pd.Timestamp(primary_data["time"].iloc[-1]).to_pydatetime()

        result = BacktestResult(
            strategy_name=",".join(s.__class__.__name__ for s in self._strategies),
            symbol=self._symbol, start_date=first_time, end_date=last_time_dt,
            initial_capital=self._initial_capital, final_equity=final_equity,
            trades=trades, equity_curve=equity_curve, **metrics,
        )
        logger.info(
            "Scalping backtest done: %d trades, %.2f%% return, %.2f%% DD, %.1f%% WR",
            result.total_trades, result.total_return_pct,
            result.max_drawdown_pct, result.win_rate,
        )
        return result

    # -- Multi-timeframe pre-computation ------------------------------------

    def _precompute_mtf(
        self, primary_data: pd.DataFrame, h1_data: pd.DataFrame | None,
    ) -> tuple[pd.DataFrame | None, pd.DataFrame | None, pd.DataFrame | None]:
        """Pre-compute all resampled timeframes ONCE at engine init."""
        m5_data: pd.DataFrame | None = None
        m15_data: pd.DataFrame | None = None

        if self._primary_tf == "M1":
            m5_data = self._resample(primary_data, "5min")
            m15_data = self._resample(primary_data, "15min")
            if h1_data is None or h1_data.empty:
                h1_data = self._resample(primary_data, "1h")
        elif self._primary_tf == "M5":
            m5_data = primary_data  # M5 IS the primary
            m15_data = self._resample(primary_data, "15min")
            if h1_data is None or h1_data.empty:
                h1_data = self._resample(primary_data, "1h")

        logger.info(
            "MTF pre-computed: M5=%s, M15=%s, H1=%s bars",
            len(m5_data) if m5_data is not None else 0,
            len(m15_data) if m15_data is not None else 0,
            len(h1_data) if h1_data is not None else 0,
        )
        return m5_data, m15_data, h1_data

    @staticmethod
    def _resample(df: pd.DataFrame, freq: str) -> pd.DataFrame:
        """Resample OHLCV data to a higher timeframe."""
        tmp = df.set_index("time")
        resampled = tmp.resample(freq).agg({
            "open": "first", "high": "max", "low": "min", "close": "last",
            "tick_volume": "sum", "real_volume": "sum", "spread": "mean",
        }).dropna(subset=["open"])
        return resampled.reset_index()

    @staticmethod
    def _safe_window(
        df: pd.DataFrame | None, bar_time: datetime, n: int,
    ) -> pd.DataFrame | None:
        """Return the last *n* bars with time <= bar_time, or None."""
        if df is None:
            return None
        return df[df["time"] <= bar_time].tail(n).copy()

    def _can_open_position(self, account: BacktestAccountManager, strategy_name: str) -> bool:
        """Check if this strategy can open a new position (per-strategy + total limits)."""
        positions = account.get_positions(self._symbol)
        if len(positions) >= self._max_total_positions:
            return False
        strategy_positions = [p for p in positions if p.comment.startswith(strategy_name + ":")]
        if len(strategy_positions) >= self._max_per_strategy:
            return False
        return True

    # -- Daily risk limits --------------------------------------------------

    def _check_daily_limits(
        self, bar_time: datetime, account: BacktestAccountManager,
    ) -> bool:
        """Check daily trade count and loss limits. Reset on new day."""
        today = bar_time.strftime("%Y-%m-%d")
        if today != self._current_date:
            self._current_date = today
            self._daily_trade_count = 0
            self._daily_start_equity = account._get_equity()

        if self._daily_trade_count >= self._max_daily_trades:
            return False

        current_equity = account._get_equity()
        if self._daily_start_equity > 0:
            daily_loss_pct = (
                (self._daily_start_equity - current_equity)
                / self._daily_start_equity
            ) * 100
            if daily_loss_pct >= self._daily_loss_limit:
                return False
        return True

    # -- Regime detection ---------------------------------------------------

    def _get_regime(
        self, h1_data: pd.DataFrame | None,
        current_time: datetime, current_price: float,
    ) -> MarketRegime:
        """Get H1 regime using only bars <= current_time (no lookahead)."""
        if h1_data is None or h1_data.empty:
            return MarketRegime.CHOPPY
        h1_visible = h1_data[h1_data["time"] <= current_time]
        if len(h1_visible) < 60:
            return MarketRegime.CHOPPY
        return detect_regime_from_ohlcv(h1_visible, self._regime_detector, current_price)

    # -- Strategy execution -------------------------------------------------

    def _run_strategies(
        self, loop: asyncio.AbstractEventLoop,
        primary_window: pd.DataFrame,
        m5_window: pd.DataFrame | None,
        m15_window: pd.DataFrame | None,
        h1_window: pd.DataFrame | None,
        bar_time: datetime, regime: MarketRegime,
    ):
        """Run all strategies, return first signal or None."""
        for strategy in self._strategies:
            try:
                signal = self._invoke_strategy(
                    loop, strategy, primary_window,
                    m5_window, m15_window, h1_window, bar_time, regime,
                )
                if signal is not None:
                    return signal
            except Exception as exc:
                logger.warning("Strategy %s error: %s", strategy.__class__.__name__, exc)
        return None

    def _invoke_strategy(
        self, loop: asyncio.AbstractEventLoop, strategy,
        primary_window: pd.DataFrame,
        m5_window: pd.DataFrame | None,
        m15_window: pd.DataFrame | None,
        h1_window: pd.DataFrame | None,
        bar_time: datetime, regime: MarketRegime,
    ):
        """Build kwargs dynamically from strategy.scan() signature and call."""
        kwargs: dict = {
            "symbol": self._symbol, "point_size": self._point_size, "as_of": bar_time,
        }

        # Primary timeframe data under the correct key
        if self._primary_tf == "M1":
            kwargs["m1_bars"] = primary_window
        else:
            kwargs["m5_bars"] = primary_window

        # Introspect scan() to pass only accepted kwargs
        sig = inspect.signature(strategy.scan)
        params = sig.parameters

        if "m5_bars" in params and m5_window is not None and self._primary_tf == "M1":
            kwargs["m5_bars"] = m5_window
        if "m15_bars" in params and m15_window is not None:
            kwargs["m15_bars"] = m15_window
        if "h1_bars" in params and h1_window is not None:
            kwargs["h1_bars"] = h1_window
        if "regime" in params:
            kwargs["regime"] = regime

        # Strip kwargs that scan() does not accept (unless it has **kwargs)
        has_var_keyword = any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        if not has_var_keyword:
            kwargs = {k: v for k, v in kwargs.items() if k in params}

        return loop.run_until_complete(strategy.scan(**kwargs))

    # -- Position sizing ----------------------------------------------------

    def _calculate_volume(
        self, account: BacktestAccountManager, entry: float, sl: float,
    ) -> float:
        """Calculate lot size: volume = risk$ / ((sl_dist / point_size) * tick_value)."""
        equity = account._get_equity()
        # Dampen profit growth — only use X% of profits for risk sizing
        # This reduces drawdown by preventing exponential position growth
        profit = max(0, equity - self._initial_capital)
        effective_equity = self._initial_capital + (profit * self._profit_growth_factor)

        # Prop firm position sizing override
        if self._prop_firm:
            risk_mult = self._prop_firm.get_risk_multiplier(equity)
            if risk_mult <= 0:
                return 0.0  # stop trading
            volume = calculate_lot_size_prop_firm(
                equity=effective_equity,
                risk_pct=self._risk_pct,
                entry_price=entry,
                stop_loss=sl,
                leverage=self._prop_firm._config.leverage_metals,
                point_size=self._point_size,
                tick_value=self._tick_value,
                commission_per_lot=self._prop_firm._config.commission_per_lot,
                risk_multiplier=risk_mult,
            )
            return volume

        risk_dollars = effective_equity * self._risk_pct / 100.0

        # Track peak equity for DD recovery
        current_equity = account._get_equity()
        if current_equity > self._peak_equity:
            self._peak_equity = current_equity

        # DD recovery mode
        dd_pct = ((self._peak_equity - current_equity) / self._peak_equity) * 100 if self._peak_equity > 0 else 0
        if dd_pct >= self._dd_trigger:
            self._in_dd_recovery = True
        elif dd_pct <= self._dd_recovery:
            self._in_dd_recovery = False

        if self._in_dd_recovery:
            risk_dollars *= self._dd_position_reduction

        sl_distance = abs(entry - sl)
        if sl_distance <= 0:
            return 0.01
        sl_points = sl_distance / self._point_size if self._point_size > 0 else sl_distance
        cost_per_lot = sl_points * self._tick_value
        volume = risk_dollars / cost_per_lot if cost_per_lot > 0 else 0.01
        effective_max_lot = self._max_lot
        if self._use_tiered_caps:
            equity = account._get_equity()
            for threshold, cap in reversed(self._lot_tiers):
                if equity >= threshold:
                    effective_max_lot = cap
                    break
        volume = max(0.01, min(volume, effective_max_lot))
        return round(volume, 2)

    def _empty_result(self) -> BacktestResult:
        """Return an empty result when insufficient data."""
        return BacktestResult(
            strategy_name=",".join(s.__class__.__name__ for s in self._strategies),
            symbol=self._symbol,
            start_date=datetime.now(timezone.utc),
            end_date=datetime.now(timezone.utc),
            initial_capital=self._initial_capital,
            final_equity=self._initial_capital,
        )
