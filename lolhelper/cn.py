"""China (Tencent) server support.

Built-in mode: bridge/cn_bridge.py is loaded in-process and talks to the League
client on this machine. Remote mode: the same script runs on the gaming PC and
the plugin calls it over HTTP (address may carry the token: http://ip:6198?token=xxx).
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

import aiohttp

from .constants import POSITION_FROM_API, QUEUE_ID_ZH, cn_server_name
from .doc import Cell, Doc
from .errors import BridgeError, NotFoundError
from .fmt import freshness, pct, tier_emblem, tier_text
from .gamedata import GameData
from .net import HttpClient
from .service import OpggService, View, rank_tile


BRIDGE_FILE = Path(__file__).resolve().parent.parent / "bridge" / "cn_bridge.py"
REMOTE_HINT = "若 AstrBot 不在登录国服客户端的电脑上，请在那台电脑运行 bridge/start_bridge.bat，并把它显示的地址填到插件配置「远程国服代理」"


def parse_remote(url: str, token: str = "") -> tuple[str, str]:
    """Split "http://host:6198?token=xxx" (or "#xxx") into base URL and token."""
    url = (url or "").strip()
    if not url:
        return "", token or ""
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    tok = (parse_qs(parts.query).get("token") or [""])[0] or parts.fragment or token or ""
    base = urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    return base, tok


class CnBridgeClient:
    def __init__(self, http: HttpClient, url: str = "", token: str = "", timeout: float = 15,
                 bridge_file: Path | None = None, logger: logging.Logger | None = None):
        self.http = http
        self.url, self.token = parse_remote(url, token)
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.request_timeout = timeout
        self.bridge_file = Path(bridge_file or BRIDGE_FILE)
        self.logger = logger or logging.getLogger("lolhelper")
        self._mod = None
        self._bridge = None

    @property
    def configured(self) -> bool:
        return True

    @property
    def mode(self) -> str:
        return "remote" if self.url else "builtin"

    @property
    def label(self) -> str:
        return self.url or "内置（本机客户端）"

    def _local(self):
        if self._bridge is None:
            if not self.bridge_file.exists():
                raise BridgeError(f"缺少国服代理脚本 {self.bridge_file}")
            spec = importlib.util.spec_from_file_location("lolhelper_cn_bridge", self.bridge_file)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cfg = dict(mod.DEFAULT_CONFIG)
            cfg["request_timeout"] = self.request_timeout
            self._mod, self._bridge = mod, mod.Bridge(cfg)
        return self._mod, self._bridge

    async def _get_local(self, path: str, params: dict[str, str]) -> Any:
        mod, bridge = self._local()
        name = mod.ROUTES.get(path)
        if not name:
            raise BridgeError(f"国服代理不支持 {path}")
        try:
            return await asyncio.to_thread(getattr(bridge, name), params)
        except mod.BridgeError as exc:
            if getattr(exc, "status", 0) == 404:
                raise NotFoundError(str(exc)) from exc
            msg = str(exc)
            if getattr(exc, "code", "") == "client_not_found":
                msg = f"{msg}。{REMOTE_HINT}"
            raise BridgeError(msg) from exc

    async def get(self, path: str, **params: Any) -> Any:
        clean = {k: str(v) for k, v in params.items() if v not in (None, "")}
        if not self.url:
            return await self._get_local(path, clean)
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            async with self.http.session.get(
                f"{self.url}{path}", params=clean, headers=headers, timeout=self.timeout, proxy=None
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except Exception:
                    body = {"ok": False, "error": (await resp.text())[:200]}
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise BridgeError(f"连接远程国服代理失败：{type(exc).__name__}（{self.url}）") from exc
        if resp.status == 401:
            raise BridgeError("远程国服代理 Token 错误，请重新复制代理窗口里显示的完整地址（含 ?token=）")
        if not isinstance(body, dict) or not body.get("ok"):
            msg = (body or {}).get("error") if isinstance(body, dict) else str(body)
            if resp.status == 404:
                raise NotFoundError(msg or "未找到")
            raise BridgeError(msg or f"远程国服代理返回 HTTP {resp.status}")
        return body.get("data")

    async def status(self) -> dict:
        return await self.get("/v1/status")

    async def lookup(self, name: str, tag: str) -> dict:
        return await self.get("/v1/summoner", name=name, tag=tag)

    async def summoner(self, puuid: str, server: str) -> dict:
        return await self.get("/v1/summoner", puuid=puuid, server=server)

    async def ranked(self, puuid: str, server: str) -> dict:
        return await self.get("/v1/ranked", puuid=puuid, server=server)

    async def matches(self, puuid: str, server: str, start: int = 0, count: int = 10) -> dict:
        return await self.get("/v1/matches", puuid=puuid, server=server, start=start, count=count)

    async def game(self, game_id: str, server: str) -> dict:
        return await self.get("/v1/game", game_id=game_id, server=server)


# ------------------------------------------------------------------ normalizers
def _participant_common(p: dict, gd: GameData) -> dict:
    champ = gd.champion(p.get("championId"))
    return {
        "champion_id": p.get("championId"),
        "champion_name": gd.display_name(champ).split(" ")[0] if champ else str(p.get("championId")),
    }


def normalize_sgp_games(raw: dict | None, puuid: str, gd: GameData) -> list[dict]:
    out = []
    for g in (raw or {}).get("games") or []:
        j = g.get("json") or {}
        me = next((p for p in j.get("participants") or [] if p.get("puuid") == puuid), None)
        if not me:
            continue
        items = [me.get(f"item{i}") for i in range(7) if me.get(f"item{i}")]
        team_kills = sum(int(p.get("kills") or 0) for p in j.get("participants") or [] if p.get("teamId") == me.get("teamId"))
        remake = bool(me.get("gameEndedInEarlySurrender"))
        out.append({
            **_participant_common(me, gd),
            "id": str(j.get("gameId")),
            "created_at": j.get("gameCreation") or j.get("gameStartTimestamp"),
            "queue_id": j.get("queueId"),
            "queue": QUEUE_ID_ZH.get(int(j.get("queueId") or -1), f"模式{j.get('queueId')}"),
            "duration": j.get("gameDuration"),
            "position": POSITION_FROM_API.get(str(me.get("teamPosition") or me.get("individualPosition") or "").upper()),
            "items": items,
            "kills": me.get("kills"),
            "deaths": me.get("deaths"),
            "assists": me.get("assists"),
            "cs": int(me.get("totalMinionsKilled") or 0) + int(me.get("neutralMinionsKilled") or 0),
            "result": "REMAKE" if remake else ("WIN" if me.get("win") else "LOSE"),
            "score": None,
            "score_rank": None,
            "damage": me.get("totalDamageDealtToChampions"),
            "kill_participation": (int(me.get("kills") or 0) + int(me.get("assists") or 0)) / team_kills if team_kills else None,
            "placement": me.get("placement") or me.get("subteamPlacement") or None,
        })
    return out


def normalize_lcu_games(raw: dict | None, puuid: str, gd: GameData) -> list[dict]:
    games = ((raw or {}).get("games") or {}).get("games") or []
    out = []
    for g in games:
        pid = None
        for ident in g.get("participantIdentities") or []:
            if (ident.get("player") or {}).get("puuid") == puuid:
                pid = ident.get("participantId")
        me = next((p for p in g.get("participants") or [] if p.get("participantId") == pid), None)
        if not me and len(g.get("participants") or []) == 1:
            me = g["participants"][0]
        if not me:
            continue
        st = me.get("stats") or {}
        tl = me.get("timeline") or {}
        out.append({
            **_participant_common(me, gd),
            "id": str(g.get("gameId")),
            "created_at": g.get("gameCreation"),
            "queue_id": g.get("queueId"),
            "queue": QUEUE_ID_ZH.get(int(g.get("queueId") or -1), f"模式{g.get('queueId')}"),
            "duration": g.get("gameDuration"),
            "position": POSITION_FROM_API.get(str(tl.get("lane") or "").upper()),
            "items": [st.get(f"item{i}") for i in range(7) if st.get(f"item{i}")],
            "kills": st.get("kills"),
            "deaths": st.get("deaths"),
            "assists": st.get("assists"),
            "cs": int(st.get("totalMinionsKilled") or 0) + int(st.get("neutralMinionsKilled") or 0),
            "result": "REMAKE" if st.get("gameEndedInEarlySurrender") else ("WIN" if st.get("win") else "LOSE"),
            "score": None,
            "score_rank": None,
            "damage": st.get("totalDamageDealtToChampions"),
        })
    return out


def normalize_ranked(payload: dict | None) -> list[dict]:
    data = (payload or {}).get("data") or {}
    queues = data.get("queues") or list((data.get("queueMap") or {}).values())
    out = []
    for q in queues:
        tier = q.get("tier")
        if not tier or str(tier).upper() in ("NONE", "UNRANKED", ""):
            continue
        out.append({
            "queue": q.get("queueType"),
            "tier": str(tier).upper(),
            "division": q.get("division") or q.get("rank"),
            "lp": q.get("leaguePoints"),
            "wins": int(q.get("wins") or 0),
            "losses": int(q.get("losses") or 0),
        })
    order = {"RANKED_SOLO_5x5": 0, "RANKED_FLEX_SR": 1}
    return sorted(out, key=lambda r: order.get(r["queue"], 9))


QUEUE_NAMES = {
    "RANKED_SOLO_5x5": "单双排",
    "RANKED_FLEX_SR": "灵活组排",
    "RANKED_TFT": "云顶之弈",
    "RANKED_TFT_DOUBLE_UP": "云顶双人",
    "RANKED_TFT_TURBO": "云顶狂暴",
    "CHERRY": "斗魂竞技场",
}


class CnService:
    def __init__(self, bridge: CnBridgeClient, svc: OpggService, logger: logging.Logger | None = None):
        self.bridge = bridge
        self.svc = svc
        self.gd = svc.gd
        self.logger = logger or logging.getLogger("lolhelper")

    async def resolve_account(self, name: str, tag: str, server: str | None) -> dict:
        hit = await self.bridge.lookup(name, tag)
        if not server:
            st = await self.bridge.status()
            server = st.get("server") or ""
        return {
            "kind": "cn",
            "server": server,
            "puuid": hit.get("puuid"),
            "game_name": hit.get("gameName") or name,
            "tag_line": hit.get("tagLine") or tag,
            "profile_icon": hit.get("profileIconId"),
            "level": hit.get("summonerLevel"),
        }

    async def recent_matches(self, account: dict, count: int = 8) -> list[dict]:
        """Per-player entries of recent games; each keeps the full game under "_game"."""
        from .game import minimal_game, normalize_sgp_game, target_entry

        await self.gd.ensure_lol()
        payload = await self.bridge.matches(account["puuid"], account["server"], 0, count)
        source = (payload or {}).get("source")
        data = (payload or {}).get("data")
        if source == "lcu":
            entries = normalize_lcu_games(data, account["puuid"], self.gd)
            for e in entries:
                e["_game"] = minimal_game(e)
            return entries
        entries = []
        for g in (data or {}).get("games") or []:
            entry = target_entry(normalize_sgp_game(g, account["puuid"], self.gd))
            if entry:
                entries.append(entry)
        return entries

    async def summoner(self, account: dict, count: int | None = None, detail: bool = False) -> View:
        await self.gd.ensure_lol()
        count = count or (5 if detail else self.svc.cfg.match_count)
        server = account.get("server") or ""
        ranked: list[dict] = []
        ranked_err = ""
        try:
            ranked = normalize_ranked(await self.bridge.ranked(account["puuid"], server))
        except BridgeError as exc:
            ranked_err = str(exc)
            self.logger.info(f"[lolhelper] 国服段位查询失败：{exc}")
        matches = await self.recent_matches(account, count)
        created = time.time()
        riot_id = f"{account.get('game_name')}#{account.get('tag_line')}"
        doc = Doc(
            title=riot_id,
            subtitle=f"国服 · {cn_server_name(server)}" + (f" · Lv.{account['level']}" if account.get("level") else ""),
            icon=self.gd.profile_icon(account.get("profile_icon")),
            theme="cn",
        )
        tiles = []
        for r in ranked:
            q = QUEUE_NAMES.get(r["queue"], r["queue"])
            t = tier_text(r["tier"], r["division"], r["lp"])
            if r["queue"] in ("RANKED_SOLO_5x5", "RANKED_FLEX_SR") or detail:
                tiles.append(rank_tile(q, t, r["wins"], r["losses"], tier_emblem(r["tier"])))
        if not ranked:
            doc.badge("段位：" + ("跨区暂不可查" if ranked_err else "未定级"), "muted")
        doc.add_tiles("段位", tiles)
        if matches:
            from .game import add_match_cards

            doc.width = 880
            add_match_cards(
                doc,
                f"最近 {len(matches)} 场",
                [m["_game"] for m in matches],
                self.gd,
                note=OpggService.match_summary(matches) + " · 发送「/lol 对局 序号」查看单局详情",
            )
        else:
            doc.add_text("", "没有查到最近的对局记录")
        doc.footer = freshness(created) + " · 数据来自国服客户端"
        plain = [{k: v for k, v in m.items() if k != "_game"} for m in matches]
        return View("cn_summoner", {"account": account, "ranked": ranked, "matches": plain}, doc, created)

    def _rows(self, matches: list[dict]) -> list[list[Cell]]:
        rows = self.svc.match_rows(matches)
        for row, m in zip(rows, matches):
            kp = m.get("kill_participation")
            row[4] = Cell(pct(kp, 0) if kp is not None else "-", sub=f"第{m['placement']}名" if m.get("placement") else "")
        return rows

    async def game_detail(self, account: dict, index: int = 1) -> View:
        from .errors import UsageError
        from .game import build_game_view, normalize_sgp_game

        matches = await self.recent_matches(account, max(index, 5))
        if not 1 <= index <= len(matches):
            raise UsageError(f"只找到最近 {len(matches)} 场对局，序号需要在 1~{len(matches)} 之间")
        m = matches[index - 1]
        game = m.get("_game") or {}
        if game.get("source") == "sgp" and sum(len(t["players"]) for t in game.get("teams") or []) >= 2:
            detail = game  # the SGP match list already carries every participant
        else:
            payload = await self.bridge.game(m["id"], account.get("server") or "")
            raw = (payload or {}).get("data") or {}
            detail = normalize_sgp_game(raw, account["puuid"], self.gd)
        if not any(t["players"] for t in detail["teams"]):
            raise BridgeError("国服代理没有返回这局的玩家数据")
        return build_game_view(detail, self.gd, "cn", self.svc.cfg.timezone)

    async def status_view(self) -> View:
        st = await self.bridge.status()
        doc = Doc(title="国服连接状态", subtitle=self.bridge.label, theme="cn")
        doc.badge("已连接客户端" if st.get("connected") else "未连接客户端", "win" if st.get("connected") else "lose")
        cur = st.get("current") or {}
        doc.add_kv(
            "",
            [
                ("连接方式", "远程代理" if self.bridge.mode == "remote" else "内置"),
                ("版本", st.get("version")),
                ("当前大区", f"{st.get('server_name') or '-'}（{st.get('server') or '-'}）"),
                ("登录账号", f"{cur.get('gameName')}#{cur.get('tagLine')}" if cur.get("gameName") else "-"),
                ("战绩令牌", "正常" if st.get("token_entitlements") else "缺失"),
                ("会话令牌", "正常" if st.get("token_league-session") else "缺失"),
                ("检测方式", st.get("source")),
                ("错误", st.get("error")),
            ],
        )
        if not st.get("connected") and self.bridge.mode == "builtin":
            doc.add_text("", REMOTE_HINT)
        return View("cn_status", st, doc, time.time())

