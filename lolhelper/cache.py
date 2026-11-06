"""In-memory TTL cache with request coalescing and a small JSON file store."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Awaitable, Callable


class TTLCache:
    """LRU + TTL cache. get_or_load() merges concurrent loads of one key."""

    def __init__(self, max_items: int = 512):
        self.max_items = max_items
        self._data: OrderedDict[str, tuple[float, float, Any]] = OrderedDict()
        self._inflight: dict[str, asyncio.Future] = {}

    def get(self, key: str) -> tuple[Any, float] | None:
        item = self._data.get(key)
        if not item:
            return None
        expires, created, value = item
        if expires < time.time():
            self._data.pop(key, None)
            return None
        self._data.move_to_end(key)
        return value, created

    def set(self, key: str, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        now = time.time()
        self._data[key] = (now + ttl, now, value)
        self._data.move_to_end(key)
        while len(self._data) > self.max_items:
            self._data.popitem(last=False)

    def clear(self, prefix: str = "") -> int:
        keys = [k for k in self._data if k.startswith(prefix)]
        for k in keys:
            self._data.pop(k, None)
        return len(keys)

    async def get_or_load(
        self, key: str, ttl: float, loader: Callable[[], Awaitable[Any]]
    ) -> tuple[Any, float]:
        """Return (value, created_at). created_at tells how fresh the data is."""
        hit = self.get(key)
        if hit is not None:
            return hit
        fut = self._inflight.get(key)
        if fut is not None:
            return await asyncio.shield(fut)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._inflight[key] = fut
        try:
            value = await loader()
            created = time.time()
            self.set(key, value, ttl)
            fut.set_result((value, created))
            return value, created
        except BaseException as exc:  # propagate to waiters too
            fut.set_exception(exc)
            # mark retrieved so asyncio does not warn when nobody waits
            fut.exception()
            raise
        finally:
            self._inflight.pop(key, None)


class JsonStore:
    """Atomic JSON file persistence used for game data and plugin state."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    def load_sync(self, default: Any = None) -> Any:
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except (OSError, json.JSONDecodeError):
            return default

    def save_sync(self, data: Any) -> None:
        fd, tmp = tempfile.mkstemp(prefix=self.path.name, dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    async def load(self, default: Any = None) -> Any:
        return await asyncio.to_thread(self.load_sync, default)

    async def save(self, data: Any) -> None:
        async with self._lock:
            await asyncio.to_thread(self.save_sync, data)

