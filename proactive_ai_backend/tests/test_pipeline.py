from __future__ import annotations

import asyncio

import pytest

from app.cache import TTLCache, make_key
from app.batcher import Batcher


def test_cache_key_is_stable():
    a = make_key("hello", "u1")
    b = make_key("hello", "u1")
    assert a == b


def test_cache_set_get():
    cache = TTLCache(max_items=3, ttl_seconds=10)
    cache.set("k", "v")
    assert cache.get("k") == "v"


def test_cache_evicts_lru():
    cache = TTLCache(max_items=2, ttl_seconds=10)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3


@pytest.mark.asyncio
async def test_batcher_merges_calls():
    seen_batches: list[int] = []

    async def processor(items: list[int]) -> list[int]:
        seen_batches.append(len(items))
        return [x * 2 for x in items]

    batcher = Batcher(processor, max_size=4, window_ms=20)
    results = await asyncio.gather(*[batcher.submit(i) for i in range(4)])
    assert sorted(results) == [0, 2, 4, 6]
    assert sum(seen_batches) == 4
