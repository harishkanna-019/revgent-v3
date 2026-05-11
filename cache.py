"""Async TTL cache with thundering-herd prevention."""

import asyncio
import time
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")


class AsyncTTLCache:
    """In-memory cache with TTL expiration and per-key locking.

    Lock-free reads: if the key exists and is not expired, return immediately.
    Lock-guarded writes: if the key is missing or expired, acquire a per-key lock
    so that only one coroutine computes the value while others wait.
    """

    def __init__(self, ttl_seconds: float = 86400):
        """Args:
            ttl_seconds: Time-to-live in seconds (default 24h)
        """
        self._ttl = ttl_seconds
        self._data: dict[str, tuple[Any, float]] = {}  # key -> (value, expiry_timestamp)
        self._locks: dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()

    def _get_lock(self, key: str) -> asyncio.Lock:
        """Get or create a per-key lock."""
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def _is_expired(self, expiry: float) -> bool:
        return time.monotonic() > expiry

    def get(self, key: str) -> Any | None:
        """Lock-free read. Returns None if missing or expired."""
        if key not in self._data:
            return None
        value, expiry = self._data[key]
        if self._is_expired(expiry):
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        """Set a value with TTL."""
        expiry = time.monotonic() + self._ttl
        self._data[key] = (value, expiry)

    async def get_or_compute(
        self,
        key: str,
        compute: Callable[[], Awaitable[T]],
    ) -> T:
        """Get a cached value or compute it, preventing thundering herd.

        If the key is missing or expired, acquires a per-key lock so that
        only one coroutine calls `compute()` while concurrent waiters
        block on the lock and get the result when it completes.
        """
        # Fast path: lock-free read
        cached = self.get(key)
        if cached is not None:
            return cached

        # Slow path: acquire per-key lock, then re-check
        lock = self._get_lock(key)
        async with lock:
            # Re-check after acquiring lock — another coroutine may have computed it
            cached = self.get(key)
            if cached is not None:
                return cached

            # Compute and cache
            value = await compute()
            self.set(key, value)
            return value

    def invalidate(self, key: str) -> None:
        """Remove a key from the cache."""
        self._data.pop(key, None)

    def clear(self) -> None:
        """Clear all cached values."""
        self._data.clear()
