"""Single entry point used by chat commands, the HTTP API, LLM tools and push.

The facade owns all clients and services. AstrBot specifics (image rendering
through Star.html_render, sending messages) are injected as callables so this
module stays importable without AstrBot.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .args import Target
from .cn import CnBridgeClient, CnService
from .constants import POSITIONS, resolve_cn_server
from .errors import BridgeError, UsageError
from .gamedata import GameData
from .mcp_client import OpggMcpClient
from .net import HttpClient, RateLimiter
from .render import render_markdown, render_text
from .render.image import ImageRenderer
from .service import OpggService, ServiceConfig, View
from .settings import Settings
from .esports import EsportsClient
from .storage import StateStore, sub_league_list
from .tft import TftService
from .fmt import set_default_tz


class Facade:
    def __init__(self, settings: Settings, data_dir: Path, html_render=None, logger: logging.Logger | None = None):
        self.s = settings
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger or logging.getLogger("lolhelper")
        set_default_tz(settings.timezone)
        self.http = HttpClient(proxy=settings.proxy or None, timeout=settings.timeout, logger=self.logger)
        self.mcp = OpggMcpClient(
            self.http,
            settings.mcp_endpoint,
            RateLimiter(settings.concurrency, settings.min_interval),
            settings.tool_overrides,
            self.logger,
        )
        self.gd = GameData(
            self.http,
            self.data_dir,
            settings.ddragon_base,
            settings.cdragon_base,
            settings.gamedata_refresh_hours,
            settings.champion_aliases,
            self.logger,
        )
        self.svc = OpggService(
            self.mcp,
            self.gd,
            ServiceConfig(
                lang=settings.lang,
                timezone=settings.timezone,
                default_region=settings.default_region,
                match_count=settings.match_count,
                match_details=settings.match_details,
                ttl_meta=settings.ttl_meta,
                ttl_build=settings.ttl_build,
                ttl_profile=settings.ttl_profile,
                ttl_matches=settings.ttl_matches,
                ttl_esports=settings.ttl_esports,
                ttl_tft=settings.ttl_tft,
            ),
            self.logger,
        )
        self.tft = TftService(self.svc)
        self.svc.esp = EsportsClient(self.http)
        self.bridge = CnBridgeClient(self.http, settings.cn_bridge_url, settings.cn_bridge_token, settings.cn_timeout)
        self.cn = CnService(self.bridge, self.svc, self.logger)
        self.store = StateStore(self.data_dir / "state.json")
        self.images = ImageRenderer(
            self.data_dir / "render",
            html_render=html_render,
            strategy=settings.image_renderer,
            theme=settings.image_theme,
            quality=settings.image_quality,
            max_rows=settings.image_max_rows,
            logger=self.logger,
            fetch=self._fetch_icon if settings.image_inline_icons else None,
        )

    async def _fetch_icon(self, url: str) -> tuple[bytes, str] | None:
        import aiohttp

        try:
            async with self.http.session.get(url, proxy=self.http.proxy, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return None
                ctype = resp.headers.get("Content-Type", "image/png").split(";")[0]
                if not ctype.startswith("image/"):
                    return None
                return await resp.read(), ctype
        except Exception:
            return None

    async def start(self) -> None:
        await self.store.load()
        try:
            await self.mcp.list_tools()
            self.logger.info(f"[lolhelper] OP.GG MCP 已连接，可用工具 {len(self.mcp.tools)} 个")
            expected = [
                "lol_get_champion_analysis", "lol_list_lane_meta_champions", "lol_get_summoner_profile",
                "lol_list_summoner_matches", "lol_esports_list_schedules", "lol_esports_list_team_standings",
                "tft_list_meta_decks", "tft_get_champion_item_build", "tft_list_champions_for_item",
                "tft_list_augments", "tft_list_item_combinations",
            ]
            missing = [t for t in expected if self.mcp.tool_name(t) not in self.mcp.tools]
            if missing:
                self.logger.warning(f"[lolhelper] OP.GG MCP 缺少工具 {missing}，可在 data_source.tool_overrides 中映射新名称")
        except Exception as exc:
            self.logger.warning(f"[lolhelper] OP.GG MCP 预检失败（首次查询时会重试）：{exc}")
        try:
            await self.gd.ensure_lol()
        except Exception as exc:
            self.logger.warning(f"[lolhelper] Data Dragon 预加载失败：{exc}")

    async def close(self) -> None:
        await self.http.close()

    # ------------------------------------------------------------------ queries
    def cn_server(self, token: str | None) -> str | None:
        return resolve_cn_server(token or "", self.s.cn_servers_extra) if token else None

    async def account_from_target(self, tgt: Target) -> dict:
        if tgt.is_cn:
            if not self.s.cn_enabled:
                raise UsageError("国服查询未启用（插件配置 cn.enabled）")
            server = tgt.cn_server or self.s.cn_default_server or None
            return await self.cn.resolve_account(tgt.game_name, tgt.tag_line, server)
        return {
            "kind": "global",
            "region": tgt.region or self.s.default_region,
            "game_name": tgt.game_name,
            "tag_line": tgt.tag_line,
        }

    async def summoner(self, account: dict, count: int | None = None, detail: bool = False) -> View:
        if account.get("kind") == "cn":
            if not self.s.cn_enabled:
                raise UsageError("国服查询未启用（插件配置 cn.enabled）")
            return await self.cn.summoner(account, count, detail)
        return await self.svc.summoner(account["game_name"], account["tag_line"], account.get("region"), count, detail)

    async def latest_matches(self, account: dict, count: int = 3) -> list[dict]:
        if account.get("kind") == "cn":
            return await self.cn.recent_matches(account, count)
        await self.gd.ensure_lol()
        matches, _ = await self.svc.recent_matches(account["game_name"], account["tag_line"], account["region"], count, ttl=30)
        return matches

    async def game_detail(self, account: dict, index: int = 1) -> View:
        if account.get("kind") == "cn":
            if not self.s.cn_enabled:
                raise UsageError("国服查询未启用（插件配置 cn.enabled）")
            return await self.cn.game_detail(account, index)
        return await self.svc.game_detail(account["game_name"], account["tag_line"], account.get("region"), index)

    async def champion(self, query: str, position: str | None = None, mode: str | None = None) -> View:
        return await self.svc.champion(query, position, mode)

    async def tier(self, position: str | None = None) -> View:
        if position and position not in POSITIONS:
            raise UsageError("位置可选：上单 / 打野 / 中单 / 下路 / 辅助")
        return await self.svc.tier(position, limit=max(self.s.text_max_rows, self.s.image_max_rows))

    async def esports(self, mode: str = "schedule", league: str | list[str] | None = None) -> View:
        return await self.svc.esports(mode, league, limit=max(self.s.text_max_rows, 12))

    def sub_leagues(self, umo: str | None) -> list[str] | None:
        """Leagues this session subscribed to; None when not subscribed or subscribed to all."""
        if not umo:
            return None
        leagues: list[str] = []
        for s in self.store.subs(umo=umo, kind="esports"):
            got = sub_league_list(s)
            if not got:
                return None
            leagues += [x for x in got if x not in leagues]
        return leagues or None

    async def esports_match(self, leagues: list[str] | None = None, index: int | None = None, team: str | None = None,
                            match_id: str | None = None, game_no: int | None = None) -> View:
        g = await self.svc.esports_find(leagues, index, team, match_id)
        key = str(index) if index else (team.upper() if team else (str(match_id) if match_id else ""))
        return await self.svc.esports_match(g, game_no, key=key)

    async def standings(self, league: str) -> View:
        return await self.svc.standings(league)

    async def cn_status(self) -> View:
        if not self.bridge.configured:
            raise BridgeError("未配置国服本地代理地址（插件配置 cn.bridge_url）")
        return await self.cn.status_view()

    async def refresh(self) -> None:
        self.svc.cache.clear()
        await self.gd.ensure_lol(force=True)
        await self.gd.ensure_tft(self.mcp, force=True)

    # ---------------------------------------------------------------- rendering
    def text(self, view: View) -> str:
        return render_text(view.doc, self.s.text_max_rows, self.s.show_footer)

    def markdown(self, view: View) -> str:
        return render_markdown(view.doc, self.s.text_max_rows, self.s.show_footer)

    async def image(self, view: View, umo: str | None = None, want_file: bool = False) -> str:
        return await self.images.render(view.doc, umo, want_file)

    def resolve_mode(self, requested: str | None, umo: str | None, platform: str | None) -> str:
        mode = requested or (self.store.mode(umo) if umo else None) or self.s.default_mode
        if mode == "markdown" and platform and platform not in self.s.markdown_platforms:
            mode = "text"
        return mode

    def view_payload(self, view: View) -> dict[str, Any]:
        return {
            "view": view.kind,
            "created_at": view.created_at,
            "data": view.data,
            "doc": view.doc.to_dict(),
            "text": self.text(view),
            "markdown": self.markdown(view),
        }

