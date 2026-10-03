"""Tiny async TTL cache — keeps us polite to Fantrax and the NHL API."""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable


class TTLCache:
    def __init__(self) -> None:
        self._store: dict[Any, tuple[float, Any]] = {}

    async def get(self, key: Any, ttl: float, fetch: Callable[[], Awaitable[Any]]) -> Any:
        hit = self._store.get(key)
        now = time.monotonic()
        if hit and now - hit[0] < ttl:
            return hit[1]
        value = await fetch()
        self._store[key] = (now, value)
        return value

    def clear(self) -> None:
        self._store.clear()
