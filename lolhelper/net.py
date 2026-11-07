"""Shared aiohttp session with proxy support, rate limiting and retries."""

from __future__ import annotations

import asyncio
import random
import ssl
import time
from typing import Any

import aiohttp

from .log import default_logger
from . import VERSION
from .errors import UpstreamError

DEFAULT_UA = f"astrbot_plugin_lolhelper/{VERSION} (+https://github.com/horizoe10/astrbot_plugin_lolhelper)"


def make_ssl_context() -> ssl.SSLContext:
    """Prefer certifi's CA bundle (shipped with AstrBot) over the OS store."""
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


class RateLimiter:
    """Concurrency limit plus a minimum interval between request starts."""

    def __init__(self, concurrency: int = 4, min_interval: float = 0.0):
        self._sem = asyncio.Semaphore(max(1, concurrency))
        self._min_interval = max(0.0, min_interval)
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def __aenter__(self):
        await self._sem.acquire()
        if self._min_interval:
            async with self._lock:
                wait = self._last + self._min_interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
        return self

    async def __aexit__(self, *exc):
        self._sem.release()
        return False


class HttpClient:
    def __init__(
        self,
        proxy: str | None = None,
        timeout: float = 20,
        logger: Any = None,
        user_agent: str = DEFAULT_UA,
    ):
        self.proxy = proxy or None
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.logger = logger or default_logger
        self.user_agent = user_agent
        self._session: aiohttp.ClientSession | None = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self.timeout,
                headers={"User-Agent": self.user_agent},
                connector=aiohttp.TCPConnector(ssl=make_ssl_context(), limit=32),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        retries: int = 2,
        use_proxy: bool = True,
        **kwargs: Any,
    ) -> Any:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                async with self.session.request(
                    method, url, proxy=self.proxy if use_proxy else None, **kwargs
                ) as resp:
                    if resp.status in (429, 500, 502, 503, 504) and attempt < retries:
                        await asyncio.sleep(_backoff(attempt, resp.headers.get("Retry-After")))
                        continue
                    if resp.status >= 400:
                        body = (await resp.text())[:300]
                        raise UpstreamError(f"HTTP {resp.status}: {body}")
                    return await resp.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt < retries:
                    await asyncio.sleep(_backoff(attempt))
                    continue
        raise UpstreamError(f"网络请求失败：{type(last_exc).__name__} {last_exc}")

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        return await self.request_json("GET", url, **kwargs)


def _backoff(attempt: int, retry_after: str | None = None) -> float:
    if retry_after:
        try:
            return min(10.0, float(retry_after))
        except ValueError:
            pass
    return min(8.0, 0.6 * (2**attempt)) + random.random() * 0.3

