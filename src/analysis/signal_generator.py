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
from src.config.schema import AppConfig
from src.core.enums import SignalAction
from src.core.events import EventBus, SignalEvent
from src.core.models import Signal
from src.mt5.client import AsyncMT5Client

logger = logging.getLogger(__name__)


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
