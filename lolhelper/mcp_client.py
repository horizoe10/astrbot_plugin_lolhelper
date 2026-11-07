"""Minimal MCP Streamable HTTP client for the OP.GG MCP server.

Only the parts we need are implemented: initialize, tools/list and tools/call.
The official endpoint is https://mcp-api.op.gg/mcp (no auth). Responses may be
plain JSON or an SSE stream; both are handled.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any

import aiohttp

from .log import default_logger
from . import VERSION
from .dsl import parse_opgg_text
from .errors import NotFoundError, UpstreamError
from .net import HttpClient, RateLimiter, _backoff

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "astrbot_plugin_lolhelper", "version": VERSION}

NOT_FOUND_HINTS = ("not found", "does not exist", "no data", "존재하지")


class OpggMcpClient:
    def __init__(
        self,
        http: HttpClient,
        endpoint: str = "https://mcp-api.op.gg/mcp",
        limiter: RateLimiter | None = None,
        tool_overrides: dict[str, str] | None = None,
        logger: Any = None,
    ):
        self.http = http
        self.endpoint = endpoint.rstrip("/") if endpoint else "https://mcp-api.op.gg/mcp"
        self.limiter = limiter or RateLimiter(4, 0.15)
        self.tool_overrides = tool_overrides or {}
        self.logger = logger or default_logger
        self._sid: str | None = None
        self._protocol = PROTOCOL_VERSION
        self._init_lock = asyncio.Lock()
        self._ids = itertools.count(1)
        self.tools: dict[str, dict] = {}

    # ------------------------------------------------------------------ protocol
    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self._protocol,
        }
        if self._sid:
            h["Mcp-Session-Id"] = self._sid
        return h

    async def _post(self, payload: dict) -> tuple[int, dict | None]:
        async with self.http.session.post(
            self.endpoint,
            data=json.dumps(payload),
            headers=self._headers(),
            proxy=self.http.proxy,
        ) as resp:
            sid = resp.headers.get("Mcp-Session-Id")
            if sid:
                self._sid = sid
            if "id" not in payload:  # notification
                return resp.status, None
            raw = await resp.text()
            if resp.status >= 400 and not raw.strip().startswith(("{", "data:", "event:")):
                return resp.status, {"error": {"code": resp.status, "message": raw[:300]}}
            ctype = resp.headers.get("Content-Type", "")
            return resp.status, _decode_body(raw, ctype, payload.get("id"))

    async def _initialize(self) -> None:
        self._sid = None
        status, body = await self._post(
            {
                "jsonrpc": "2.0",
                "id": next(self._ids),
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": CLIENT_INFO,
                },
            }
        )
        if not body or "result" not in body:
            raise UpstreamError(f"OP.GG MCP 初始化失败 (HTTP {status})")
        self._protocol = body["result"].get("protocolVersion") or PROTOCOL_VERSION
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def ensure(self) -> None:
        if self._sid:
            return
        async with self._init_lock:
            if not self._sid:
                await self._initialize()

    async def _rpc(self, method: str, params: dict, retries: int = 2) -> Any:
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                await self.ensure()
                async with self.limiter:
                    status, body = await self._post(
                        {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_err = UpstreamError(f"连接 OP.GG 失败：{type(exc).__name__}")
                await asyncio.sleep(_backoff(attempt))
                continue
            if status in (400, 404) and body and _looks_like_session_error(body):
                self._sid = None  # session expired -> re-initialize
                last_err = UpstreamError("OP.GG 会话过期")
                continue
            if status == 429 or status >= 500:
                last_err = UpstreamError(f"OP.GG 繁忙 (HTTP {status})")
                await asyncio.sleep(_backoff(attempt))
                continue
            if body is None:
                raise UpstreamError(f"OP.GG 返回空响应 (HTTP {status})")
            if "error" in body:
                msg = str(body["error"].get("message") or body["error"])
                if any(h in msg.lower() for h in NOT_FOUND_HINTS):
                    raise NotFoundError(_humanize(msg))
                raise UpstreamError(f"OP.GG 错误：{_humanize(msg)}")
            return body.get("result")
        raise last_err or UpstreamError("OP.GG 请求失败")

    # ---------------------------------------------------------------- public api
    async def list_tools(self) -> dict[str, dict]:
        result = await self._rpc("tools/list", {})
        self.tools = {t["name"]: t for t in (result or {}).get("tools", [])}
        return self.tools

    def tool_name(self, name: str) -> str:
        return self.tool_overrides.get(name, name)

    async def call(self, tool: str, arguments: dict | None = None) -> Any:
        name = self.tool_name(tool)
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if result is None:
            raise UpstreamError("OP.GG 没有返回数据")
        texts = [c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"]
        if result.get("isError"):
            msg = " ".join(texts) or "unknown error"
            if any(h in msg.lower() for h in NOT_FOUND_HINTS):
                raise NotFoundError(_humanize(msg))
            raise UpstreamError(f"OP.GG 错误：{_humanize(msg)}")
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        joined = "\n".join(t for t in texts if t)
        try:
            return parse_opgg_text(joined)
        except ValueError as exc:
            self.logger.warning(f"[lolhelper] 解析 {name} 返回失败：{exc}; 原文前 200 字：{joined[:200]}")
            raise UpstreamError("OP.GG 返回了无法解析的数据") from exc


def _decode_body(raw: str, ctype: str, want_id: Any) -> dict | None:
    raw = raw.strip()
    if not raw:
        return None
    if "text/event-stream" in ctype or raw.startswith(("event:", "data:")):
        messages: list[dict] = []
        buf: list[str] = []
        for line in raw.splitlines() + [""]:
            if line.startswith("data:"):
                buf.append(line[5:].strip())
            elif not line.strip() and buf:
                try:
                    messages.append(json.loads("".join(buf)))
                except json.JSONDecodeError:
                    pass
                buf = []
        for msg in messages:
            if msg.get("id") == want_id:
                return msg
        return messages[-1] if messages else None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"error": {"code": -1, "message": raw[:300]}}


def _looks_like_session_error(body: dict) -> bool:
    msg = json.dumps(body.get("error", ""), ensure_ascii=False).lower()
    return "session" in msg


def _humanize(msg: str) -> str:
    msg = msg.strip()
    try:
        data = json.loads(msg)
        if isinstance(data, dict):
            parts = []
            for k, v in data.items():
                parts.append(f"{k}: {'; '.join(v) if isinstance(v, list) else v}")
            msg = " | ".join(parts)
    except (json.JSONDecodeError, TypeError):
        pass
    mapping = {
        "Summoner not found": "找不到该召唤师，请检查名字#标签和大区",
        "The selected position is invalid.": "该模式不支持指定位置",
    }
    for en, zh in mapping.items():
        msg = msg.replace(en, zh)
    return msg[:200]

