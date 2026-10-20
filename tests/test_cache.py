from __future__ import annotations

import asyncio

import pytest

from homelab_mcp.cache import TTLCache


async def test_hit_avoids_second_call() -> None:
    cache = TTLCache(ttl_seconds=60)
    calls = 0

    async def factory() -> int:
        nonlocal calls
        calls += 1
        return 42

    assert await cache.get_or_call("k", factory) == 42
    assert await cache.get_or_call("k", factory) == 42
    assert calls == 1


async def test_expiry_triggers_recall(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = TTLCache(ttl_seconds=10)
    now = [1000.0]
    monkeypatch.setattr("homelab_mcp.cache.time.monotonic", lambda: now[0])
    calls = 0

    async def factory() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await cache.get_or_call("k", factory) == 1
    now[0] += 5
    assert await cache.get_or_call("k", factory) == 1
    now[0] += 6  # now 11s elapsed, entry expired
    assert await cache.get_or_call("k", factory) == 2
    assert calls == 2


async def test_zero_ttl_disables_caching() -> None:
    cache = TTLCache(ttl_seconds=0)
    calls = 0

    async def factory() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await cache.get_or_call("k", factory) == 1
    assert await cache.get_or_call("k", factory) == 2


async def test_bounded_entries_and_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = TTLCache(ttl_seconds=100, max_entries=4)
    now = [1000.0]
    monkeypatch.setattr("homelab_mcp.cache.time.monotonic", lambda: now[0])

    async def factory() -> int:
        return 1

    for i in range(20):
        await cache.get_or_call(("k", i), factory)
    assert len(cache) <= 4
    assert len(cache._locks) <= 4  # locks are pruned alongside entries


async def test_expired_entries_and_locks_are_swept_on_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = TTLCache(ttl_seconds=10, max_entries=100)
    now = [1000.0]
    monkeypatch.setattr("homelab_mcp.cache.time.monotonic", lambda: now[0])

    async def factory() -> int:
        return 1

    await cache.get_or_call("old", factory)
    now[0] += 100  # "old" is now stale
    await cache.get_or_call("new", factory)
    assert "old" not in cache._entries
    assert "old" not in cache._locks


async def test_concurrent_miss_calls_factory_once() -> None:
    cache = TTLCache(ttl_seconds=60)
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return "value"

    results = await asyncio.gather(*(cache.get_or_call("k", factory) for _ in range(8)))
    assert results == ["value"] * 8
    assert calls == 1
