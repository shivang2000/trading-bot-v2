"""Async retry decorator with exponential backoff."""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def async_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable[[F], F]:
    """Decorator that retries an async function with exponential backoff."""

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exception: Exception | None = None
            delay = base_delay

            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt == max_retries:
                        break
                    logger.warning(
                        "%s failed (attempt %d/%d): %s. Retrying in %.1fs...",
                        func.__qualname__,
                        attempt,
                        max_retries,
                        e,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, max_delay)

            raise last_exception  # type: ignore[misc]

        return wrapper  # type: ignore[return-value]

    return decorator
