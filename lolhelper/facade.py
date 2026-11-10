"""Single entry point used by chat commands, the HTTP API, LLM tools and push.

The facade owns all clients and services. AstrBot specifics (image rendering
through Star.html_render, sending messages) are injected as callables so this
module stays importable without AstrBot.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from .log import default_logger
from .args import Target
from .cn import CnBridgeClient, CnService
from .constants import POSITIONS, resolve_cn_server, set_league_posters
from .errors import BridgeError, UsageError
from .gamedata import GameData
from .lolesports import LolEsportsLogos, match_team
from .mcp_client import OpggMcpClient
from .net import HttpClient, RateLimiter, image_type
from .render import render_markdown, render_text
from .render.art import ArtProcessor
from .render.image import ImageRenderer
from .service import OpggService, ServiceConfig, View
from .settings import Settings
from .esports import EsportsClient
from .opgg_web import OpggWebClient
from .wegame import WeGameClient
from .storage import StateStore, sub_league_list
from .tft import TftService
from .fmt import set_default_tz


class Facade:
    def __init__(self, settings: Settings, data_dir: Path, html_render=None, logger: Any = None):
        self.s = settings
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger or default_logger
        set_default_tz(settings.timezone)
        set_league_posters(settings.esports_posters)
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
                meta_region=settings.meta_region,
                meta_tier=settings.meta_tier,
            ),
            self.logger,
        )
        self.svc.web = OpggWebClient(self.http)
        self.tft = TftService(self.svc)
        self.svc.esp = EsportsClient(self.http)
        self.bridge = CnBridgeClient(self.http, settings.cn_bridge_url, settings.cn_bridge_token, settings.cn_timeout, logger=self.logger)
        self.wegame = WeGameClient(self.http, settings.cn_wegame_cookie, settings.cn_timeout, self.logger)
        self.cn = CnService(self.bridge, self.svc, self.logger, self.wegame)
        self.store = StateStore(self.data_dir / "state.json")
        self.les = LolEsportsLogos(self.http, self.data_dir / "lolesports.json", self.logger)
        self.art = ArtProcessor(self.data_dir / "art_cache", self._fetch_icon, self._team_logos,
                                inline=settings.image_inline_icons, logger=self.logger)
        self.images = ImageRenderer(
            self.data_dir / "render",
            html_render=html_render,
            strategy=settings.image_renderer,
            quality=settings.image_quality,
            max_rows=settings.image_max_rows,
            logger=self.logger,
            fetch=self._fetch_icon if settings.image_inline_icons else None,
            art=self.art,
        )

    async def _fetch_icon(self, url: str) -> tuple[bytes, str] | None:
        import aiohttp

        try:
            async with self.http.session.get(url, proxy=self.http.proxy, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return None
                ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if ctype.startswith("image/"):
                    return await resp.read(), ctype
                if ctype not in ("", "binary/octet-stream", "application/octet-stream"):
                    return None
                data = await resp.read()  # static.lolesports.com sends some logos as octet-stream
                kind = image_type(data)
                return (data, kind) if kind else None
        except Exception:
            return None

    async def _team_logos(self) -> dict[str, str]:
        """OP.GG team logo URL -> the logo shown on image cards: the team's LoL Esports logo when it is ranked
        there, else OP.GG's dark-mode logo."""
        async def known() -> dict[str, dict]:
            return await self.svc.esp.team_logos() if self.svc.esp else {}

        teams, ranked = await asyncio.gather(known(), self.les.teams())
        out = {}
        for url, t in teams.items():
            hit = match_team(ranked, t.get("acronym"), t.get("name")) if ranked else None
            pick = (hit or {}).get("image") or t.get("dark")
            if pick and pick != url:
                out[url] = pick
        return out

    async def start(self) -> None:
        await self.store.load()
        self.les.refresh()  # LoL Esports team logos, loaded in the background
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
        for task in (self.les._task, self.svc.esp._logos_task if self.svc.esp else None):
            if task is not None and not task.done():  # background logo loads
                task.cancel()
        await self.http.close()

    # ------------------------------------------------------------------ queries
    def cn_server(self, token: str | None) -> str | None:
        return resolve_cn_server(token or "", self.s.cn_servers_extra) if token else None

    async def account_from_target(self, tgt: Target) -> dict:
        if tgt.is_cn:
            if not self.s.cn_enabled:
                raise UsageError("国服战绩查询未开启，可在插件配置「国服 → 启用国服战绩查询」中开启（开启前请阅读风险提示）")
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
                raise UsageError("国服战绩查询未开启，可在插件配置「国服 → 启用国服战绩查询」中开启（开启前请阅读风险提示）")
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
                raise UsageError("国服战绩查询未开启，可在插件配置「国服 → 启用国服战绩查询」中开启（开启前请阅读风险提示）")
            return await self.cn.game_detail(account, index)
        return await self.svc.game_detail(account["game_name"], account["tag_line"], account.get("region"), index)

    async def champion(self, query: str, position: str | None = None, mode: str | None = None,
                       region: str | None = None, tier: str | None = None) -> View:
        return await self.svc.champion(query, position, mode, region, tier)

    async def tier(self, position: str | None = None, region: str | None = None, tier: str | None = None) -> View:
        if position and position not in POSITIONS:
            raise UsageError("位置可选：上单 / 打野 / 中单 / 下路 / 辅助")
        return await self.svc.tier(position, limit=max(self.s.text_max_rows, self.s.image_max_rows), region=region, tier=tier)

    async def esports(self, mode: str = "schedule", league: str | list[str] | None = None) -> View:
        return await self.svc.esports(mode, league, limit=max(self.s.text_max_rows, 12))

    def sub_leagues(self, umo: str | None) -> list[str] | None:
        """Default leagues for a session: its esports subscriptions, else the configured defaults; None = all."""
        subs = self.store.subs(umo=umo, kind="esports") if umo else []
        if not subs:
            return list(self.s.esports_default_leagues) or None
        leagues: list[str] = []
        for s in subs:
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
        return await self.cn.status_view(self.store.state("wegame") or {})

    async def refresh(self) -> None:
        self.svc.cache.clear()
        await self.gd.ensure_lol(force=True)
        await self.gd.ensure_tft(self.mcp, force=True)

    # ---------------------------------------------------------------- AI briefs
    async def _game_for(self, account: dict, index: int) -> tuple[dict, dict]:
        """(normalized 10-player game, match list entry) for the index-th recent game of an account."""
        from .game import normalize_opgg_game

        if account.get("kind") == "cn":
            if not self.s.cn_enabled:
                raise UsageError("国服战绩查询未开启，可在插件配置「国服 → 启用国服战绩查询」中开启（开启前请阅读风险提示）")
            matches = await self.cn.recent_matches(account, max(index, 5))
            if not 1 <= index <= len(matches):
                raise UsageError(f"只找到最近 {len(matches)} 场对局，序号需要在 1~{len(matches)} 之间")
            m = matches[index - 1]
            return await self.cn.match_detail(account, m), m
        await self.gd.ensure_lol()
        matches, _ = await self.svc.recent_matches(account["game_name"], account["tag_line"], account["region"], max(index, 5))
        if not 1 <= index <= len(matches):
            raise UsageError(f"只找到最近 {len(matches)} 场对局，序号需要在 1~{len(matches)} 之间")
        m = matches[index - 1]
        raw, _ = await self.svc._game_raw(account["region"], m, account["game_name"], account["tag_line"])
        game = normalize_opgg_game(raw, self.gd)
        game["map"] = game.get("map") or m.get("map", "")
        return game, m

    async def _champ_meta(self, target: dict, opp: dict | None) -> dict:
        """OP.GG reference for the reviewed champion: tier, win rate, core build, the lane matchup."""
        pos = target.get("position")
        if not target.get("champion_id") or pos not in POSITIONS:
            return {}
        champ = self.gd.champion(target["champion_id"])
        if not champ:
            return {}
        try:
            view = await self.svc.champion(champ["key"], pos)
        except Exception as exc:
            self.logger.info(f"[lolhelper] AI 点评获取英雄参考数据失败：{exc}")
            return {}
        d = (view.data or {}).get("analysis") or {}
        stats = (d.get("summary") or {}).get("average_stats") or {}
        core = d.get("core_items") or {}
        boots = d.get("boots") or {}
        meta = {
            "tier": stats.get("tier"), "win_rate": stats.get("win_rate"),
            "core": [self.gd.item_name(i) for i in core.get("ids") or []],
            "core_wr": core["win"] / core["play"] if core.get("play") else None,
            "boots": self.gd.item_name((boots.get("ids") or [None])[0]) if boots.get("ids") else "",
        }
        if opp and opp.get("champion_id"):
            for key_name, label in (("weak_counters", "难打"), ("strong_counters", "好打")):
                for c in d.get(key_name) or []:
                    if str(c.get("champion_id")) == str(opp["champion_id"]):
                        wr = c.get("my_win_rate")
                        meta["matchup"] = f"对线 {opp.get('champion_name', '').split(' ')[0]} 属于{label}对局，本英雄胜率 {wr * 100:.1f}%" if wr else ""
        return meta

    async def _cn_timeline(self, account: dict, game: dict, m: dict) -> dict:
        from .ai import summarize_timeline

        if game.get("source") not in ("sgp", "lcu", "list") or self.bridge is None:
            return {}
        target = next((p for t in game.get("teams") or [] for p in t["players"] if p.get("is_target")), None)
        side = next((t.get("key") for t in game.get("teams") or [] if target in t["players"]), "BLUE")
        try:
            payload = await self.bridge.timeline(m["id"], account.get("server") or "")
            raw = (payload or {}).get("data") or {}
            puuid = await self.cn._puuid(account)
            return summarize_timeline(raw, side or "BLUE", puuid)
        except Exception as exc:
            self.logger.info(f"[lolhelper] 获取国服时间线失败（点评改用终局数据）：{exc}")
            return {}

    async def ai_game_brief(self, account: dict, index: int = 1, game: dict | None = None, m: dict | None = None) -> tuple[str, str, dict]:
        """(cache key, brief, game) for one game of an account."""
        from .ai import brief_game

        if game is None or m is None:
            game, m = await self._game_for(account, index)
        players = [p for t in game.get("teams") or [] for p in t["players"]]
        target = next((p for p in players if p.get("is_target")), None)
        if not target or len(players) < 2:
            raise UsageError("这局缺少双方玩家数据，无法点评")
        my = next(t for t in game["teams"] if target in t["players"])
        opp = next((p for t in game["teams"] if t is not my for p in t["players"]
                    if p.get("position") and p.get("position") == target.get("position")), None)
        meta = await self._champ_meta(target, opp) if game.get("queue") in ("单双排", "灵活排位", "匹配") or "排" in str(game.get("queue")) else {}
        timeline = await self._cn_timeline(account, game, m) if account.get("kind") == "cn" else {}
        who = f"{account.get('game_name')}#{account.get('tag_line')}"
        return f"{who}:{m.get('id')}", brief_game(game, self.gd, meta, timeline), game

    async def ai_recent_brief(self, account: dict, count: int) -> tuple[str, str, list[dict], dict]:
        from .ai import brief_recent, recent_rows, recent_stats

        if account.get("kind") == "cn":
            if not self.s.cn_enabled:
                raise UsageError("国服战绩查询未开启，可在插件配置「国服 → 启用国服战绩查询」中开启（开启前请阅读风险提示）")
            matches = await self.cn.recent_matches(account, count)
            games = [m.get("_game") or {} for m in matches]
        else:
            await self.gd.ensure_lol()
            matches, _ = await self.svc.recent_matches(account["game_name"], account["tag_line"], account["region"], count)
            games = await self.svc.games_for(matches, account["game_name"], account["tag_line"], account["region"])
        sr = [x for x in games if x.get("map") == "召唤师峡谷" or (not x.get("map") and "排" in str(x.get("queue") or ""))]
        games = sr or games
        rows = recent_rows(games)
        if len(rows) < 3:
            raise UsageError(f"最近只找到 {len(rows)} 局有效对局，至少需要 3 局才能诊断")
        st = recent_stats(rows)
        who = f"{account.get('game_name')}#{account.get('tag_line')}"
        key = f"{who}:{'-'.join(str(m.get('id')) for m in matches[:len(rows)])}"
        return key, brief_recent(who, rows, st), rows, st

    async def ai_esports(self, leagues: list[str] | None, index: int | None, team: str | None,
                         match_id: str | None, game_no: int | None) -> tuple[dict, list[dict], float]:
        g = await self.svc.esports_find(leagues, index, team, match_id)
        await self.gd.ensure_lol()
        games, created = await self.svc.esports_series_games(g)
        if not games:
            raise UsageError("这场比赛还没有每局数据")
        return g, games, created

    @staticmethod
    def _match_title(g: dict) -> str:
        from .constants import stage_zh
        from .service import league_info

        info = league_info(g.get("league"))
        stage = stage_zh(str(g.get("name") or "").split(":")[0].strip())
        return " ".join(x for x in [info.get("full") or str(g.get("league") or ""), stage] if x)

    def ai_esports_game_brief(self, g: dict, game: dict) -> tuple[str, str]:
        from .ai import brief_esports_game
        from .esports import normalize_esports_game

        n = normalize_esports_game(game, self.gd)
        home, away = g.get("homeTeam") or {}, g.get("awayTeam") or {}
        header = f"{self._match_title(g)}：{home.get('acronym')} vs {away.get('acronym')}（BO{g.get('numberOfGames') or '?'}，当前大比分 {g.get('homeScore')}:{g.get('awayScore')}），第{n.get('set')}局"
        return f"{g.get('id')}:{n.get('set')}", brief_esports_game(n, g, header, self.gd)

    def ai_series_brief(self, g: dict, games: list[dict]) -> tuple[str, str]:
        from .ai import brief_series
        from .esports import normalize_esports_game, pick_mvp

        ns = [normalize_esports_game(x, self.gd) for x in games]
        home, away = g.get("homeTeam") or {}, g.get("awayTeam") or {}
        header = f"{self._match_title(g)}：{home.get('acronym')} {g.get('homeScore')}:{g.get('awayScore')} {away.get('acronym')}（BO{g.get('numberOfGames') or '?'}）"
        mvp_of = lambda n: pick_mvp(next((t["players"] for t in n["teams"] if t.get("win")), []))
        key = f"{g.get('id')}:series:{len(ns)}:{g.get('homeScore')}-{g.get('awayScore')}"
        return key, brief_series(g, ns, header, mvp_of)


    # ---------------------------------------------------------------- rendering
    def text(self, view: View) -> str:
        return render_text(self._tips(view.doc), self.s.text_max_rows)

    def markdown(self, view: View) -> str:
        return render_markdown(self._tips(view.doc), self.s.text_max_rows)

    async def image(self, view: View, umo: str | None = None, want_file: bool = False) -> str:
        return await self.images.render(self._tips(view.doc), umo, want_file)

    def _tips(self, doc):
        if not self.s.show_tips:
            doc.tips = []
        return doc

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

