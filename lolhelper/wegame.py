"""China server data through the WeGame web API (the "LOL 助手" pages on wegame.com.cn).

Needs the tgp_id / tgp_ticket cookies of a logged-in WeGame account. Used when the
League client (built-in or remote bridge) is not reachable. Undocumented API: may
change without notice.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import unquote

import aiohttp

from .constants import POSITION_FROM_API, QUEUE_ID_ZH, resolve_cn_server
from .errors import BridgeError, NotFoundError
from .gamedata import GameData
from .net import HttpClient

API = "https://www.wegame.com.cn/api/v1/wegame.pallas.game.LolBattle/"
FROM = "lol_helper"

# WeGame area id -> (old server name); from wegame.gtimg.com/.../LOLGameArea.js
WG_AREAS: dict[int, str] = {
    1: "艾欧尼亚", 2: "比尔吉沃特", 3: "祖安", 4: "诺克萨斯", 5: "班德尔城", 6: "德玛西亚", 7: "皮尔特沃夫",
    8: "战争学院", 9: "弗雷尔卓德", 10: "巨神峰", 11: "雷瑟守备", 12: "无畏先锋", 13: "裁决之地", 14: "黑色玫瑰",
    15: "暗影岛", 16: "恕瑞玛", 17: "钢铁烈阳", 18: "水晶之痕", 19: "均衡教派", 20: "扭曲丛林", 21: "教育网",
    22: "影流", 23: "守望之海", 24: "征服之海", 25: "卡拉曼达", 26: "巨龙之巢", 27: "皮城警备", 30: "男爵领域",
    31: "峡谷之巅", 66: "海斗之巅",
}
# WeGame tier index -> Riot tier; LOLKeywords.js: 最强王者 钻石 铂金 黄金 白银 青铜 超凡大师 傲世宗师 黑铁 翡翠
WG_TIERS = ["CHALLENGER", "DIAMOND", "PLATINUM", "GOLD", "SILVER", "BRONZE", "MASTER", "GRANDMASTER", "IRON", "EMERALD"]
APEX = {"CHALLENGER", "MASTER", "GRANDMASTER"}
DIVISIONS = ["I", "II", "III", "IV", "V"]

ERR_EXPIRED = 8025004
ERR_NO_LOGIN = 8000102
ERR_PRIVATE = 8000021


def parse_cookie(text: str) -> str:
    """Keep only tgp_id / tgp_ticket from a pasted cookie string."""
    kv = {}
    for part in (text or "").replace("\n", ";").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            kv[k.strip()] = v.strip()
    if kv.get("tgp_id") and kv.get("tgp_ticket"):
        return f"tgp_id={kv['tgp_id']}; tgp_ticket={kv['tgp_ticket']}"
    return ""


def wg_server(area: int | str) -> str:
    name = WG_AREAS.get(int(area or 0), "")
    return resolve_cn_server(name) or ("TENCENT_BGP3" if int(area or 0) == 66 else f"WG_{area}")


class WeGameClient:
    def __init__(self, http: HttpClient | None, cookie: str = "", timeout: float = 15, logger: logging.Logger | None = None):
        self.http = http
        self.cookie = parse_cookie(cookie)
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.logger = logger or logging.getLogger("lolhelper")
        self.expired = False

    @property
    def configured(self) -> bool:
        return bool(self.cookie)

    async def call(self, method: str, body: dict) -> dict:
        if not self.cookie:
            raise BridgeError("未配置 WeGame 凭证")
        headers = {
            "Content-Type": "application/json;charset=UTF-8", "Cookie": self.cookie,
            "Referer": "https://www.wegame.com.cn/helper/lol/", "Origin": "https://www.wegame.com.cn",
        }
        try:
            async with self.http.session.post(API + method, json={**body, "from_src": FROM}, headers=headers,
                                              timeout=self.timeout, proxy=None) as resp:
                data = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise BridgeError(f"连接 WeGame 失败：{type(exc).__name__}") from exc
        code = ((data or {}).get("result") or {}).get("error_code")
        if code in (ERR_EXPIRED, ERR_NO_LOGIN):
            self.expired = True
            raise BridgeError("WeGame 凭证已过期，请重新登录 wegame.com.cn 并更新插件配置「国服 → WeGame 凭证」")
        if code == ERR_PRIVATE:
            raise NotFoundError("该玩家在 WeGame 设置了战绩不公开")
        if code not in (0, None):
            msg = ((data or {}).get("result") or {}).get("error_message") or ""
            raise BridgeError(f"WeGame 返回错误 {code} {msg}".strip())
        self.expired = False
        return data or {}

    async def search(self, name: str, tag: str = "") -> list[dict]:
        players = (await self.call("SearchPlayer", {"nickname": name})).get("players") or []
        if tag and str(tag).isdigit():
            players = [p for p in players if str(p.get("tag_num")) == str(int(tag))]
        return players

    async def summoner_info(self, openid: str, area: int) -> dict:
        return (await self.call("GetSummonerInfo", {"account_type": 2, "id": openid, "area": int(area)})).get("summoner") or {}

    async def battles(self, openid: str, area: int, offset: int = 0, count: int = 10) -> list[dict]:
        body = {"account_type": 2, "id": openid, "area": int(area), "offset": offset, "count": count, "filter": ""}
        return (await self.call("GetBattleList", body)).get("battles") or []

    async def detail(self, openid: str, area: int, game_id: str) -> dict:
        body = {"account_type": 2, "id": openid, "area": int(area), "game_id": str(game_id)}
        return (await self.call("GetBattleDetail", body)).get("battle_detail") or {}


# ------------------------------------------------------------------ normalizers
def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _score(v) -> float | None:
    return round(_i(v) / 10000, 1) if v else None


def wg_ranked(summoner: dict) -> list[dict]:
    s = summoner.get("season") or {}
    out = []
    for queue, prefix in (("RANKED_SOLO_5x5", ""), ("RANKED_FLEX_SR", "team_")):
        t = s.get(prefix + "tier")
        if t is None or _i(t) == 255 or not 0 <= _i(t) < len(WG_TIERS):
            continue
        tier = WG_TIERS[_i(t)]
        out.append({
            "queue": queue, "tier": tier,
            "division": None if tier in APEX else DIVISIONS[min(_i(s.get(prefix + "queue")), 4)],
            "lp": s.get(prefix + "win_point"),
            "wins": _i(s.get(prefix + "wins")), "losses": _i(s.get(prefix + "losses")),
        })
    return out


def normalize_wg_game(d: dict, openid: str, gd: GameData) -> dict[str, Any]:
    from .game import MAPS, _sort_players

    remake = bool(d.get("was_early_surrender"))
    by_team: dict[str, list[dict]] = {}
    for p in d.get("player_details") or []:
        champ = gd.champion(p.get("championId"))
        full = unquote(str(p.get("name") or ""))
        name, _, tag = full.rpartition("#") if "#" in full else (full, "", "")
        win = str(p.get("win")) == "Win"
        by_team.setdefault(str(p.get("teamId")), []).append({
            "champion_id": p.get("championId"),
            "champion_name": (champ or {}).get("name_zh") or str(p.get("championId")),
            "game_name": name, "tag_line": tag,
            "is_target": bool(openid) and p.get("openid") == openid,
            "position": POSITION_FROM_API.get(str(p.get("position") or "").upper()),
            "items": [p.get(f"item{i}") for i in range(7) if p.get(f"item{i}")],
            "spells": [x for x in (p.get("summonSpell1Id"), p.get("summonSpell2Id")) if x],
            "keystone": p.get("PERK0") or p.get("keystoneId") or None,
            "sub_style": p.get("perkSubStyle") or None,
            "kills": p.get("championsKilled"), "deaths": p.get("numDeaths"), "assists": p.get("assists"),
            "level": p.get("level"),
            "cs": _i(p.get("minionsKilled")) + _i(p.get("neutralMinionsKilled")),
            "gold": p.get("goldEarned"),
            "damage": p.get("totalDamageToChampions"),
            "taken": p.get("totalDamageTaken"),
            "wards": p.get("wardPlaced"),
            "cc": p.get("timeCcingOthers"),
            "heal": p.get("totalHealth"),
            "shield": None,
            "multikill": p.get("largestMultiKill"),
            "score": _score(p.get("gameScore")),
            "score_rank": None,
            "result": "REMAKE" if remake else ("WIN" if win else "LOSE"),
            "win": win,
        })
    meta = {str(t.get("teamId")): t for t in d.get("team_details") or []}
    teams = []
    for tid in sorted(by_team):
        players, t = by_team[tid], meta.get(tid) or {}
        teams.append({
            "key": "BLUE" if tid == "100" else ("RED" if tid == "200" else tid),
            "win": str(t.get("win")) == "Win" if t else any(p["win"] for p in players),
            "kills": t.get("totalKills", sum(_i(p.get("kills")) for p in players)),
            "gold": t.get("totalGoldEarned") or sum(_i(p.get("gold")) for p in players),
            "towers": t.get("totalTurretsKilled"),
            "dragons": t.get("totalDragonKills"),
            "barons": t.get("totalBaronKills"),
            "hordes": None,
            "bans": [b.get("championId") for b in t.get("banInfoList") or [] if _i(b.get("championId")) > 0],
            "players": _sort_players(players),
        })
    qid = _i(d.get("game_queue_id")) if d.get("game_queue_id") is not None else -1
    return {
        "id": str(d.get("game_id")),
        "created_at": _i(d.get("game_start_time")) or None,
        "queue": QUEUE_ID_ZH.get(qid, f"模式{qid}"),
        "queue_id": qid,
        "map": MAPS.get(_i(d.get("map_id")), ""),
        "duration": d.get("game_time_played"),
        "avg_tier": "",
        "teams": teams,
        "source": "wegame",
    }


def wg_list_entry(b: dict, gd: GameData) -> dict[str, Any]:
    """Match entry from the battle list alone (no items / other players)."""
    from .game import minimal_game

    champ = gd.champion(b.get("champion_id"))
    qid = _i(b.get("game_queue_id"))
    e = {
        "id": str(b.get("game_id")),
        "champion_id": b.get("champion_id"),
        "champion_name": (champ or {}).get("name_zh") or str(b.get("champion_id")),
        "created_at": _i(b.get("game_start_time")) or None,
        "queue_id": qid,
        "queue": QUEUE_ID_ZH.get(qid, f"模式{qid}"),
        "duration": b.get("game_time_played"),
        "position": POSITION_FROM_API.get(str(b.get("position") or "").upper()),
        "items": [], "kills": b.get("kills"), "deaths": b.get("deaths"), "assists": b.get("assists"),
        "cs": None, "damage": None, "score": _score(b.get("game_score")), "score_rank": None,
        "result": "REMAKE" if b.get("was_early_surrender") else ("WIN" if str(b.get("win")) == "Win" else "LOSE"),
    }
    e["_game"] = {**minimal_game(e), "source": "wegame_list"}
    return e
