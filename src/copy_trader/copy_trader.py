"""CopyTrader — mirrors trades from source MT5 account to dest MT5 account.

Pure replicator. NO strategies, NO signal generators, NO news filter.
Polls source positions every `poll_interval_ms`, diffs against last
snapshot, fires open/modify/close on dest accordingly.

Hard isolation rules (per design spec):
  * Source account A is single source of truth — bot ignores any manual
    actions on dest B. If user closes B by hand, B closes but A stays
    open and bot will not re-mirror.
  * Existing positions on A at boot are added to ignored set — never
    mirrored.
  * News filter is NOT consulted; trader's discretion is respected.
  * Lot mode `exact` (1:1) is the only validated path. Other modes wired
    in `_compute_dest_volume` for future use, off by default.

The class accepts callable `source_mt5` and `dest_mt5` clients with the
shape of `AsyncMT5Client`, plus a `MirrorJournal` for state and a
`SlackNotifier` for alerts. All MT5 access is mockable for unit tests.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from src.config.copy_trader_schema import CopyTraderConfig
from src.copy_trader.mirror_journal import MirrorJournal
from src.copy_trader.notifier import SlackNotifier
from src.core.enums import OrderSide, OrderType
from src.core.models import Position

logger = logging.getLogger(__name__)


class _MT5Like(Protocol):
    """Minimal interface required from an MT5 client. Lets tests inject mocks."""

    async def positions_get(self, symbol: str | None = None) -> list[Position]: ...
    async def order_send(self, request: dict[str, Any]) -> dict[str, Any]: ...
    async def position_modify(
        self, ticket: int, stop_loss: float | None = None,
        take_profit: float | None = None, symbol: str | None = None,
    ) -> dict[str, Any]: ...
    async def symbol_info_tick(self, symbol: str) -> Any: ...


# MT5 trade-action / order-type / filling constants (avoid importing MetaTrader5 module here)
_TRADE_ACTION_DEAL = 1
_TRADE_ACTION_SLTP = 6
_ORDER_TYPE_BUY = 0
_ORDER_TYPE_SELL = 1
_ORDER_TIME_GTC = 0
_ORDER_FILLING_IOC = 1


@dataclass
class _MirrorRow:
    """In-memory mirror state, mirrors a row in mirror_map table."""

    src_ticket: int
    dest_ticket: int
    symbol: str
    side: OrderSide
    volume: float
    src_open_price: float
    dest_open_price: float
    src_sl: Optional[float]
    src_tp: Optional[float]


class CopyTrader:
    """Drives the mirror loop. One instance per source/dest pair."""

    def __init__(
        self,
        source_mt5: _MT5Like,
        dest_mt5: _MT5Like,
        journal: MirrorJournal,
        notifier: SlackNotifier,
        config: CopyTraderConfig,
    ) -> None:
        self._src = source_mt5
        self._dst = dest_mt5
        self._journal = journal
        self._notify = notifier
        self._cfg = config

        self._mirror: dict[int, _MirrorRow] = {}
        self._ignored: set[int] = set()
        # Per-ticket "last seen" Position to detect SL/TP modifications
        self._last_seen: dict[int, Position] = {}
        self._running = False
        self._first_cycle = True

    # ----- Public lifecycle -----

    async def boot(self) -> None:
        """Pre-populate ignored set + reload mirror_map from journal.

        Must be called once before `run_forever()`.
        """
        # Persisted ignored tickets (e.g. from a prior boot cycle)
        self._ignored.update(self._journal.load_ignored())

        # Persisted mirror map — reconcile on restart
        persisted = self._journal.load_all_mappings()
        for src_t, row in persisted.items():
            self._mirror[src_t] = _MirrorRow(
                src_ticket=src_t,
                dest_ticket=row["dest_ticket"],
                symbol=row["symbol"],
                side=OrderSide(row["side"]),
                volume=row["volume"],
                src_open_price=row["src_open_price"],
                dest_open_price=row["dest_open_price"],
                src_sl=row["src_sl"],
                src_tp=row["src_tp"],
            )
        if persisted:
            logger.info("Restored %d mirror mappings from journal", len(persisted))

        if self._cfg.ignore_existing_at_boot:
            current_src = await self._src.positions_get()
            for pos in current_src:
                if pos.ticket not in self._mirror:  # not a known mirror
                    self._ignored.add(pos.ticket)
                    self._journal.add_ignored(pos.ticket, reason="boot_snapshot")
            logger.info(
                "Boot snapshot: %d source positions ignored (pre-existing)",
                len(self._ignored),
            )

    async def run_forever(self) -> None:
        """Main poll loop. Cancel via task.cancel() or self.stop()."""
        self._running = True
        interval = self._cfg.poll_interval_ms / 1000.0
        while self._running:
            cycle_start = time.monotonic()
            try:
                await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("CopyTrader cycle error (continuing)")
                await self._notify.error("CopyTrader cycle error", fields={"detail": "see logs"})
            elapsed = time.monotonic() - cycle_start
            sleep_for = max(0.0, interval - elapsed)
            await asyncio.sleep(sleep_for)

    def stop(self) -> None:
        self._running = False

    # ----- Cycle -----

    async def _cycle(self) -> None:
        positions_now = await self._src.positions_get()
        present_tickets = {p.ticket for p in positions_now}

        # Closes (in mirror but not in present)
        for src_t in list(self._mirror.keys()):
            if src_t not in present_tickets:
                await self._handle_close(src_t)

        # Opens + modifies
        for pos in positions_now:
            if pos.ticket in self._ignored:
                continue
            if pos.ticket not in self._mirror:
                await self._handle_open(pos)
            else:
                await self._handle_modify_if_changed(pos)
            self._last_seen[pos.ticket] = pos

        self._first_cycle = False

    # ----- Open -----

    async def _handle_open(self, pos: Position) -> None:
        volume = self._compute_dest_volume(pos.volume)
        if volume < self._cfg.lot_min:
            logger.warning(
                "Mirror volume %.4f below lot_min %.2f; skipping src_ticket=%d",
                volume, self._cfg.lot_min, pos.ticket,
            )
            self._journal.log_event(
                "OPEN", success=False, src_ticket=pos.ticket,
                symbol=pos.symbol, side=pos.side.value, volume=volume,
                error_message="below lot_min",
            )
            await self._notify.error(
                f"Mirror skipped (volume below floor) src_ticket={pos.ticket}"
            )
            self._ignored.add(pos.ticket)  # don't keep retrying every cycle
            self._journal.add_ignored(pos.ticket, reason="below_lot_min")
            return
        volume = min(volume, self._cfg.lot_max)

        request = self._build_market_request(
            symbol=pos.symbol, side=pos.side, volume=volume,
            stop_loss=pos.stop_loss, take_profit=pos.take_profit,
            comment=f"mirror:src={pos.ticket}",
        )

        attempt = 0
        send_started = time.monotonic()
        last_error = None
        while attempt <= self._cfg.order_send_max_retries:
            attempt += 1
            try:
                result = await self._dst.order_send(request)
                retcode = result.get("retcode", -1)
                if retcode == 10009:  # TRADE_RETCODE_DONE
                    dest_ticket = int(result.get("order", 0) or result.get("deal", 0) or 0)
                    dest_price = float(result.get("price", 0.0) or pos.open_price)
                    self._mirror[pos.ticket] = _MirrorRow(
                        src_ticket=pos.ticket,
                        dest_ticket=dest_ticket,
                        symbol=pos.symbol,
                        side=pos.side,
                        volume=volume,
                        src_open_price=pos.open_price,
                        dest_open_price=dest_price,
                        src_sl=pos.stop_loss,
                        src_tp=pos.take_profit,
                    )
                    self._journal.upsert_mapping(
                        src_ticket=pos.ticket, dest_ticket=dest_ticket,
                        symbol=pos.symbol, side=pos.side.value, volume=volume,
                        src_open_price=pos.open_price, dest_open_price=dest_price,
                        src_sl=pos.stop_loss, src_tp=pos.take_profit,
                    )
                    self._journal.log_event(
                        "OPEN", success=True, src_ticket=pos.ticket,
                        dest_ticket=dest_ticket, symbol=pos.symbol,
                        side=pos.side.value, volume=volume, price=dest_price,
                        sl=pos.stop_loss, tp=pos.take_profit,
                        latency_ms=int((time.monotonic() - send_started) * 1000),
                        raw_response=dict(result),
                    )
                    await self._notify.open(
                        src_ticket=pos.ticket, dest_ticket=dest_ticket,
                        symbol=pos.symbol, side=pos.side.value,
                        volume=volume, price=dest_price,
                    )
                    return
                last_error = result.get("comment", f"retcode={retcode}")
            except Exception as exc:
                last_error = str(exc)
            if attempt <= self._cfg.order_send_max_retries:
                await asyncio.sleep(self._cfg.order_send_retry_delay_seconds)

        # All attempts failed
        self._journal.log_event(
            "OPEN", success=False, src_ticket=pos.ticket,
            symbol=pos.symbol, side=pos.side.value, volume=volume,
            error_message=str(last_error),
        )
        await self._notify.error(
            f"Mirror OPEN failed: {last_error}",
            fields={"src_ticket": pos.ticket, "symbol": pos.symbol},
        )

    # ----- Close -----

    async def _handle_close(self, src_ticket: int) -> None:
        row = self._mirror[src_ticket]
        request = self._build_close_request(row)
        send_started = time.monotonic()
        try:
            result = await self._dst.order_send(request)
            retcode = result.get("retcode", -1)
            success = retcode == 10009
            self._journal.log_event(
                "CLOSE", success=success,
                src_ticket=src_ticket, dest_ticket=row.dest_ticket,
                symbol=row.symbol, side=row.side.value, volume=row.volume,
                latency_ms=int((time.monotonic() - send_started) * 1000),
                error_message=None if success else str(result.get("comment")),
                raw_response=dict(result),
            )
            if success:
                await self._notify.close(
                    src_ticket=src_ticket, dest_ticket=row.dest_ticket,
                    symbol=row.symbol,
                )
            else:
                await self._notify.error(
                    f"Mirror CLOSE failed: {result.get('comment')}",
                    fields={"src_ticket": src_ticket, "dest_ticket": row.dest_ticket},
                )
        except Exception as exc:
            self._journal.log_event(
                "CLOSE", success=False,
                src_ticket=src_ticket, dest_ticket=row.dest_ticket,
                symbol=row.symbol, error_message=str(exc),
            )
            await self._notify.error(
                f"Mirror CLOSE exception: {exc}",
                fields={"src_ticket": src_ticket, "dest_ticket": row.dest_ticket},
            )
        finally:
            # Remove from in-memory + persisted maps regardless — source closed,
            # we don't keep tracking it. If broker close failed, alert + manual fix.
            self._mirror.pop(src_ticket, None)
            self._last_seen.pop(src_ticket, None)
            self._journal.remove_mapping(src_ticket)

    # ----- Modify (SL/TP sync) -----

    async def _handle_modify_if_changed(self, pos: Position) -> None:
        row = self._mirror[pos.ticket]
        if not self._sl_or_tp_meaningfully_changed(row, pos):
            return
        send_started = time.monotonic()
        try:
            result = await self._dst.position_modify(
                ticket=row.dest_ticket,
                stop_loss=pos.stop_loss,
                take_profit=pos.take_profit,
                symbol=pos.symbol,
            )
            retcode = result.get("retcode", -1)
            success = retcode == 10009
            row.src_sl = pos.stop_loss
            row.src_tp = pos.take_profit
            self._journal.update_mapping_sl_tp(pos.ticket, pos.stop_loss, pos.take_profit)
            self._journal.log_event(
                "MODIFY", success=success,
                src_ticket=pos.ticket, dest_ticket=row.dest_ticket,
                symbol=pos.symbol, sl=pos.stop_loss, tp=pos.take_profit,
                latency_ms=int((time.monotonic() - send_started) * 1000),
                error_message=None if success else str(result.get("comment")),
                raw_response=dict(result),
            )
            if success:
                await self._notify.modify(
                    src_ticket=pos.ticket, dest_ticket=row.dest_ticket,
                    sl=pos.stop_loss, tp=pos.take_profit,
                )
            else:
                await self._notify.error(
                    f"Mirror MODIFY failed: {result.get('comment')}",
                    fields={"src_ticket": pos.ticket, "dest_ticket": row.dest_ticket},
                )
        except Exception as exc:
            self._journal.log_event(
                "MODIFY", success=False,
                src_ticket=pos.ticket, dest_ticket=row.dest_ticket,
                symbol=pos.symbol, error_message=str(exc),
            )
            await self._notify.error(
                f"Mirror MODIFY exception: {exc}",
                fields={"src_ticket": pos.ticket, "dest_ticket": row.dest_ticket},
            )

    # ----- Helpers -----

    def _compute_dest_volume(self, src_volume: float) -> float:
        cfg = self._cfg
        if cfg.lot_mode == "exact":
            return float(src_volume)
        if cfg.lot_mode == "multiplier":
            return float(src_volume) * cfg.lot_multiplier
        # 'proportional' would need account balance ratios — not in v1 scope
        return float(src_volume)

    def _sl_or_tp_meaningfully_changed(self, row: _MirrorRow, pos: Position) -> bool:
        # Use absolute price diff with min-points threshold from config.
        # Caller-side broker decimals can jitter by sub-pip noise; ignore.
        threshold = max(self._cfg.sl_change_min_points, self._cfg.tp_change_min_points) * 0.0001
        sl_now = pos.stop_loss or 0.0
        tp_now = pos.take_profit or 0.0
        sl_prev = row.src_sl or 0.0
        tp_prev = row.src_tp or 0.0
        return abs(sl_now - sl_prev) > threshold or abs(tp_now - tp_prev) > threshold

    def _build_market_request(
        self,
        *,
        symbol: str,
        side: OrderSide,
        volume: float,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        comment: str,
    ) -> dict[str, Any]:
        order_type = _ORDER_TYPE_BUY if side == OrderSide.BUY else _ORDER_TYPE_SELL
        request: dict[str, Any] = {
            "action": _TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(volume),
            "type": order_type,
            "deviation": 20,
            "magic": 0,  # mirror trades; no bot magic, treats as "external"
            "comment": comment,
            "type_time": _ORDER_TIME_GTC,
            "type_filling": _ORDER_FILLING_IOC,
        }
        if stop_loss is not None:
            request["sl"] = float(stop_loss)
        if take_profit is not None:
            request["tp"] = float(take_profit)
        return request

    def _build_close_request(self, row: _MirrorRow) -> dict[str, Any]:
        # Closing = market order in OPPOSITE direction with position= field
        close_side = _ORDER_TYPE_SELL if row.side == OrderSide.BUY else _ORDER_TYPE_BUY
        return {
            "action": _TRADE_ACTION_DEAL,
            "symbol": row.symbol,
            "volume": float(row.volume),
            "type": close_side,
            "position": row.dest_ticket,
            "deviation": 20,
            "magic": 0,
            "comment": f"mirror_close:src={row.src_ticket}",
            "type_time": _ORDER_TIME_GTC,
            "type_filling": _ORDER_FILLING_IOC,
        }

    # ----- Test/diagnostic accessors -----

    @property
    def stats(self) -> dict[str, int]:
        return {
            "mirror_open_count": len(self._mirror),
            "ignored_count": len(self._ignored),
        }


# Re-export for convenience
LotScalingMode = str  # Module-level alias for type clarity
CopyTraderConfig = CopyTraderConfig  # noqa: F811
