"""A small, bounded async TTL cache.

Its only job is to absorb repeated identical calls within one agent turn (an LLM
often asks the same question two or three ways). It is not correctness-critical:
entries expire, and a stampede of concurrent callers for the same missing key
results in exactly one upstream call.

Bounded on two axes so it is safe in a long-running daemon:

* every write first sweeps expired entries (and their locks), and
* if the live set still exceeds ``max_entries`` the oldest-expiring entries are
  dropped.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Hashable
from typing import TypeVar

T = TypeVar("T")

_DEFAULT_MAX_ENTRIES = 512


class TTLCache:
    """Time-boxed memoisation keyed by an arbitrary hashable.

    A ``ttl_seconds`` of ``0`` (or less) disables caching entirely — every call
    goes straight to the factory.
    """

    def __init__(self, ttl_seconds: float, *, max_entries: int = _DEFAULT_MAX_ENTRIES) -> None:
        self._ttl = ttl_seconds
        self._max_entries = max(1, max_entries)
        self._entries: dict[Hashable, tuple[float, object]] = {}
        self._locks: dict[Hashable, asyncio.Lock] = {}
        self._guard = asyncio.Lock()

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def __len__(self) -> int:
        return len(self._entries)

    def _fresh(self, key: Hashable) -> tuple[bool, object]:
        entry = self._entries.get(key)
        if entry is None:
            return False, None
        expiry, value = entry
        if time.monotonic() >= expiry:
            self._entries.pop(key, None)
            self._locks.pop(key, None)
            return False, None
        return True, value

    def _evict(self) -> None:
        """Drop expired entries; then, if still over budget, the oldest-expiring."""
        now = time.monotonic()
        for key in [k for k, (exp, _) in self._entries.items() if now >= exp]:
            self._entries.pop(key, None)
            self._locks.pop(key, None)
        if len(self._entries) > self._max_entries:
            ordered = sorted(self._entries.items(), key=lambda kv: kv[1][0])
            for key, _ in ordered[: len(self._entries) - self._max_entries]:
                self._entries.pop(key, None)
                self._locks.pop(key, None)

    async def get_or_call(self, key: Hashable, factory: Callable[[], Awaitable[T]]) -> T:
        """Return the cached value for *key*, or await *factory* and cache it."""
        if self._ttl <= 0:
            return await factory()

        hit, value = self._fresh(key)
        if hit:
            return value  # type: ignore[return-value]

        async with self._guard:
            lock = self._locks.setdefault(key, asyncio.Lock())

        async with lock:
            hit, value = self._fresh(key)
            if hit:
                return value  # type: ignore[return-value]
            result = await factory()
            self._entries[key] = (time.monotonic() + self._ttl, result)
            self._evict()
            return result

    def clear(self) -> None:
        self._entries.clear()
        self._locks.clear()
