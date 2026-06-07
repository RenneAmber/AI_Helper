"""
分布式工作流队列与状态存储。

- 优先使用 Redis（生产/多副本场景）：状态 Hash + 任务 List
- 未配置 REDIS_URL 时退化为进程内 asyncio.Queue + dict（开发模式）
- 提供 enqueue / dequeue / set_state / get_state 四个原语
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from .config import settings
from .logging_setup import get_logger

logger = get_logger("workflow.queue")


class _MemoryBackend:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.state: dict[str, dict[str, Any]] = {}

    async def enqueue(self, payload: dict[str, Any]) -> None:
        await self.queue.put(json.dumps(payload))

    async def dequeue(self) -> dict[str, Any]:
        raw = await self.queue.get()
        return json.loads(raw)

    async def set_state(self, workflow_id: str, state: dict[str, Any]) -> None:
        self.state[workflow_id] = state

    async def get_state(self, workflow_id: str) -> dict[str, Any] | None:
        return self.state.get(workflow_id)


class _RedisBackend:
    def __init__(self, url: str) -> None:
        import redis.asyncio as redis
        self._redis = redis.from_url(url, decode_responses=True)
        self._queue_key = settings.workflow_queue_key
        self._state_prefix = settings.workflow_state_prefix

    async def enqueue(self, payload: dict[str, Any]) -> None:
        await self._redis.lpush(self._queue_key, json.dumps(payload))

    async def dequeue(self) -> dict[str, Any]:
        # BRPOP 阻塞拉取，避免空转
        _, raw = await self._redis.brpop(self._queue_key, timeout=0)
        return json.loads(raw)

    async def set_state(self, workflow_id: str, state: dict[str, Any]) -> None:
        await self._redis.set(self._state_prefix + workflow_id, json.dumps(state))

    async def get_state(self, workflow_id: str) -> dict[str, Any] | None:
        raw = await self._redis.get(self._state_prefix + workflow_id)
        return json.loads(raw) if raw else None


def _build_backend():
    if settings.redis_url:
        try:
            backend = _RedisBackend(settings.redis_url)
            logger.info("workflow_queue_redis", extra={"url": settings.redis_url})
            return backend
        except Exception as exc:
            logger.warning("workflow_queue_redis_failed_fallback", extra={"error": str(exc)})
    logger.info("workflow_queue_memory")
    return _MemoryBackend()


backend = _build_backend()
