from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from typing import Any

from .config import settings


class TTLCache:
    """Thread-safe LRU + TTL cache for inference responses."""

    def __init__(self, max_items: int, ttl_seconds: int) -> None:
        self._max = max_items
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        self._store: "OrderedDict[str, tuple[float, Any]]" = OrderedDict()

    def _is_expired(self, ts: float) -> bool:
        return (time.time() - ts) > self._ttl

    def get(self, key: str) -> Any | None:
        with self._lock:
            item = self._store.get(key)
            if item is None:
                return None
            ts, value = item
            if self._is_expired(ts):
                self._store.pop(key, None)
                return None
            self._store.move_to_end(key)
            return value

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self._store[key] = (time.time(), value)
            self._store.move_to_end(key)
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    def size(self) -> int:
        with self._lock:
            return len(self._store)


def make_key(prompt: str, user_id: str) -> str:
    h = hashlib.sha256()
    h.update(prompt.encode("utf-8"))
    h.update(b"|")
    h.update(user_id.encode("utf-8"))
    return h.hexdigest()


response_cache = TTLCache(
    max_items=settings.cache_max_items,
    ttl_seconds=settings.cache_ttl_seconds,
)
