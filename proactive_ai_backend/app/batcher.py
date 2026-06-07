from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Generic, TypeVar

from .config import settings

T = TypeVar("T")
R = TypeVar("R")


@dataclass
class _Pending(Generic[T, R]):
    item: T
    future: asyncio.Future[R]


class Batcher(Generic[T, R]):
    """Time + size based request batcher to improve throughput.

    The processor receives a list and must return a list of the same length.
    """

    def __init__(
        self,
        processor: Callable[[list[T]], Awaitable[list[R]]],
        max_size: int = settings.batch_max_size,
        window_ms: int = settings.batch_window_ms,
    ) -> None:
        self._processor = processor
        self._max_size = max_size
        self._window = window_ms / 1000.0
        self._queue: list[_Pending[T, R]] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None

    async def submit(self, item: T) -> R:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[R] = loop.create_future()
        async with self._lock:
            self._queue.append(_Pending(item=item, future=future))
            if len(self._queue) >= self._max_size:
                await self._flush_locked()
            elif self._flush_task is None or self._flush_task.done():
                self._flush_task = asyncio.create_task(self._schedule_flush())
        return await future

    async def _schedule_flush(self) -> None:
        await asyncio.sleep(self._window)
        async with self._lock:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        if not self._queue:
            return
        batch = self._queue
        self._queue = []
        items = [p.item for p in batch]
        try:
            results = await self._processor(items)
            for pending, result in zip(batch, results, strict=True):
                if not pending.future.done():
                    pending.future.set_result(result)
        except Exception as exc:  # propagate to every awaiter
            for pending in batch:
                if not pending.future.done():
                    pending.future.set_exception(exc)
