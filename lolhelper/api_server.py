"""Local HTTP API so other programs can reuse the plugin's data and renderers.

Default bind is 127.0.0.1:6197. A token is required whenever the server listens
on a non-loopback address. Every GET endpoint accepts format=json|text|markdown|html|image.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web

from .args import parse_leagues, parse_target
from .constants import POSITION_ALIASES, resolve_league
from .errors import BridgeError, NotFoundError, LolHelperError, UpstreamError, UsageError
from .render.html import render_html
from .service import View

PushFn = Callable[[str, View, str | None], Awaitable[None]]

STATUS = {UsageError: 400, NotFoundError: 404, BridgeError: 502, UpstreamError: 502}


def _is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ApiServer:
    def __init__(self, facade, push: PushFn | None = None, logger: logging.Logger | None = None):
        self.f = facade
        self.s = facade.s
        self.push = push
        self.logger = logger or logging.getLogger("lolhelper")
        self._runner: web.AppRunner | None = None

    # ------------------------------------------------------------------ server
    async def start(self) -> bool:
        if not _is_loopback(self.s.api_host) and not self.s.api_token:
            self.logger.error("[lolhelper] 本地接口监听非本机地址时必须设置 local_api.token，已拒绝启动")
            return False
        app = web.Application(middlewares=[self._auth])
        app.router.add_get("/health", self.health)
        app.router.add_get("/api/v1/{path:.*}", self.get_view)
        app.router.add_post("/api/v1/mcp/call", self.mcp_call)
        app.router.add_post("/api/v1/push", self.push_view)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.s.api_host, self.s.api_port)
        await site.start()
        self.logger.info(f"[lolhelper] 本地接口已启动 http://{self.s.api_host}:{self.s.api_port}")
        return True

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    @web.middleware
    async def _auth(self, request: web.Request, handler):
        if request.path != "/health" and self.s.api_token:
            got = request.headers.get("Authorization", "")
            got = got[7:] if got.lower().startswith("bearer ") else request.query.get("token", "")
            if not hmac.compare_digest(got.strip(), self.s.api_token):
                return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            return await handler(request)
        except LolHelperError as exc:
            status = next((v for k, v in STATUS.items() if isinstance(exc, k)), 500)
            return web.json_response({"ok": False, "error": str(exc)}, status=status, dumps=_dumps)

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "version": "1.0", "tools": len(self.f.mcp.tools)})

    # ------------------------------------------------------------------- views
    async def build_view(self, path: str, q: dict) -> View:
        path = path.strip("/")
        f = self.f

        def pos(v):
            if not v:
                return None
            p = POSITION_ALIASES.get(str(v).lower())
            if not p:
                raise UsageError(f"未知位置 {v}")
            return p

        def count(v, default=None):
            return int(v) if v not in (None, "") else default

        if path == "lol/champion":
            return await f.champion(q.get("name", ""), pos(q.get("position")), q.get("mode") or None)
        if path == "lol/tier":
            return await f.tier(pos(q.get("position")))
        if path in ("lol/summoner", "lol/matches"):
            words = [q.get("riot_id", "")] + ([q["region"]] if q.get("region") else [])
            tgt = parse_target(words, f.s.cn_servers_extra)
            acc = await f.account_from_target(tgt)
            return await f.summoner(acc, count(q.get("count")), q.get("detail") in ("1", "true", "yes"))
        if path == "lol/game":
            words = [q.get("riot_id", "")] + ([q["region"]] if q.get("region") else [])
            acc = await f.account_from_target(parse_target(words, f.s.cn_servers_extra))
            return await f.game_detail(acc, count(q.get("index"), 1))
        if path == "lol/esports":
            mode = "result" if q.get("mode") == "result" else "schedule"
            leagues, _ = parse_leagues(q.get("league", "").replace(",", " ").split())
            return await f.esports(mode, leagues or None)
        if path == "lol/esports/match":
            leagues, _ = parse_leagues(q.get("league", "").replace(",", " ").split())
            return await f.esports_match(
                leagues or None, count(q.get("index"), 0) or None, q.get("team") or None,
                q.get("match_id") or None, count(q.get("game"), 0) or None,
            )
        if path == "lol/standings":
            league = resolve_league(q.get("league", "")) or q.get("league", "")
            if not league:
                raise UsageError("缺少 league 参数")
            return await f.standings(league)
        if path == "tft/meta":
            return await f.tft.meta(count(q.get("limit"), 10))
        if path == "tft/deck":
            return await f.tft.deck(count(q.get("index"), 1))
        if path == "tft/champion":
            return await f.tft.champion(q.get("name", ""))
        if path == "tft/item":
            return await f.tft.item(q.get("name", ""))
        if path == "tft/augment":
            return await f.tft.augment(q.get("q", ""))
        if path == "cn/status":
            return await f.cn_status()
        raise NotFoundError(f"未知接口 /api/v1/{path}")

    async def respond(self, view: View, fmt: str) -> web.StreamResponse:
        fmt = (fmt or "json").lower()
        if fmt in ("text", "txt"):
            return web.Response(text=self.f.text(view), content_type="text/plain", charset="utf-8")
        if fmt in ("markdown", "md"):
            return web.Response(text=self.f.markdown(view), content_type="text/markdown", charset="utf-8")
        if fmt == "html":
            html = render_html(view.doc, self.s.image_theme, self.s.image_max_rows)
            return web.Response(text=html, content_type="text/html", charset="utf-8")
        if fmt in ("image", "img", "jpg", "png"):
            ref = await self.f.image(view, want_file=True)
            p = Path(ref)
            if p.is_file():
                ctype = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
                return web.Response(body=p.read_bytes(), content_type=ctype)
            raise web.HTTPFound(ref)
        return web.json_response({"ok": True, **self.f.view_payload(view)}, dumps=_dumps)

    async def get_view(self, request: web.Request) -> web.StreamResponse:
        q = dict(request.query)
        view = await self.build_view(request.match_info["path"], q)
        return await self.respond(view, q.get("format", "json"))

    async def mcp_call(self, request: web.Request) -> web.Response:
        if not self.s.api_allow_mcp:
            return web.json_response({"ok": False, "error": "已禁用 MCP 透传（local_api.allow_mcp_passthrough）"}, status=403)
        body = await request.json()
        tool = str(body.get("tool") or "")
        if not tool.startswith(("lol_", "tft_")):
            return web.json_response({"ok": False, "error": "只允许 lol_* / tft_* 工具"}, status=400)
        data = await self.f.mcp.call(tool, body.get("arguments") or {})
        return web.json_response({"ok": True, "data": data}, dumps=_dumps)

    async def push_view(self, request: web.Request) -> web.Response:
        if not self.s.api_allow_push or not self.push:
            return web.json_response({"ok": False, "error": "已禁用推送接口（local_api.allow_push）"}, status=403)
        body = await request.json()
        umo = str(body.get("umo") or "")
        if not umo:
            raise UsageError("缺少 umo")
        if body.get("text"):
            from .doc import Doc

            view = View("custom", {}, Doc(title=str(body.get("title") or "通知")).add_text("", str(body["text"])))
        else:
            view = await self.build_view(str(body.get("path") or ""), dict(body.get("params") or {}))
        await self.push(umo, view, body.get("format"))
        return web.json_response({"ok": True})


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)

