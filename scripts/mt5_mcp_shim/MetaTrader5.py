"""RPyC-backed shim of the Windows-only ``MetaTrader5`` package.

Lets packages that hard-depend on ``MetaTrader5`` (e.g. metatrader-mcp-server)
run on macOS/Linux by forwarding every module-level call to the real
MetaTrader5 module inside the Wine container's Python, over the same
mt5linux RPyC bridge the trading bot uses (src/mt5/client.py).

Config via env:
    MT5_RPYC_HOST  (default 127.0.0.1)
    MT5_RPYC_PORT  (default 8001)

Conversion rules (netrefs never escape this module):
    - namedtuples (account_info, positions, order results…) -> attribute
      objects that also support ._asdict(), recursively converted
    - copy_rates_* / copy_ticks_* -> list[dict] (pandas.DataFrame accepts it)
    - everything else -> rpyc.classic.obtain deep copy
"""

from __future__ import annotations

import os
import threading
from types import SimpleNamespace
from typing import Any

import rpyc

_HOST = os.environ.get("MT5_RPYC_HOST", "127.0.0.1")
_PORT = int(os.environ.get("MT5_RPYC_PORT", "8001"))

_lock = threading.Lock()
_conn: rpyc.Connection | None = None
_remote = None
_const_cache: dict[str, Any] = {}


class _Result(SimpleNamespace):
    """Attribute access + namedtuple-style ._asdict()."""

    def _asdict(self) -> dict:
        return {k: (v._asdict() if isinstance(v, _Result) else v)
                for k, v in self.__dict__.items()}


def _get_remote():
    global _conn, _remote
    with _lock:
        if _remote is None:
            _conn = rpyc.classic.connect(_HOST, _PORT)
            _conn._config["sync_request_timeout"] = 120
            _remote = _conn.modules["MetaTrader5"]
    return _remote


def _convert(obj: Any) -> Any:
    """Recursively convert a remote result into plain local objects."""
    if obj is None or isinstance(obj, (bool, int, float, str, bytes)):
        return obj
    # namedtuple-like (TradePosition, AccountInfo, OrderSendResult, ...)
    if hasattr(obj, "_asdict"):
        d = rpyc.classic.obtain(_remote_asdict(obj))
        return _Result(**{k: _convert(v) for k, v in d.items()})
    if isinstance(obj, (list, tuple)) or type(obj).__name__ in ("list", "tuple"):
        return [_convert(x) for x in obj]
    try:
        return rpyc.classic.obtain(obj)
    except Exception:
        return obj


def _remote_asdict(obj: Any) -> Any:
    """_asdict() may itself contain nested namedtuples (e.g. request in
    OrderSendResult) that don't pickle locally — flatten them remotely."""
    d = obj._asdict()
    out = {}
    for k in d:
        v = d[k]
        out[k] = _remote_asdict(v) if hasattr(v, "_asdict") else v
    return out


_RATES_FUNCS = {
    "copy_rates_from", "copy_rates_from_pos", "copy_rates_range",
    "copy_ticks_from", "copy_ticks_range",
}


def _call(name: str, *args: Any, **kwargs: Any) -> Any:
    remote = _get_remote()
    fn = getattr(remote, name)
    res = fn(*args, **kwargs)
    if res is None:
        return None
    if name in _RATES_FUNCS:
        # structured numpy array -> list[dict]; avoids cross-version
        # numpy pickle issues and feeds pandas.DataFrame directly.
        names = rpyc.classic.obtain(res.dtype.names)
        rows = rpyc.classic.obtain(res.tolist())
        return [dict(zip(names, row)) for row in rows]
    if name in ("positions_get", "orders_get", "history_orders_get",
                "history_deals_get", "symbols_get"):
        return tuple(_convert(x) for x in res)
    return _convert(res)


def __getattr__(name: str) -> Any:  # PEP 562 module-level forwarding
    if name.startswith("__"):
        raise AttributeError(name)
    if name in _const_cache:
        return _const_cache[name]
    remote = _get_remote()
    attr = getattr(remote, name)
    if callable(attr):
        def _wrapper(*args: Any, __name: str = name, **kwargs: Any) -> Any:
            return _call(__name, *args, **kwargs)
        _wrapper.__name__ = name
        _const_cache[name] = _wrapper
        return _wrapper
    value = rpyc.classic.obtain(attr)
    _const_cache[name] = value
    return value
