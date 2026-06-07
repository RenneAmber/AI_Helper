import json
from datetime import UTC, datetime

from redis.asyncio import Redis

from .config import settings

redis_client = Redis.from_url(settings.redis_url, decode_responses=True)


async def update_session_state(session_id: str, payload: dict) -> None:
    state = {
        **payload,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    await redis_client.set(f"session:{session_id}", json.dumps(state), ex=60 * 60 * 24)


async def append_memory(user_id: str, role: str, content: str) -> None:
    key = f"memory:{user_id}"
    item = json.dumps({"role": role, "content": content}, ensure_ascii=False)
    await redis_client.lpush(key, item)
    await redis_client.ltrim(key, 0, settings.memory_max_items - 1)
    await redis_client.expire(key, 60 * 60 * 24 * 7)
