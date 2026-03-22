"""Helpers for selecting MT5 order filling modes."""

from __future__ import annotations

from typing import Any

# MT5 order filling values used in trade requests.
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2

DEFAULT_FILLING_MODES: list[int] = [
    ORDER_FILLING_IOC,
    ORDER_FILLING_FOK,
    ORDER_FILLING_RETURN,
]


def preferred_filling_modes(symbol_info: dict[str, Any] | None) -> list[int]:
    """Return filling mode candidates ordered by probability of success."""
    if not symbol_info:
        return list(DEFAULT_FILLING_MODES)

    candidates: list[int] = []

    raw = symbol_info.get("filling_mode")
    if isinstance(raw, int):
        if raw in {ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN}:
            candidates.append(raw)
        else:
            if raw & 2:
                candidates.append(ORDER_FILLING_IOC)
            if raw & 1:
                candidates.append(ORDER_FILLING_FOK)
            if raw & 4:
                candidates.append(ORDER_FILLING_RETURN)

    candidates.extend(DEFAULT_FILLING_MODES)

    deduped: list[int] = []
    for mode in candidates:
        if mode not in deduped:
            deduped.append(mode)
    return deduped
