from __future__ import annotations

import asyncio

import pytest

from app.reliability import CircuitBreakerOpen, Reliability


@pytest.mark.asyncio
async def test_retry_then_success():
    state = {"n": 0}

    async def flaky():
        state["n"] += 1
        if state["n"] < 2:
            raise RuntimeError("boom")
        return "ok"

    r = Reliability(name="t1", retry_max=3, base_delay_ms=1, timeout_ms=1000)
    assert await r.call(flaky) == "ok"
    assert state["n"] == 2


@pytest.mark.asyncio
async def test_fallback_invoked_on_failure():
    async def always_fail():
        raise RuntimeError("nope")

    async def fb(exc: Exception):
        return "fallback"

    r = Reliability(name="t2", retry_max=2, base_delay_ms=1, timeout_ms=1000)
    assert await r.call(always_fail, fallback=fb) == "fallback"


@pytest.mark.asyncio
async def test_timeout_triggers_fallback():
    async def slow():
        await asyncio.sleep(2)
        return "late"

    async def fb(exc: Exception):
        return "timeout-fallback"

    r = Reliability(name="t3", retry_max=1, base_delay_ms=1, timeout_ms=50)
    assert await r.call(slow, fallback=fb) == "timeout-fallback"


@pytest.mark.asyncio
async def test_circuit_breaker_opens():
    async def fail():
        raise RuntimeError("x")

    r = Reliability(name="t4", retry_max=1, base_delay_ms=1, timeout_ms=200)
    r.breaker.failure_threshold = 2

    for _ in range(2):
        with pytest.raises(Exception):
            await r.call(fail)

    with pytest.raises(CircuitBreakerOpen):
        await r.call(fail)
