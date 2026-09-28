"""
utils.timing
============

Decorator / context manager for measuring async/sync call latency.
"""
from __future__ import annotations

import asyncio
import functools
import time
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Callable, Iterator, TypeVar

T = TypeVar("T")


@contextmanager
def measure_sync(label: str) -> Iterator[float]:
    """Context manager that yields elapsed seconds on exit (sync)."""
    start = time.perf_counter()
    try:
        yield 0.0
    finally:
        elapsed = time.perf_counter() - start
        # The yielded value cannot be updated in-place; consumer should use return value
        # of the context manager's __exit__ alternative. We expose via attribute below.
        measure_sync.last_elapsed = elapsed  # type: ignore[attr-defined]


@asynccontextmanager
async def measure_async(label: str) -> AsyncIterator[float]:
    """Async context manager that yields elapsed seconds on exit."""
    start = time.perf_counter()
    try:
        yield 0.0
    finally:
        elapsed = time.perf_counter() - start
        measure_async.last_elapsed = elapsed  # type: ignore[attr-defined]


def timed(func: Callable[..., T]) -> Callable[..., T]:
    """Decorator measuring sync function latency; stores on ``func.last_latency``."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            wrapper.last_latency = time.perf_counter() - start  # type: ignore[attr-defined]

    wrapper.last_latency = 0.0  # type: ignore[attr-defined]
    return wrapper


def timed_async(func: Callable[..., "asyncio.Future[T]"]) -> Callable[..., "asyncio.Future[T]"]:
    """Decorator measuring async function latency."""

    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        start = time.perf_counter()
        try:
            return await func(*args, **kwargs)
        finally:
            wrapper.last_latency = time.perf_counter() - start  # type: ignore[attr-defined]

    wrapper.last_latency = 0.0  # type: ignore[attr-defined]
    return wrapper
