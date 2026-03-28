"""Technical Signal Generator — runs strategies on a timer.

Orchestrates multiple trading strategies, each scanning the market
independently. Signals are published to the EventBus and flow through
the same pipeline as Telegram signals (confidence gate → risk manager →
executor).

Architecture:
  SignalGenerator.start()
    └── _scan_loop() [every N seconds]
         ├── For each instrument:
         │   ├── Fetch H1 bars → detect regime
         │   ├── Check session (London/NY only)
         │   ├── Fetch M15 bars
         │   ├── Run EMA Pullback strategy
         │   └── Run London Breakout strategy
         └── Publish SignalEvents for any triggers
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from src.analysis.news_filter import NewsEventFilter
from src.analysis.regime import (
    MarketRegime,
    RegimeDetector,
    detect_regime_from_ohlcv,
)
from src.analysis.sessions import SessionManager, SessionName, SessionPriority
from src.analysis.smc_confluence import adjust_confidence as smc_adjust
from src.analysis.strategies.ema_pullback import EmaPullbackStrategy
from src.analysis.strategies.london_breakout import LondonBreakoutStrategy
from src.analysis.strategies.ny_momentum import NyMomentumStrategy, NyRangeBreakoutStrategy

# Scalping strategies
from src.analysis.strategies.m5_dual_supertrend import M5DualSupertrendStrategy
from src.analysis.strategies.m5_keltner_squeeze import M5KeltnerSqueezeStrategy
from src.analysis.strategies.m5_vwap_mean_reversion import M5VwapMeanReversionStrategy
from src.analysis.strategies.m5_stochrsi_adx import M5StochRsiAdxStrategy
from src.analysis.strategies.m5_mtf_momentum import M5MtfMomentumStrategy
from src.analysis.strategies.m5_bb_squeeze import M5BbSqueezeStrategy
from src.analysis.strategies.m5_mean_reversion import M5MeanReversionStrategy
from src.analysis.strategies.m1_heikin_ashi_momentum import M1HeikinAshiMomentumStrategy
from src.analysis.strategies.m1_rsi_scalp import M1RsiScalpStrategy
from src.analysis.strategies.m1_supertrend_scalp import M1SupertrendScalpStrategy
from src.analysis.strategies.m1_ema_micro import M1EmaMicroStrategy

from src.config.schema import AppConfig
from src.core.enums import SignalAction
from src.core.events import EventBus, SignalEvent
from src.core.models import Signal
from src.mt5.client import AsyncMT5Client

logger = logging.getLogger(__name__)

SCALPING_REGISTRY: dict[str, type] = {
    "m5_dual_supertrend": M5DualSupertrendStrategy,
    "m5_keltner_squeeze": M5KeltnerSqueezeStrategy,
    "m5_vwap_mean_reversion": M5VwapMeanReversionStrategy,
    "m5_stochrsi_adx": M5StochRsiAdxStrategy,
    "m5_mtf_momentum": M5MtfMomentumStrategy,
    "m5_bb_squeeze": M5BbSqueezeStrategy,
    "m5_mean_reversion": M5MeanReversionStrategy,
    "m1_heikin_ashi_momentum": M1HeikinAshiMomentumStrategy,
    "m1_rsi_scalp": M1RsiScalpStrategy,
    "m1_supertrend_scalp": M1SupertrendScalpStrategy,
    "m1_ema_micro": M1EmaMicroStrategy,
}


class SignalGenerator:
    """Timer-based multi-strategy signal scanner."""

    def __init__(
        self,
        config: AppConfig,
        event_bus: EventBus,
        mt5_client: AsyncMT5Client,
    ) -> None:
        self._config = config
        self._event_bus = event_bus
        self._mt5 = mt5_client
        self._running = False
        self._task: asyncio.Task | None = None

        # News event filter
        self._news_filter = NewsEventFilter(calendar_path="config/news_calendar.csv")

        # Regime detection
        self._regime_detector = RegimeDetector()

        # Session management
        allowed = [
            SessionName(s) for s in config.signal_generator.allowed_sessions
        ]
        self._session_mgr = SessionManager(allowed_sessions=allowed)

        # Strategies
        self._ema_pullback = EmaPullbackStrategy(config.strategies.ema_pullback)
        self._london_breakout = LondonBreakoutStrategy(config.strategies.london_breakout)
        ny_cfg = config.strategies.ny_momentum
        self._ny_range_breakout = NyRangeBreakoutStrategy(
            breakout_buffer_pips=ny_cfg.range_breakout_buffer_pips,
            tp_multiplier=ny_cfg.range_tp_multiplier,
            max_trades_per_day=ny_cfg.range_max_trades_per_day,
        )
        self._ny_momentum = NyMomentumStrategy(
            max_trades_per_day=ny_cfg.momentum_max_trades_per_day,
        )

        # Instrument point sizes for pip calculations
        self._point_sizes: dict[str, float] = {
            inst.symbol: inst.point_size for inst in config.instruments
        }

        # Scalping strategies (instantiate from config)
        self._scalping_strategies = []
        self._scalping_enabled = False
        if hasattr(config.strategies, 'scalping') and config.strategies.scalping.enabled:
            self._scalping_enabled = True
            self._scalping_interval = config.strategies.scalping.scan_interval_seconds
            for name in config.strategies.scalping.strategies_enabled:
                cls = SCALPING_REGISTRY.get(name)
                if cls:
                    self._scalping_strategies.append(cls())
                    logger.info("Scalping strategy enabled: %s", name)

    async def start(self) -> None:
        """Start the signal generation loop."""
        self._running = True
        self._task = asyncio.create_task(self._scan_loop())

        active_strategies = []
        if self._config.strategies.ema_pullback.enabled:
            active_strategies.append("EMA Pullback")
        if self._config.strategies.london_breakout.enabled:
            active_strategies.append("London Breakout")
        if self._config.strategies.ny_momentum.enabled:
            active_strategies.append("NY Range Breakout")
            active_strategies.append("NY Momentum")

        # Start scalping scan loop (faster interval)
        self._scalping_task = None
        if self._scalping_enabled and self._scalping_strategies:
            self._scalping_task = asyncio.create_task(self._scalping_loop())
            active_strategies.extend([s.__class__.__name__ for s in self._scalping_strategies])

        logger.info(
            "SignalGenerator started (%d strategies active: %s, scan every %ds)",
            len(active_strategies),
            ", ".join(active_strategies) or "none",
            self._config.signal_generator.scan_interval_seconds,
        )

    async def stop(self) -> None:
        """Stop the signal generation loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if hasattr(self, '_scalping_task') and self._scalping_task:
            self._scalping_task.cancel()
            try:
                await self._scalping_task
            except asyncio.CancelledError:
                pass
        logger.info("SignalGenerator stopped")

    async def _scan_loop(self) -> None:
        """Main scanning loop — runs every scan_interval_seconds."""
        interval = self._config.signal_generator.scan_interval_seconds

        # Wait 60s on startup to let PositionMonitor sync MT5 positions
        # This prevents opening duplicate trades on every restart
        logger.info("SignalGenerator waiting 60s for position sync before first scan...")
        await asyncio.sleep(60)
        logger.info("SignalGenerator startup delay complete, beginning scans")

        while self._running:
            try:
                await self._run_scan()
            except Exception:
                logger.exception("Signal scan error (will retry next cycle)")

            await asyncio.sleep(interval)

    async def _run_scan(self) -> None:
        """Run one full scan across all instruments."""
        # Weekend guard — forex markets closed Fri 22:00 → Sun 22:00 UTC
        now = datetime.now(timezone.utc)
        if now.weekday() == 5 or (now.weekday() == 6 and now.hour < 22):
            # Saturday all day, or Sunday before 22:00
            logger.debug("Signal scan skipped: weekend (markets closed)")
            return
        if now.weekday() == 4 and now.hour >= 22:
            # Friday after 22:00
            logger.debug("Signal scan skipped: weekend (Friday close)")
            return

        logger.debug("Running scan cycle at %s UTC", now.strftime("%H:%M:%S"))

        # Check session first
        session = self._session_mgr.get_current_session()
        if not self._session_mgr.is_trading_allowed():
            logger.debug(
                "Signal scan skipped: session=%s (priority=%s)",
                session.name.value, session.priority.value,
            )
            return

        instruments = self._config.signal_generator.instruments

        for symbol in instruments:
            try:
                await self._scan_symbol(symbol, session)
            except Exception:
                logger.error("Scan CRASHED for %s", symbol, exc_info=True)

    async def _scan_symbol(self, symbol: str, session) -> None:
        """Run all strategies for one symbol."""
        # Check per-instrument overrides
        overrides = self._config.signal_generator.instrument_overrides.get(symbol)
        if overrides:
            logger.debug("Applying overrides for %s: %s", symbol, overrides)

        # Fetch H1 bars for regime detection (with timeout to prevent hang)
        try:
            h1_bars = await asyncio.wait_for(
                self._mt5.get_bars(symbol, "H1", count=100), timeout=30
            )
        except asyncio.TimeoutError:
            logger.warning("Timeout fetching H1 bars for %s", symbol)
            return
        except Exception:
            logger.warning("Cannot fetch H1 bars for %s", symbol, exc_info=True)
            return

        logger.debug("Fetched H1 for %s: %d bars", symbol, len(h1_bars) if h1_bars is not None else 0)

        if h1_bars is None or h1_bars.empty or len(h1_bars) < 60:
            logger.info("Insufficient H1 data for %s (%d bars)", symbol, len(h1_bars) if h1_bars is not None else 0)
            return

        # Detect market regime
        current_price = float(h1_bars["close"].iloc[-1])
        regime = detect_regime_from_ohlcv(
            h1_bars, self._regime_detector, current_price
        )

        logger.info("Regime for %s: %s", symbol, regime.value)

        # Map regime to strategy flags
        is_choppy = regime == MarketRegime.CHOPPY
        is_ranging = regime == MarketRegime.RANGING

        # Determine trend direction
        if regime == MarketRegime.VOLATILE_TREND:
            # Use H1 EMA(50) slope to determine direction
            ema50 = h1_bars["close"].ewm(span=50).mean()
            is_up = float(ema50.iloc[-1]) > float(ema50.iloc[-5])
            is_down = not is_up
        else:
            is_up = regime == MarketRegime.TRENDING_UP
            is_down = regime == MarketRegime.TRENDING_DOWN

        # EMA Pullback and London Breakout skip CHOPPY — but NY strategies don't
        skip_trend_strategies = is_choppy

        # Fetch M15 bars for entry strategies (with timeout)
        try:
            m15_bars = await asyncio.wait_for(
                self._mt5.get_bars(symbol, "M15", count=100), timeout=30
            )
        except asyncio.TimeoutError:
            logger.warning("Timeout fetching M15 bars for %s", symbol)
            return
        except Exception:
            logger.warning("Cannot fetch M15 bars for %s", symbol, exc_info=True)
            return

        if m15_bars is None or m15_bars.empty:
            return

        point_size = self._point_sizes.get(symbol, 0.01)

        # Run EMA Pullback strategy (skip in CHOPPY — needs trend)
        if self._config.strategies.ema_pullback.enabled and not skip_trend_strategies:
            sig = await self._ema_pullback.scan(
                symbol=symbol,
                m15_bars=m15_bars,
                h1_regime_is_trending_up=is_up,
                h1_regime_is_trending_down=is_down,
                h1_regime_is_choppy=is_choppy,
            )
            if sig:
                sig.confidence = smc_adjust(
                    sig.action, symbol, m15_bars, sig.confidence,
                    self._config.strategies.smc_confluence,
                )
                await self._publish_signal(sig, "ema_pullback")

        # Run London Breakout strategy (skip in CHOPPY — needs directional breakout)
        if self._config.strategies.london_breakout.enabled and not skip_trend_strategies:
            sig = await self._london_breakout.scan(
                symbol=symbol,
                m15_bars=m15_bars,
                point_size=point_size,
                regime_is_choppy=is_choppy,
                regime_is_ranging=is_ranging,
            )
            if sig:
                sig.confidence = smc_adjust(
                    sig.action, symbol, m15_bars, sig.confidence,
                    self._config.strategies.smc_confluence,
                )
                await self._publish_signal(sig, "london_breakout")

        # Run NY Range Breakout (works in ALL regimes except RANGING)
        if self._config.strategies.ny_momentum.enabled:
            sig = await self._ny_range_breakout.scan(
                symbol=symbol,
                m15_bars=m15_bars,
                point_size=point_size,
                regime_is_ranging=is_ranging,
            )
            if sig:
                sig.confidence = smc_adjust(
                    sig.action, symbol, m15_bars, sig.confidence,
                    self._config.strategies.smc_confluence,
                )
                await self._publish_signal(sig, "ny_range_breakout")

        # Run NY Momentum Continuation (works in ALL regimes except RANGING)
        if self._config.strategies.ny_momentum.enabled:
            sig = await self._ny_momentum.scan(
                symbol=symbol,
                m15_bars=m15_bars,
                regime_is_ranging=is_ranging,
            )
            if sig:
                sig.confidence = smc_adjust(
                    sig.action, symbol, m15_bars, sig.confidence,
                    self._config.strategies.smc_confluence,
                )
                await self._publish_signal(sig, "ny_momentum")

    async def _scan_scalping(self, symbol: str, h1_bars, session) -> None:
        """Run M5/M1 scalping strategies for a symbol."""
        blocked, reason = self._news_filter.is_blocked(datetime.now(timezone.utc))
        if blocked:
            logger.info("Trading blocked: %s", reason)
            return

        try:
            m5_bars = await asyncio.wait_for(
                self._mt5.get_bars(symbol, "M5", count=200), timeout=30
            )
        except (asyncio.TimeoutError, Exception):
            return

        if m5_bars is None or m5_bars.empty or len(m5_bars) < 50:
            return

        # Resample M5 to M15 for MTF context
        m15_bars = m5_bars.set_index("time").resample("15min").agg({
            "open": "first", "high": "max", "low": "min", "close": "last",
            "tick_volume": "sum",
        }).dropna(subset=["open"]).reset_index()

        current_price = float(m5_bars["close"].iloc[-1])
        regime = detect_regime_from_ohlcv(h1_bars, self._regime_detector, current_price)
        point_size = self._point_sizes.get(symbol, 0.01)

        for strategy in self._scalping_strategies:
            try:
                kwargs = {"symbol": symbol, "point_size": point_size, "as_of": datetime.now(timezone.utc)}

                # Determine if M1 or M5 strategy
                cls_name = strategy.__class__.__name__.lower()
                if "m1" in cls_name:
                    # Fetch M1 bars for M1 strategies
                    try:
                        m1_bars = await asyncio.wait_for(
                            self._mt5.get_bars(symbol, "M1", count=200), timeout=30
                        )
                        if m1_bars is None or len(m1_bars) < 30:
                            continue
                        kwargs["m1_bars"] = m1_bars
                        kwargs["m5_bars"] = m5_bars
                    except Exception:
                        continue
                else:
                    kwargs["m5_bars"] = m5_bars

                kwargs["m15_bars"] = m15_bars
                kwargs["h1_bars"] = h1_bars
                kwargs["regime"] = regime

                signal = await strategy.scan(**kwargs)
                if signal:
                    await self._publish_scalping_signal(signal, symbol, session)
            except Exception:
                logger.debug("Scalping strategy %s error for %s", strategy.__class__.__name__, symbol, exc_info=True)

    async def _publish_scalping_signal(self, signal, symbol: str, session) -> None:
        """Publish a scalping strategy signal to the EventBus."""
        from src.core.events import SignalEvent

        action = SignalAction.BUY if signal.action == "BUY" else SignalAction.SELL

        sig = Signal(
            source=f"scalping:{signal.strategy_name}",
            symbol=symbol,
            action=action,
            strength=signal.confidence,
            timestamp=datetime.now(timezone.utc),
            entry_price=signal.entry_price,
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            metadata={"strategy": signal.strategy_name, "reason": signal.reason},
        )

        logger.info(
            "Scalping signal: %s %s %s @ %.2f (SL=%.2f TP=%.2f conf=%.2f strategy=%s)",
            symbol, signal.action, signal.strategy_name, signal.entry_price,
            signal.stop_loss, signal.take_profit, signal.confidence, signal.strategy_name,
        )

        await self._event_bus.publish(SignalEvent(timestamp=sig.timestamp, signal=sig))

    async def _scalping_loop(self) -> None:
        """Fast scan loop for scalping strategies (every 15s)."""
        interval = getattr(self, '_scalping_interval', 15)

        logger.info("Scalping scan loop waiting 90s for startup...")
        await asyncio.sleep(90)
        logger.info("Scalping scan loop starting (interval=%ds, %d strategies)",
                    interval, len(self._scalping_strategies))

        while self._running:
            try:
                now = datetime.now(timezone.utc)
                # Weekend guard
                if now.weekday() >= 5:
                    await asyncio.sleep(interval)
                    continue

                # Friday auto-close — no new scalping trades after 20:00 UTC
                if now.weekday() == 4 and now.hour >= 20:
                    logger.info("Friday close window — no new scalping trades")
                    await asyncio.sleep(interval)
                    continue

                session = self._session_mgr.get_current_session()
                if not self._session_mgr.is_trading_allowed():
                    await asyncio.sleep(interval)
                    continue

                for symbol in self._config.signal_generator.instruments:
                    try:
                        h1_bars = await asyncio.wait_for(
                            self._mt5.get_bars(symbol, "H1", count=100), timeout=30
                        )
                        if h1_bars is not None and len(h1_bars) >= 20:
                            await self._scan_scalping(symbol, h1_bars, session)
                    except Exception:
                        logger.debug("Scalping scan failed for %s", symbol, exc_info=True)
            except Exception:
                logger.exception("Scalping scan loop error")

            await asyncio.sleep(interval)

    async def _publish_signal(self, sig, strategy_name: str) -> None:
        """Convert a strategy signal to a SignalEvent and publish."""
        action = SignalAction.BUY if sig.action == "BUY" else SignalAction.SELL

        signal = Signal(
            source=f"technical:{strategy_name}",
            symbol=sig.symbol,
            action=action,
            strength=sig.confidence,
            timestamp=datetime.now(timezone.utc),
            entry_price=sig.entry_price,
            stop_loss=sig.stop_loss,
            take_profit=sig.take_profit,
            metadata={"strategy": strategy_name, "reason": sig.reason},
        )

        logger.info(
            "Technical signal: %s %s @ %.5f SL=%.5f TP=%.5f "
            "(strategy=%s, confidence=%.2f)",
            sig.action, sig.symbol, sig.entry_price,
            sig.stop_loss, sig.take_profit,
            strategy_name, sig.confidence,
        )

        await self._event_bus.publish(
            SignalEvent(timestamp=datetime.now(timezone.utc), signal=signal)
        )
