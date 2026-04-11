"""Trading Bot V2 — Telegram Signal Execution Bot.

Entry point that wires all components together and runs the main loop.

Pipeline:
  Telegram Channel → Listener → Parser (Claude AI) → EventBus
  → RiskManager → OrderExecutor → MT5 → Notifications
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from src.config.loader import load_config
from src.config.schema import AppConfig
from src.core.events import EventBus, Event, FillEvent, PositionClosedEvent
from src.execution.executor import OrderExecutor
from src.logging_.daily_summary import DailySummary
from src.logging_.journal import TradeJournal
from src.monitoring.notifier import TelegramNotifier
from src.monitoring.position_monitor import PositionMonitor
from src.monitoring.slack import SlackNotifier
from src.mt5.client import AsyncMT5Client
from src.risk.manager import RiskManager
from src.safety.emergency import EmergencyStop
from src.telegram.channel_config import ChannelRegistry
from src.telegram.listener import TelegramListener
from src.telegram.parser import SignalParser
from src.analysis.signal_generator import SignalGenerator
from src.tracking.database import TrackingDB

logger = logging.getLogger(__name__)


def _setup_logging(config: AppConfig) -> None:
    """Configure logging based on config."""
    log_level = getattr(logging, config.monitoring.log_level.upper(), logging.INFO)
    log_file = config.monitoring.log_file

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)-8s] %(name)-30s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file),
        ],
    )
    # Quiet noisy libraries
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("rpyc").setLevel(logging.WARNING)


class TradingBot:
    """Main application — composes and manages all components."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._shutdown_event = asyncio.Event()
        self._cached_positions: list = []
        self._cached_account_state = None
        self._live_symbol_info: dict[str, dict] = {}  # symbol → MT5 symbol_info

        # Core
        self._event_bus = EventBus()

        # MT5
        self._mt5 = AsyncMT5Client(
            host=config.mt5.rpyc_host,
            port=config.mt5.rpyc_port,
        )

        # Database
        self._db = TrackingDB(db_path=config.database.path)

        # Channel registry
        self._registry = ChannelRegistry(config.channels)

        # Notifications
        self._telegram_notifier = TelegramNotifier(config.monitoring.telegram)
        self._slack_notifier = SlackNotifier(config.monitoring.slack)

        # Signal parser
        self._parser = SignalParser(
            config=config.signal_parser,
            event_bus=self._event_bus,
            mt5_client=self._mt5,
            channel_registry=self._registry,
            tracking_db=self._db,
        )

        # Telegram listener
        self._listener = TelegramListener(
            api_id=config.telegram_listener.api_id,
            api_hash=config.telegram_listener.api_hash,
            phone=config.telegram_listener.phone,
            session_path=config.telegram_listener.session_path,
            channel_registry=self._registry,
            signal_parser=self._parser,
            tracking_db=self._db,
        )

        # Risk manager — uses callback functions for MT5 access
        self._risk_manager = RiskManager(
            config=config,
            event_bus=self._event_bus,
            symbol_info_func=self._sync_symbol_info,
            account_state_func=self._sync_account_state,
            positions_func=self._sync_positions,
        )

        # Executor
        self._executor = OrderExecutor(
            event_bus=self._event_bus,
            mt5_client=self._mt5,
        )

        # Safety
        self._emergency = EmergencyStop(
            max_daily_loss_usd=config.account.initial_balance * config.risk.max_daily_loss_pct / 100,
            max_drawdown_usd=config.account.initial_balance * config.risk.max_drawdown_pct / 100,
        )

        # Monitoring
        self._position_monitor = PositionMonitor(
            mt5_client=self._mt5,
            event_bus=self._event_bus,
            tracking_db=self._db,
            poll_interval=config.position_monitor.poll_interval_seconds,
            account_state_func=self._sync_account_state,
            trailing_stop_config=config.trailing_stop,
            positions_callback=self._update_cached_positions,
            initial_balance=config.account.initial_balance,
            prop_firm_guard=self._risk_manager._prop_firm_guard,
            partial_profit_config=config.partial_profit,
        )

        # Signal generator (own technical signals)
        self._signal_generator = SignalGenerator(
            config=config,
            event_bus=self._event_bus,
            mt5_client=self._mt5,
        )

        # Journal + Summary
        self._journal = TradeJournal(
            event_bus=self._event_bus,
            tracking_db=self._db,
        )
        self._daily_summary = DailySummary(
            tracking_db=self._db,
            telegram_notifier=self._telegram_notifier,
            slack_notifier=self._slack_notifier,
            account_state_func=self._sync_account_state,
        )

    # ── Sync wrappers for callback-based interfaces ──

    def _sync_symbol_info(self, symbol: str) -> dict:
        """Return symbol info — live MT5 data preferred, config as fallback.

        Live data is cached at startup via _cache_live_symbol_info().
        This avoids the stale tick_value problem (e.g. USDJPY tick_value
        changes with the exchange rate, US30 tick_value=0.1 not 1.0).
        """
        # Prefer live MT5 data
        if symbol in self._live_symbol_info:
            return self._live_symbol_info[symbol]

        # Fallback to config
        for inst in self._config.instruments:
            if inst.symbol == symbol:
                return {
                    "symbol": inst.symbol,
                    "point": inst.point_size,
                    "trade_tick_value": inst.tick_value,
                    "volume_min": inst.min_lot,
                    "volume_max": inst.max_lot,
                    "volume_step": inst.lot_step,
                }
        return {}

    def _sync_account_state(self):
        """Return cached account state (non-blocking).

        Updated by _refresh_account_cache() called from PositionMonitor cycle.
        Falls back to initial balance if no cache yet.
        """
        if self._cached_account_state is not None:
            return self._cached_account_state
        from src.core.models import AccountState
        from datetime import datetime
        return AccountState(
            balance=self._config.account.initial_balance,
            equity=self._config.account.initial_balance,
            margin=0, free_margin=self._config.account.initial_balance,
            margin_level=0, profit=0, timestamp=datetime.now(),
        )

    def _update_cached_positions(self, positions: list) -> None:
        """Called by PositionMonitor every 30s with current MT5 positions."""
        self._cached_positions = positions

    async def _cache_live_symbol_info(self) -> None:
        """Fetch and cache symbol info from MT5 for all configured instruments.

        Live tick_value, point, contract_size etc. are essential for accurate
        lot sizing and P&L calculation. Config values are only fallbacks.
        """
        symbols = set()
        for inst in self._config.instruments:
            symbols.add(inst.symbol)
        # Also add signal_generator instruments
        if self._config.signal_generator:
            for sym in self._config.signal_generator.instruments:
                symbols.add(sym)

        for symbol in symbols:
            try:
                info = await self._mt5.symbol_info(symbol)
                if info and isinstance(info, dict):
                    self._live_symbol_info[symbol] = {
                        "symbol": symbol,
                        "point": float(info.get("point", 0.01)),
                        "trade_tick_value": float(info.get("trade_tick_value", 1.0)),
                        "trade_tick_size": float(info.get("trade_tick_size", 0.01)),
                        "trade_contract_size": float(info.get("trade_contract_size", 100)),
                        "volume_min": float(info.get("volume_min", 0.01)),
                        "volume_max": float(info.get("volume_max", 100.0)),
                        "volume_step": float(info.get("volume_step", 0.01)),
                        "digits": int(info.get("digits", 2)),
                    }
                    logger.info(
                        "Live symbol info: %s point=%s tick_value=%s contract=%s vol_max=%s",
                        symbol,
                        self._live_symbol_info[symbol]["point"],
                        self._live_symbol_info[symbol]["trade_tick_value"],
                        self._live_symbol_info[symbol]["trade_contract_size"],
                        self._live_symbol_info[symbol]["volume_max"],
                    )
                else:
                    logger.warning("No MT5 symbol_info for %s — using config fallback", symbol)
            except Exception:
                logger.warning("Failed to fetch symbol_info for %s", symbol, exc_info=True)

        logger.info(
            "Cached live symbol info for %d/%d instruments",
            len(self._live_symbol_info), len(symbols),
        )

    async def _account_cache_loop(self) -> None:
        """Background loop: refresh account state every 30s."""
        while not self._shutdown_event.is_set():
            await self._refresh_account_cache()
            # Persist peak equity so it survives restarts
            try:
                await self._db.save_bot_state(
                    "peak_equity", str(self._risk_manager.peak_equity)
                )
            except Exception:
                pass
            await asyncio.sleep(30)

    async def _refresh_account_cache(self) -> None:
        """Refresh cached account state from MT5 (called from async context)."""
        try:
            state = await self._mt5.account_info()
            if state:
                self._cached_account_state = state
        except Exception:
            pass  # keep stale cache rather than crash

    def _sync_positions(self, symbol: str | None = None):
        """Return cached positions for RiskManager (non-blocking).

        PositionMonitor updates _cached_positions every 30s poll cycle.
        We can't call MT5 async from sync context (deadlocks the event loop).
        """
        positions = self._cached_positions
        if symbol is not None:
            positions = [p for p in positions if p.symbol == symbol]
        return positions

    # ── Notification hooks ──

    async def _on_fill_notify(self, event: Event) -> None:
        """Send notifications and track when an order is filled."""
        if not isinstance(event, FillEvent) or event.order is None:
            return
        order = event.order
        source = order.comment or ""

        # Skip partial close fills (don't re-register or double-count)
        if source.startswith("partial:"):
            logger.info(
                "Partial close filled: %s %s %.2f lots @ %.2f",
                order.side.value, order.symbol, event.fill_volume, event.fill_price,
            )
            return

        # Track in our database (survives restart)
        try:
            ticket = getattr(order, "ticket", 0) or 0
            if ticket:
                await self._db.save_bot_position(
                    ticket=ticket, symbol=order.symbol,
                    side=order.side.value, volume=event.fill_volume,
                    open_price=event.fill_price,
                    sl=order.stop_loss, tp=order.take_profit,
                    source=source,
                )
                await self._db.increment_daily_trades()

                # Register with partial profit manager if multi-TP signal
                tp_levels = getattr(order, "take_profit_levels", [])
                pp = self._position_monitor._partial_profit
                if tp_levels and len(tp_levels) >= 2 and pp:
                    pp.register(
                        ticket=ticket,
                        side=order.side,
                        volume=event.fill_volume,
                        entry_price=event.fill_price,
                        tp_levels=tp_levels,
                    )
                    # Persist to DB
                    await self._db.save_partial_profit_state(
                        ticket=ticket,
                        tp_levels=tp_levels,
                        levels_hit=[],
                        original_volume=event.fill_volume,
                        entry_price=event.fill_price,
                        side=order.side.value,
                    )
        except Exception:
            logger.debug("Failed to persist bot position", exc_info=True)

        await self._telegram_notifier.send_trade_opened(
            symbol=order.symbol,
            side=order.side.value,
            volume=event.fill_volume,
            price=event.fill_price,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            source=source,
        )
        await self._slack_notifier.send_trade_opened(
            symbol=order.symbol,
            side=order.side.value,
            volume=event.fill_volume,
            price=event.fill_price,
            stop_loss=order.stop_loss,
            take_profit=order.take_profit,
            source=source,
        )

    async def _on_position_closed_notify(self, event: Event) -> None:
        """Send notifications when a position is closed."""
        if not isinstance(event, PositionClosedEvent) or event.position is None:
            return
        pos = event.position
        duration_h = 0.0
        if pos.open_time:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            open_time = pos.open_time
            if open_time.tzinfo is None:
                open_time = open_time.replace(tzinfo=timezone.utc)
            duration_h = (now - open_time).total_seconds() / 3600

        await self._telegram_notifier.send_trade_closed(
            symbol=pos.symbol,
            side=pos.side.value,
            volume=pos.volume,
            close_price=event.close_price,
            pnl=event.pnl,
            duration_hours=duration_h,
        )
        await self._slack_notifier.send_trade_closed(
            symbol=pos.symbol,
            side=pos.side.value,
            volume=pos.volume,
            close_price=event.close_price,
            pnl=event.pnl,
            duration_hours=duration_h,
        )

    # ── Lifecycle ──

    async def start(self) -> None:
        """Initialize all components and start the bot."""
        logger.info("=" * 60)
        logger.info("Trading Bot V2 starting...")
        logger.info("=" * 60)

        # 1. Connect to database
        await self._db.connect()

        # 2. Connect to MT5 (retry — container may still be starting)
        logger.info("Connecting to MT5 at %s:%d...", self._config.mt5.rpyc_host, self._config.mt5.rpyc_port)
        mt5_connected = False
        for attempt in range(1, 13):  # retry for up to ~2 minutes
            try:
                await self._mt5.connect()
                mt5_connected = True
                break
            except Exception as e:
                logger.warning("MT5 connect attempt %d/12 failed: %s", attempt, e)
                await asyncio.sleep(10)

        if not mt5_connected:
            logger.error("Could not connect to MT5 — bot will start but cannot trade")
            logger.error("Make sure the metatrader5 container is running and MT5 is logged in via VNC")

        # 2b. Cache live symbol info from MT5 (accurate tick values for lot sizing)
        if mt5_connected:
            await self._cache_live_symbol_info()

        # 3. Initialize event-driven components
        await self._risk_manager.initialize()
        await self._executor.initialize()
        await self._journal.initialize()

        # 4. Register notification handlers
        self._event_bus.subscribe("FILL", self._on_fill_notify)
        self._event_bus.subscribe("POSITION_CLOSED", self._on_position_closed_notify)

        # 5. Restore persisted state from database
        try:
            # Reset daily trade counter on restart (prevents stale count from
            # previous sessions/accounts blocking trades)
            await self._db.reset_daily_state()

            trailing_stops = await self._db.get_trailing_stops()
            if trailing_stops and self._position_monitor._trailing_manager:
                self._position_monitor._trailing_manager.restore(trailing_stops)

            # Restore partial profit states
            pp = self._position_monitor._partial_profit
            if pp:
                pp_rows = await self._db.get_partial_profit_states()
                if pp_rows:
                    from src.core.enums import OrderSide
                    from src.monitoring.partial_profit_manager import PartialProfitState
                    states = {}
                    for row in pp_rows:
                        side = OrderSide.BUY if row["side"] == "BUY" else OrderSide.SELL
                        state = PartialProfitState(
                            ticket=row["mt5_ticket"],
                            side=side,
                            entry_price=row["entry_price"],
                            original_volume=row["original_volume"],
                            tp_levels=row["tp_levels"],
                            levels_hit=row["levels_hit"],
                        )
                        states[row["mt5_ticket"]] = state
                    pp.restore(states)

            daily_count = await self._db.get_daily_trade_count()
            self._risk_manager.set_daily_trade_count(daily_count)

            saved_peak = await self._db.get_bot_state("peak_equity")
            if saved_peak:
                self._risk_manager.set_peak_equity(float(saved_peak))
        except Exception:
            logger.warning("Failed to restore persisted state", exc_info=True)

        # 5b. Sync MT5 positions into cache BEFORE signal generator starts
        # This prevents the bot from opening duplicate positions on restart
        if mt5_connected:
            try:
                mt5_positions = await self._mt5.positions_get()
                if mt5_positions:
                    self._cached_positions = mt5_positions
                    logger.info(
                        "Pre-synced %d MT5 position(s) into cache",
                        len(mt5_positions),
                    )
                    # Also sync any orphan BOT positions into our DB (skip manual)
                    for pos in mt5_positions:
                        if not pos.comment or not pos.comment.startswith("tg:"):
                            continue  # Skip manual positions
                        existing = await self._db.get_trade_by_ticket(pos.ticket)
                        if not existing:
                            await self._db.save_bot_position(
                                ticket=pos.ticket, symbol=pos.symbol,
                                side="BUY" if pos.side.value == "BUY" else "SELL",
                                volume=pos.volume, open_price=pos.open_price,
                                sl=pos.stop_loss, tp=pos.take_profit,
                                source="synced-from-mt5",
                            )
                            logger.info(
                                "Synced orphan MT5 position #%d %s into DB",
                                pos.ticket, pos.symbol,
                            )
            except Exception:
                logger.warning("Failed to pre-sync MT5 positions", exc_info=True)

        # 5c. Clean stale DB positions not found in MT5 (e.g. after account reset)
        try:
            db_open = await self._db.get_open_bot_positions()
            mt5_tickets = {pos.ticket for pos in self._cached_positions}
            for db_pos in db_open:
                if db_pos["mt5_ticket"] not in mt5_tickets:
                    await self._db.close_bot_position(
                        db_pos["mt5_ticket"], 0, 0, "stale-cleanup"
                    )
                    logger.info(
                        "Cleaned stale DB position #%d (not in MT5)",
                        db_pos["mt5_ticket"],
                    )
        except Exception:
            logger.debug("Stale position cleanup skipped", exc_info=True)

        # 6. Start background services (skip if MT5 not connected)
        if mt5_connected:
            await self._position_monitor.start()
            # Start account cache refresh loop (avoids deadlock in RiskManager)
            asyncio.create_task(self._account_cache_loop())
            if self._config.signal_generator.enabled:
                await self._signal_generator.start()
            else:
                logger.info("SignalGenerator disabled in config")
        else:
            logger.warning("PositionMonitor + SignalGenerator skipped — MT5 not connected")
        await self._daily_summary.start()

        # 6. Start Telegram listener
        logger.info("Starting Telegram listener...")
        await self._listener.start()

        # 7. Start event bus processing loop
        event_bus_task = asyncio.create_task(self._event_bus.process())

        # 8. Health gate — refuse to run as a zombie
        n_channels = len(self._registry.channel_ids)
        telegram_ok = self._listener._client is not None and self._listener._running
        degraded_parts: list[str] = []

        if not mt5_connected:
            degraded_parts.append("MT5 disconnected")
        if not telegram_ok or n_channels == 0:
            degraded_parts.append(f"Telegram {'not authenticated' if not telegram_ok else 'has 0 channels'}")

        if not mt5_connected and (not telegram_ok or n_channels == 0):
            msg = (
                "FATAL: Both MT5 and Telegram are down — bot cannot trade. "
                "Fix: (1) start MT5 container (2) run telegram auth script. Exiting."
            )
            logger.critical(msg)
            await self._slack_notifier.send(f"CRITICAL: {msg}")
            await self._telegram_notifier.send(f"CRITICAL: {msg}")
            self._shutdown_event.set()
            self._event_bus.stop()
            await event_bus_task
            return

        if degraded_parts:
            degraded_msg = f"Trading Bot V2 started DEGRADED: {', '.join(degraded_parts)}"
            logger.warning(degraded_msg)
            await self._slack_notifier.send(f"WARNING: {degraded_msg}")
            await self._telegram_notifier.send(f"WARNING: {degraded_msg}")

        logger.info("=" * 60)
        logger.info("Trading Bot V2 is LIVE")
        logger.info("  MT5: %s", "connected" if mt5_connected else "DISCONNECTED")
        logger.info("  Telegram: %s", "connected" if telegram_ok else "NOT AUTHENTICATED")
        logger.info("  Channels: %d", n_channels)
        logger.info("  Instruments: %s", [i.symbol for i in self._config.instruments])
        logger.info("  Risk per trade: %.1f%%", self._config.account.risk_per_trade_pct)
        logger.info("  Max lot: %.2f", self._config.account.max_lot_per_trade)
        logger.info("=" * 60)

        # Notify that we're live
        await self._telegram_notifier.send("Trading Bot V2 started")
        await self._slack_notifier.send("Trading Bot V2 started")

        # Wait for shutdown signal
        await self._shutdown_event.wait()

        # Cleanup
        self._event_bus.stop()
        await event_bus_task

    async def stop(self) -> None:
        """Gracefully shutdown all components."""
        logger.info("Shutting down Trading Bot V2...")

        await self._daily_summary.stop()
        await self._signal_generator.stop()
        await self._position_monitor.stop()
        await self._listener.stop()
        await self._mt5.disconnect()
        await self._db.close()

        await self._telegram_notifier.send("Trading Bot V2 stopped")
        await self._slack_notifier.send("Trading Bot V2 stopped")

        self._shutdown_event.set()
        logger.info("Trading Bot V2 stopped")

    def request_shutdown(self) -> None:
        """Signal the bot to shut down (called from signal handlers)."""
        self._shutdown_event.set()


async def run() -> None:
    """Main async entry point."""
    # Load env vars
    load_dotenv()

    # Load config and set up logging FIRST so errors are captured
    config = load_config()
    _setup_logging(config)

    try:
        bot = TradingBot(config)
    except Exception:
        logger.critical("Failed to initialize TradingBot — check .env and config", exc_info=True)
        return

    # Handle OS signals for graceful shutdown
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, bot.request_shutdown)

    try:
        await bot.start()
    except KeyboardInterrupt:
        pass
    except Exception:
        logger.critical("Unhandled exception in bot.start()", exc_info=True)
    finally:
        await bot.stop()


def main() -> None:
    """CLI entry point."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
