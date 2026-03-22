"""MT5 client using RPyC classic connection to the gmag11 Docker container.

The gmag11/metatrader5_vnc container runs an mt5linux RPyC SlaveService
that exposes the full MetaTrader5 Python API over the network. We connect
via rpyc.classic.connect() which gives access to conn.modules['MetaTrader5'].

RPyC returns "netref" proxy objects. We convert them to native Python
types (dicts, lists) to avoid serialization issues downstream. Trade
requests are executed remotely via conn.execute() because MT5's order_send
rejects dicts passed through RPyC serialization.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import pandas as pd
import rpyc

from src.core.enums import Timeframe
from src.core.exceptions import MT5APIError, MT5ConnectionError
from src.core.models import AccountState, Position, Tick

logger = logging.getLogger(__name__)


def _to_native(obj: Any) -> Any:
    """Convert RPyC netref proxy objects to native Python types."""
    if obj is None:
        return None
    try:
        return rpyc.classic.obtain(obj)
    except Exception:
        return obj


def _named_tuple_to_dict(nt: Any) -> dict[str, Any]:
    """Convert a named tuple (possibly RPyC netref) to a plain dict."""
    try:
        d = nt._asdict()
        return {str(k): _to_native(v) for k, v in d.items()}
    except Exception:
        try:
            return {field: _to_native(getattr(nt, field)) for field in nt._fields}
        except Exception:
            return {}


class MT5Client:
    """Synchronous MT5 client via RPyC connection to gmag11 Docker container."""

    def __init__(self, host: str = "localhost", port: int = 8001) -> None:
        self._host = host
        self._port = port
        self._conn: rpyc.Connection | None = None
        self._mt5: Any = None

    def connect(self) -> None:
        """Connect to the MT5 RPyC SlaveService and initialize."""
        try:
            self._conn = rpyc.classic.connect(self._host, self._port)
            self._mt5 = self._conn.modules["MetaTrader5"]
        except Exception as e:
            raise MT5ConnectionError(
                f"Failed to connect to MT5 RPyC at {self._host}:{self._port}: {e}"
            ) from e

        if not self._mt5.initialize():
            error = self._mt5.last_error()
            raise MT5ConnectionError(f"MT5 initialize() failed: {_to_native(error)}")

        info = self._mt5.account_info()
        if info is None:
            raise MT5ConnectionError("MT5 connected but account_info() returned None")

        info_dict = _named_tuple_to_dict(info)
        logger.info(
            "Connected to MT5 — Login: %s, Server: %s, Balance: %.2f %s",
            info_dict.get("login"),
            info_dict.get("server"),
            info_dict.get("balance", 0),
            info_dict.get("currency", ""),
        )

    def disconnect(self) -> None:
        """Shutdown MT5 and close RPyC connection."""
        if self._mt5:
            try:
                self._mt5.shutdown()
            except Exception:
                pass
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
        self._mt5 = None
        self._conn = None

    def __enter__(self) -> MT5Client:
        self.connect()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._conn is not None and not self._conn.closed and self._mt5 is not None

    def _ensure_connected(self) -> None:
        if not self.is_connected:
            raise MT5ConnectionError("Not connected. Call connect() first.")

    # --- Account ---

    def account_info(self) -> AccountState:
        """Get account state (balance, equity, margin, etc.)."""
        self._ensure_connected()
        info = self._mt5.account_info()
        if info is None:
            raise MT5APIError("account_info() returned None")
        d = _named_tuple_to_dict(info)
        return AccountState(
            balance=float(d.get("balance", 0)),
            equity=float(d.get("equity", 0)),
            margin=float(d.get("margin", 0)),
            free_margin=float(d.get("margin_free", 0)),
            margin_level=float(d.get("margin_level", 0) or 0),
            profit=float(d.get("profit", 0)),
            timestamp=datetime.now(),
        )

    def account_info_raw(self) -> dict[str, Any]:
        """Get raw account info as dict (all fields)."""
        self._ensure_connected()
        info = self._mt5.account_info()
        if info is None:
            raise MT5APIError("account_info() returned None")
        return _named_tuple_to_dict(info)

    # --- Symbol Info ---

    def symbol_info(self, symbol: str) -> dict[str, Any]:
        """Get symbol specification (point, digits, trade sizes, etc.)."""
        self._ensure_connected()
        info = self._mt5.symbol_info(symbol)
        if info is None:
            raise MT5APIError(f"symbol_info({symbol}) returned None — symbol may not exist")
        return _named_tuple_to_dict(info)

    def symbol_info_tick(self, symbol: str) -> Tick:
        """Get latest tick for a symbol."""
        self._ensure_connected()
        tick = self._mt5.symbol_info_tick(symbol)
        if tick is None:
            raise MT5APIError(f"symbol_info_tick({symbol}) returned None")
        d = _named_tuple_to_dict(tick)
        return Tick(
            symbol=symbol,
            timestamp=datetime.fromtimestamp(d.get("time", 0)),
            bid=float(d.get("bid", 0)),
            ask=float(d.get("ask", 0)),
            last=float(d.get("last", 0)),
            volume=float(d.get("volume", 0)),
        )

    # --- Market Data ---

    def get_bars(
        self,
        symbol: str,
        timeframe: Timeframe | str,
        count: int = 100,
        start_pos: int = 0,
    ) -> pd.DataFrame:
        """Get the last N bars as a DataFrame."""
        self._ensure_connected()
        tf = self._resolve_timeframe(timeframe)
        rates = self._mt5.copy_rates_from_pos(symbol, tf, start_pos, count)
        return self._rates_to_dataframe(rates)

    def get_bars_range(
        self,
        symbol: str,
        timeframe: Timeframe | str,
        date_from: datetime,
        date_to: datetime,
    ) -> pd.DataFrame:
        """Get bars within a date range."""
        self._ensure_connected()
        tf = self._resolve_timeframe(timeframe)
        rates = self._mt5.copy_rates_range(symbol, tf, date_from, date_to)
        return self._rates_to_dataframe(rates)

    def _rates_to_dataframe(self, rates: Any) -> pd.DataFrame:
        """Convert MT5 rates to DataFrame."""
        if rates is None or len(rates) == 0:
            return pd.DataFrame(
                columns=["open", "high", "low", "close", "volume", "tick_volume", "spread"]
            )
        rates_native = _to_native(rates)
        df = pd.DataFrame(rates_native)
        if "time" in df.columns:
            df["timestamp"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df = df.set_index("timestamp")
        if "real_volume" in df.columns:
            df = df.rename(columns={"real_volume": "volume"})
        for col in ["tick_volume", "spread"]:
            if col not in df.columns:
                df[col] = 0
        if "volume" not in df.columns:
            df["volume"] = 0
        return df[["open", "high", "low", "close", "volume", "tick_volume", "spread"]]

    # --- Trading ---

    def order_send(self, request: dict[str, Any]) -> dict[str, Any]:
        """Send a trade order via remote execution."""
        self._ensure_connected()
        return self._execute_order("order_send", request)

    def order_check(self, request: dict[str, Any]) -> dict[str, Any]:
        """Check if an order can be placed."""
        self._ensure_connected()
        return self._execute_order("order_check", request)

    def _execute_order(self, func_name: str, request: dict[str, Any]) -> dict[str, Any]:
        """Execute an order function remotely to avoid RPyC dict serialization issues."""
        import json
        request_json = json.dumps(request)
        self._conn.execute(
            "import json, MetaTrader5 as mt5\n"
            "mt5.initialize()\n"
            f"_req = json.loads({request_json!r})\n"
            f"_result = mt5.{func_name}(_req)\n"
            "_result_dict = _result._asdict() if _result is not None else None"
        )
        result_dict = _to_native(self._conn.namespace["_result_dict"])
        if result_dict is None:
            self._conn.execute("_last_err = mt5.last_error()")
            error = _to_native(self._conn.namespace["_last_err"])
            raise MT5APIError(f"{func_name}() failed: {error}")

        retcode = result_dict.get("retcode", 0)
        if func_name == "order_send" and retcode != 10009:
            logger.warning(
                "Order result: retcode=%d, comment=%s",
                retcode, result_dict.get("comment", ""),
            )
        return result_dict

    def positions_get(self, symbol: str | None = None) -> list[Position]:
        """Get open positions."""
        self._ensure_connected()
        if symbol:
            positions = self._mt5.positions_get(symbol=symbol)
        else:
            positions = self._mt5.positions_get()
        if positions is None:
            return []

        from src.core.enums import OrderSide
        result = []
        for p in positions:
            d = _named_tuple_to_dict(p)
            result.append(Position(
                ticket=int(d.get("ticket", 0)),
                symbol=str(d.get("symbol", "")),
                side=OrderSide.BUY if d.get("type", 0) == 0 else OrderSide.SELL,
                volume=float(d.get("volume", 0)),
                open_price=float(d.get("price_open", 0)),
                open_time=datetime.fromtimestamp(d.get("time", 0)),
                stop_loss=float(d["sl"]) if d.get("sl") else None,
                take_profit=float(d["tp"]) if d.get("tp") else None,
                current_price=float(d.get("price_current", 0)),
                profit=float(d.get("profit", 0)),
                swap=float(d.get("swap", 0)),
                commission=float(d.get("commission", 0)),
                magic=int(d.get("magic", 0)),
                comment=str(d.get("comment", "")),
            ))
        return result

    def orders_get(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Get pending orders."""
        self._ensure_connected()
        if symbol:
            orders = self._mt5.orders_get(symbol=symbol)
        else:
            orders = self._mt5.orders_get()
        if orders is None:
            return []
        return [_named_tuple_to_dict(o) for o in orders]

    def history_deals_get(
        self, date_from: datetime, date_to: datetime
    ) -> list[dict[str, Any]]:
        """Get historical deals within a date range."""
        self._ensure_connected()
        deals = self._mt5.history_deals_get(date_from, date_to)
        if deals is None:
            return []
        return [_named_tuple_to_dict(d) for d in deals]

    # --- Helpers ---

    def _resolve_timeframe(self, timeframe: Timeframe | str) -> int:
        """Convert Timeframe enum or string to MT5 integer constant."""
        if isinstance(timeframe, Timeframe):
            return timeframe.mt5_value
        return Timeframe(timeframe).mt5_value


class AsyncMT5Client:
    """Async wrapper around MT5Client with auto-reconnect.

    RPyC is synchronous, so all calls are wrapped in asyncio.to_thread()
    to avoid blocking the event loop.
    """

    _RECONNECTABLE_ERRORS = (
        MT5ConnectionError,
        EOFError,
        ConnectionError,
        OSError,
    )

    def __init__(
        self,
        host: str = "localhost",
        port: int = 8001,
        max_reconnect_attempts: int = 10,
        max_backoff_seconds: float = 60.0,
    ) -> None:
        self._sync = MT5Client(host, port)
        self._host = host
        self._port = port
        self._max_attempts = max_reconnect_attempts
        self._max_backoff = max_backoff_seconds
        self._reconnect_count = 0

    async def connect(self) -> None:
        await asyncio.to_thread(self._sync.connect)
        self._reconnect_count = 0

    async def disconnect(self) -> None:
        await asyncio.to_thread(self._sync.disconnect)

    async def __aenter__(self) -> AsyncMT5Client:
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._sync.is_connected

    async def _reconnect(self) -> None:
        """Attempt to reconnect with exponential backoff."""
        for attempt in range(1, self._max_attempts + 1):
            delay = min(2 ** (attempt - 1), self._max_backoff)
            logger.warning(
                "MT5 reconnect attempt %d/%d (backoff %.1fs)",
                attempt, self._max_attempts, delay,
            )
            await asyncio.sleep(delay)
            try:
                try:
                    self._sync.disconnect()
                except Exception:
                    pass
                await asyncio.to_thread(self._sync.connect)
                self._reconnect_count += 1
                logger.info(
                    "MT5 reconnected (attempt %d, total reconnects: %d)",
                    attempt, self._reconnect_count,
                )
                return
            except Exception as e:
                logger.warning("Reconnect attempt %d failed: %s", attempt, e)

        raise MT5ConnectionError(
            f"Failed to reconnect after {self._max_attempts} attempts"
        )

    async def _call_with_reconnect(self, func, *args):
        """Call a sync MT5 function, reconnecting on connection errors."""
        try:
            return await asyncio.to_thread(func, *args)
        except self._RECONNECTABLE_ERRORS as e:
            logger.warning("MT5 connection lost during call: %s", e)
            await self._reconnect()
            return await asyncio.to_thread(func, *args)

    async def account_info(self) -> AccountState:
        return await self._call_with_reconnect(self._sync.account_info)

    async def account_info_raw(self) -> dict[str, Any]:
        return await self._call_with_reconnect(self._sync.account_info_raw)

    async def symbol_info(self, symbol: str) -> dict[str, Any]:
        return await self._call_with_reconnect(self._sync.symbol_info, symbol)

    async def symbol_info_tick(self, symbol: str) -> Tick:
        return await self._call_with_reconnect(self._sync.symbol_info_tick, symbol)

    async def get_bars(
        self, symbol: str, timeframe: Timeframe | str, count: int = 100, start_pos: int = 0
    ) -> pd.DataFrame:
        return await self._call_with_reconnect(
            self._sync.get_bars, symbol, timeframe, count, start_pos
        )

    async def get_bars_range(
        self, symbol: str, timeframe: Timeframe | str,
        date_from: datetime, date_to: datetime,
    ) -> pd.DataFrame:
        return await self._call_with_reconnect(
            self._sync.get_bars_range, symbol, timeframe, date_from, date_to
        )

    async def order_send(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self._call_with_reconnect(self._sync.order_send, request)

    async def order_check(self, request: dict[str, Any]) -> dict[str, Any]:
        return await self._call_with_reconnect(self._sync.order_check, request)

    async def positions_get(self, symbol: str | None = None) -> list[Position]:
        return await self._call_with_reconnect(self._sync.positions_get, symbol)

    async def orders_get(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return await self._call_with_reconnect(self._sync.orders_get, symbol)

    async def history_deals_get(
        self, date_from: datetime, date_to: datetime
    ) -> list[dict[str, Any]]:
        return await self._call_with_reconnect(
            self._sync.history_deals_get, date_from, date_to
        )
