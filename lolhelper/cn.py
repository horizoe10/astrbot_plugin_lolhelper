"""China (Tencent) server support.

Built-in mode: bridge/cn_bridge.py is loaded in-process and talks to the League
client on this machine. Remote mode: the same script runs on the gaming PC and
the plugin calls it over HTTP (address may carry the token: http://ip:6198?token=xxx).
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlsplit, urlunsplit

import aiohttp

from .log import default_logger
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
                 bridge_file: Path | None = None, logger: Any = None):
        self.http = http
        self.url, self.token = parse_remote(url, token)
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.request_timeout = timeout
        self.bridge_file = Path(bridge_file or BRIDGE_FILE)
        self.logger = logger or default_logger
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
            mod.log = self.logger  # log through AstrBot's logger when running in-process
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
    """China server queries. Data source order: League client (built-in / remote bridge), then WeGame."""

    def __init__(self, bridge: CnBridgeClient, svc: OpggService, logger: Any = None,
                 wegame: "WeGameClient | None" = None):
        from .wegame import WeGameClient

        self.bridge = bridge
        self.svc = svc
        self.gd = svc.gd
        self.logger = logger or default_logger
        self.wg = wegame or WeGameClient(None)
        self._ids: dict[tuple, Any] = {}  # (kind, name, tag, server) -> puuid / (openid, area)

    # ------------------------------------------------------------- sources
    def sources(self) -> list[str]:
        if not self.wg.configured:
            return ["client"]
        if self.bridge.mode == "builtin" and os.name != "nt":
            return ["wegame"]  # no League client can run here
        return ["client", "wegame"]

    async def _run(self, what: str, ops: dict[str, Callable[[], Awaitable[Any]]]) -> tuple[str, Any]:
        errors: list[str] = []
        for src in self.sources():
            try:
                return src, await ops[src]()
            except NotFoundError:
                raise
            except BridgeError as exc:
                errors.append(str(exc))
                self.logger.info(f"[lolhelper] 国服{what}（{'客户端' if src == 'client' else 'WeGame'}）失败：{exc}")
        if len(errors) > 1:
            raise BridgeError("客户端：" + errors[0] + "\nWeGame：" + errors[1])
        raise BridgeError(errors[0] if errors else "没有可用的国服数据源")

    async def _wg_ids(self, account: dict) -> tuple[str, int]:
        from .wegame import wg_server

        if account.get("wg_openid"):
            return account["wg_openid"], int(account.get("wg_area") or 0)
        key = ("wg", account.get("game_name"), str(account.get("tag_line")), account.get("server"))
        if key in self._ids:
            return self._ids[key]
        players = await self.wg.search(account.get("game_name") or "", str(account.get("tag_line") or ""))
        same = [p for p in players if wg_server(p.get("area")) == account.get("server")] or players
        if not same:
            raise NotFoundError(f"WeGame 没有找到 {account.get('game_name')}#{account.get('tag_line')}")
        p = max(same, key=lambda x: int(x.get("level") or 0))
        self._ids[key] = (p.get("openid"), int(p.get("area") or 0))
        return self._ids[key]

    async def _puuid(self, account: dict) -> str:
        if account.get("puuid"):
            return account["puuid"]
        key = ("puuid", account.get("game_name"), str(account.get("tag_line")))
        if key not in self._ids:
            hit = await self.bridge.lookup(account.get("game_name") or "", str(account.get("tag_line") or ""))
            self._ids[key] = hit.get("puuid")
        return self._ids[key]

    async def _wg_detail(self, openid: str, area: int, game_id: str) -> dict:
        raw, _ = await self.svc.cache.get_or_load(f"wg:game:{game_id}", 86400, lambda: self.wg.detail(openid, area, game_id))
        return raw

    # ------------------------------------------------------------- queries
    async def resolve_account(self, name: str, tag: str, server: str | None) -> dict:
        async def client():
            hit = await self.bridge.lookup(name, tag)
            srv = server
            if not srv:
                srv = (await self.bridge.status()).get("server") or ""
            return {
                "kind": "cn", "server": srv, "puuid": hit.get("puuid"),
                "game_name": hit.get("gameName") or name, "tag_line": hit.get("tagLine") or tag,
                "profile_icon": hit.get("profileIconId"), "level": hit.get("summonerLevel"),
            }

        async def wegame():
            from .wegame import wg_server

            players = await self.wg.search(name, tag)
            if server:
                players = [p for p in players if wg_server(p.get("area")) == server]
            if not players:
                where = f"（{cn_server_name(server)}）" if server else ""
                raise NotFoundError(f"WeGame 没有找到 {name}#{tag}{where}")
            p = max(players, key=lambda x: int(x.get("level") or 0))
            return {
                "kind": "cn", "server": wg_server(p.get("area")), "puuid": "",
                "wg_openid": p.get("openid"), "wg_area": int(p.get("area") or 0),
                "game_name": name, "tag_line": str(p.get("tag_num") or tag),
                "profile_icon": p.get("icon_id"), "level": p.get("level"),
            }

        _, acc = await self._run("账号查询", {"client": client, "wegame": wegame})
        return acc

    async def recent_matches(self, account: dict, count: int = 8) -> list[dict]:
        """Per-player entries of recent games; each keeps the full game under "_game"."""
        from .game import minimal_game, normalize_sgp_game, target_entry
        from .wegame import normalize_wg_game, wg_list_entry

        await self.gd.ensure_lol()

        async def client():
            puuid = await self._puuid(account)
            payload = await self.bridge.matches(puuid, account["server"], 0, count)
            source = (payload or {}).get("source")
            data = (payload or {}).get("data")
            if source == "lcu":
                entries = normalize_lcu_games(data, puuid, self.gd)
                for e in entries:
                    e["_game"] = minimal_game(e)
                return entries
            entries = []
            for g in (data or {}).get("games") or []:
                entry = target_entry(normalize_sgp_game(g, puuid, self.gd))
                if entry:
                    entries.append(entry)
            return entries

        async def wegame():
            openid, area = await self._wg_ids(account)
            battles = await self.wg.battles(openid, area, 0, count)
            if not self.svc.cfg.match_details:
                return [wg_list_entry(b, self.gd) for b in battles]
            details = await asyncio.gather(*(self._wg_detail(openid, area, b.get("game_id")) for b in battles),
                                           return_exceptions=True)
            entries = []
            for b, d in zip(battles, details):
                entry = target_entry(normalize_wg_game(d, openid, self.gd)) if isinstance(d, dict) and d else None
                entries.append(entry or wg_list_entry(b, self.gd))
            return entries

        _, entries = await self._run("战绩查询", {"client": client, "wegame": wegame})
        return entries

    async def ranked(self, account: dict) -> list[dict]:
        from .wegame import wg_ranked

        async def client():
            return normalize_ranked(await self.bridge.ranked(await self._puuid(account), account.get("server") or ""))

        async def wegame():
            info = await self.wg.summoner_info(*(await self._wg_ids(account)))
            if info.get("level"):
                account["level"] = info["level"]
            if info.get("icon_id"):
                account["profile_icon"] = info["icon_id"]
            return wg_ranked(info)

        _, out = await self._run("段位查询", {"client": client, "wegame": wegame})
        return out

    async def summoner(self, account: dict, count: int | None = None, detail: bool = False) -> View:
        await self.gd.ensure_lol()
        count = count or (5 if detail else self.svc.cfg.match_count)
        server = account.get("server") or ""
        ranked: list[dict] = []
        ranked_err = ""
        try:
            ranked = await self.ranked(account)
        except (BridgeError, NotFoundError) as exc:
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
                note=OpggService.match_summary(matches),
            )
            OpggService.match_tips(doc, matches)
        else:
            doc.add_text("", "没有查到最近的对局记录")
        from_wg = bool(matches) and str((matches[0].get("_game") or {}).get("source", "")).startswith("wegame")
        doc.footer = freshness(created) + (" · 数据来自 WeGame" if from_wg else " · 数据来自国服客户端")
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
        from .wegame import normalize_wg_game

        matches = await self.recent_matches(account, max(index, 5))
        if not 1 <= index <= len(matches):
            raise UsageError(f"只找到最近 {len(matches)} 场对局，序号需要在 1~{len(matches)} 之间")
        m = matches[index - 1]
        game = m.get("_game") or {}
        full = sum(len(t["players"]) for t in game.get("teams") or []) >= 2
        if game.get("source") in ("sgp", "wegame") and full:
            detail = game  # already carries every participant
        elif str(game.get("source")).startswith("wegame"):
            openid, area = await self._wg_ids(account)
            detail = normalize_wg_game(await self._wg_detail(openid, area, m["id"]), openid, self.gd)
        else:
            payload = await self.bridge.game(m["id"], account.get("server") or "")
            raw = (payload or {}).get("data") or {}
            detail = normalize_sgp_game(raw, await self._puuid(account), self.gd)
        if not any(t["players"] for t in detail["teams"]):
            raise BridgeError("没有返回这局的玩家数据")
        view = build_game_view(detail, self.gd, "cn", self.svc.cfg.timezone)
        if detail.get("source") == "wegame":
            view.doc.footer = view.doc.footer.replace("数据来自国服客户端", "数据来自 WeGame")
            if "WeGame" not in view.doc.footer:
                view.doc.footer += " · 数据来自 WeGame"
        return view

    async def status_view(self) -> View:
        try:
            st = await self.bridge.status()
        except BridgeError as exc:
            st = {"connected": False, "error": str(exc)}
        wg_state = "未配置"
        if self.wg.configured:
            try:
                await self.wg.search("Uzi")
                wg_state = "正常"
            except BridgeError as exc:
                wg_state = "已过期，请更新" if self.wg.expired else f"异常：{exc}"
        doc = Doc(title="国服连接状态", subtitle=self.bridge.label, theme="cn")
        if st.get("connected"):
            doc.badge("已连接客户端", "win")
        elif wg_state == "正常":
            doc.badge("使用 WeGame", "win")
        else:
            doc.badge("未连接客户端", "lose")
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
                ("WeGame 凭证", wg_state),
                ("查询顺序", " → ".join("客户端" if s == "client" else "WeGame" for s in self.sources())),
            ],
        )
        if not st.get("connected") and wg_state != "正常":
            doc.add_text("", REMOTE_HINT + "；或在「国服 → WeGame 凭证」填写 WeGame 登录凭证")
        return View("cn_status", {**st, "wegame": wg_state}, doc, time.time())
