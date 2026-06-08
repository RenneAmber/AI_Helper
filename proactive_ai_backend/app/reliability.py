from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, TypeVar

from .config import settings
from .logging_setup import get_logger
from .metrics import (
    circuit_breaker_state,
    metrics,
    reliability_call_attempts,
    reliability_calls_total,
)

T = TypeVar("T")

logger = get_logger("reliability")


class CircuitBreakerOpen(Exception):
    pass


@dataclass
class CircuitBreaker:
    failure_threshold: int = settings.circuit_failure_threshold
    reset_seconds: int = settings.circuit_reset_seconds
    failures: int = 0
    opened_at: float = 0.0

    def allow(self) -> bool:
        if self.failures < self.failure_threshold:
            return True
        if (time.time() - self.opened_at) > self.reset_seconds:
            # half-open
            self.failures = self.failure_threshold - 1
            return True
        return False

    def on_success(self) -> None:
        self.failures = 0
        self.opened_at = 0.0

    def on_failure(self) -> None:
        self.failures += 1
        if self.failures == self.failure_threshold:
            self.opened_at = time.time()


class Reliability:
    """Bundle of retry + timeout + circuit breaker + fallback."""

    def __init__(
        self,
        name: str,
        retry_max: int = settings.retry_max_attempts,
        base_delay_ms: int = settings.retry_base_delay_ms,
        timeout_ms: int = settings.request_timeout_ms,
    ) -> None:
        self.name = name
        self.retry_max = retry_max
        self.base_delay = base_delay_ms / 1000.0
        self.timeout = timeout_ms / 1000.0
        self.breaker = CircuitBreaker()

    async def call(
        self,
        fn: Callable[[], Awaitable[T]],
        fallback: Callable[[Exception], Awaitable[T]] | None = None,
    ) -> T:
        if not self.breaker.allow():
            metrics.inc(f"breaker.open.{self.name}")
            circuit_breaker_state.labels(self.name).set(2)  # OPEN
            if fallback is not None:
                reliability_calls_total.labels(self.name, "fallback").inc()
                return await fallback(CircuitBreakerOpen(self.name))
            reliability_calls_total.labels(self.name, "failure").inc()
            raise CircuitBreakerOpen(self.name)

        attempt = 0
        last_exc: Exception | None = None
        while attempt < self.retry_max:
            attempt += 1
            try:
                async with _timeout(self.timeout):
                    result = await fn()
                self.breaker.on_success()
                circuit_breaker_state.labels(self.name).set(0)  # CLOSED
                metrics.inc(f"call.success.{self.name}")
                metrics.observe(f"call.attempts.{self.name}", attempt)
                reliability_calls_total.labels(self.name, "success").inc()
                reliability_call_attempts.labels(self.name).observe(attempt)
                return result
            except Exception as exc:
                last_exc = exc
                self.breaker.on_failure()
                # half-open 探测阶段也在这里表现为 1 才会恢复
                if self.breaker.failures >= self.breaker.failure_threshold:
                    circuit_breaker_state.labels(self.name).set(2)
                metrics.inc(f"call.failure.{self.name}")
                logger.warning(
                    "call_failed",
                    extra={"op": self.name, "attempt": attempt, "error": str(exc)},
                )
                await asyncio.sleep(self.base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.05))

        if fallback is not None and last_exc is not None:
            reliability_calls_total.labels(self.name, "fallback").inc()
            reliability_call_attempts.labels(self.name).observe(attempt)
            return await fallback(last_exc)
        assert last_exc is not None
        reliability_calls_total.labels(self.name, "failure").inc()
        reliability_call_attempts.labels(self.name).observe(attempt)
        raise last_exc


class _timeout:
    """Async context manager wrapping asyncio.timeout for 3.10+ compatibility."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._cm = None

    async def __aenter__(self):
        # asyncio.timeout exists in 3.11+; fall back to wait_for-style timeout if missing.
        self._cm = asyncio.timeout(self.seconds)
        return await self._cm.__aenter__()

    async def __aexit__(self, exc_type, exc, tb):
        return await self._cm.__aexit__(exc_type, exc, tb)
