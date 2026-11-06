"""Per-game esports data from esports.op.gg (public GraphQL used by the website).

The OP.GG MCP only returns series scores; this module fills in each game's
picks, bans, players, items, runes and objectives.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .errors import UpstreamError
from .gamedata import GameData
from .net import HttpClient

GQL_ENDPOINT = "https://esports.op.gg/matches/graphql"

_TEAM = "id acronym name imageUrl imageUrlDarkMode"
MATCH_QUERY = """query LhMatch($id: ID!) { match(id: $id) {
  id name scheduledAt beginAt numberOfGames homeScore awayScore status
  homeTeam { %s } awayTeam { %s } winnerTeam { id }
  tournament { name serie { league { shortName } } } } }""" % (_TEAM, _TEAM)

GAME_QUERY = """query LhGame($matchId: ID!, $set: Int) { gameByMatch(matchId: $matchId, set: $set) {
  id beginAt length finished status winner { id acronym }
  teams { side bans kills towerKills inhibitorKills dragonKills elderDrakeKills baronKills heraldKills atakhanKills goldEarned team { %s } }
  players { side position championId kills deaths assists items spells opScore level creepScore goldEarned
    totalDamageDealtToChampions totalDamageTaken totalHeal totalTimeCrowdControlDealt wardsPlaced largestMultiKill
    perk { primary { style selections } sub { style selections } }
    player { nickName imageUrl } team { id acronym } } } }""" % _TEAM

POSITION = {"top": "top", "jun": "jungle", "jungle": "jungle", "mid": "mid", "adc": "adc", "bot": "adc", "sup": "support", "support": "support"}
POS_ORDER = {"top": 0, "jungle": 1, "mid": 2, "adc": 3, "support": 4}
LIVE = ("running", "in_progress", "live", "started")


class EsportsClient:
    def __init__(self, http: HttpClient, endpoint: str = GQL_ENDPOINT):
        self.http = http
        self.endpoint = endpoint

    async def _query(self, query: str, variables: dict) -> dict:
        data = await self.http.request_json(
            "POST", self.endpoint, json={"query": query, "variables": variables},
            headers={"Content-Type": "application/json", "Referer": "https://esports.op.gg/"},
        )
        if not isinstance(data, dict):
            raise UpstreamError("赛事数据接口返回格式异常")
        if data.get("errors") and not data.get("data"):
            raise UpstreamError(f"赛事数据接口错误：{data['errors'][0].get('message', '')}"[:200])
        return data.get("data") or {}

    async def match(self, match_id: str | int) -> dict | None:
        return (await self._query(MATCH_QUERY, {"id": str(match_id)})).get("match")

    async def game(self, match_id: str | int, set_no: int) -> dict | None:
        return (await self._query(GAME_QUERY, {"matchId": str(match_id), "set": int(set_no)})).get("gameByMatch")

    async def games(self, match_id: str | int, count: int) -> list[dict]:
        """Games 1..count of a series; missing (not started) games are dropped."""
        res = await asyncio.gather(*(self.game(match_id, i) for i in range(1, count + 1)), return_exceptions=True)
        out = []
        for i, g in enumerate(res, 1):
            if isinstance(g, dict) and g.get("players"):
                g["set"] = i
                out.append(g)
        return out


def match_to_schedule(m: dict) -> dict:
    """GraphQL match -> the MCP schedule shape used elsewhere in the plugin."""
    def team(t: dict | None) -> dict:
        t = t or {}
        return {"id": t.get("id"), "name": t.get("name"), "acronym": t.get("acronym"), "image_url": t.get("imageUrl")}

    status = str(m.get("status") or "").upper()
    if status in ("RUNNING", "STARTED"):
        status = "RUNNING"
    return {
        "id": m.get("id"), "name": m.get("name"), "status": status,
        "homeScore": m.get("homeScore"), "awayScore": m.get("awayScore"),
        "scheduledAt": m.get("beginAt") or m.get("scheduledAt"), "numberOfGames": m.get("numberOfGames"),
        "league": (((m.get("tournament") or {}).get("serie") or {}).get("league") or {}).get("shortName") or "",
        "homeTeam": team(m.get("homeTeam")), "awayTeam": team(m.get("awayTeam")),
        "details": f"https://esports.op.gg/matches/{m.get('id')}",
    }


def _i(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def normalize_esports_game(g: dict, gd: GameData) -> dict[str, Any]:
    """Convert a GraphQL game into the plugin's normalized game dict (see game.py)."""
    winner = str((g.get("winner") or {}).get("id") or "")
    teams = []
    for t in sorted(g.get("teams") or [], key=lambda t: 0 if t.get("side") == "blue" else 1):
        side = t.get("side")
        info = t.get("team") or {}
        players = []
        for p in g.get("players") or []:
            if p.get("side") != side:
                continue
            perk = p.get("perk") or {}
            prim, sub = perk.get("primary") or {}, perk.get("sub") or {}
            champ = gd.champion(p.get("championId"))
            players.append({
                "champion_id": _i(p.get("championId")),
                "champion_name": gd.display_name(champ).split(" ")[0] if champ else str(p.get("championId")),
                "game_name": (p.get("player") or {}).get("nickName") or "?",
                "tag_line": "",
                "is_target": False,
                "position": POSITION.get(str(p.get("position") or "").lower()),
                "items": [i for i in p.get("items") or [] if i],
                "spells": p.get("spells") or [],
                "keystone": (prim.get("selections") or [None])[0],
                "sub_style": sub.get("style"),
                "kills": p.get("kills"), "deaths": p.get("deaths"), "assists": p.get("assists"),
                "level": p.get("level"),
                "cs": p.get("creepScore"),
                "gold": p.get("goldEarned"),
                "damage": p.get("totalDamageDealtToChampions"),
                "taken": p.get("totalDamageTaken"),
                "wards": p.get("wardsPlaced"),
                "cc": p.get("totalTimeCrowdControlDealt"),
                "heal": p.get("totalHeal"),
                "shield": None,
                "multikill": p.get("largestMultiKill"),
                "score": p.get("opScore"),
            })
        players.sort(key=lambda p: POS_ORDER.get(p.get("position") or "", 9))
        teams.append({
            "key": "BLUE" if side == "blue" else "RED",
            "team_id": str(info.get("id") or ""),
            "label": info.get("acronym") or info.get("name") or "",
            "logo": info.get("imageUrl"),
            "win": bool(winner) and str(info.get("id")) == winner,
            "kills": t.get("kills"),
            "gold": t.get("goldEarned"),
            "towers": t.get("towerKills"),
            "dragons": t.get("dragonKills"),
            "barons": t.get("baronKills"),
            "hordes": None,
            "bans": [b for b in t.get("bans") or [] if b],
            "players": players,
        })
    return {
        "id": g.get("id"),
        "set": g.get("set"),
        "created_at": g.get("beginAt"),
        "queue": "",
        "map": "召唤师峡谷",
        "duration": g.get("length"),
        "finished": bool(g.get("finished")),
        "teams": teams,
        "source": "esports",
    }


# ---------------------------------------------------------------- views
def _pick_sides(n: dict, home_id: str, home_acr: str) -> tuple[dict, dict]:
    teams = n["teams"]
    if len(teams) < 2:
        raise UpstreamError("该局数据不完整")
    for i, t in enumerate(teams):
        if (home_id and t["team_id"] == str(home_id)) or (home_acr and t["label"].lower() == home_acr.lower()):
            return t, teams[1 - i]
    return teams[0], teams[1]


def series_row(n: dict, g: dict, gd: GameData) -> tuple[dict, str]:
    from .fmt import big, duration, kda

    home = g.get("homeTeam") or {}
    ht, at = _pick_sides(n, str(home.get("id") or ""), home.get("acronym") or "")
    win_t = ht if ht["win"] else at if at["win"] else None
    mvp = max(win_t["players"], key=lambda p: float(p.get("score") or -1)) if win_t and win_t["players"] else None

    def side(t: dict) -> dict:
        return {
            "label": t["label"], "logo": t["logo"], "win": t["win"], "lose": bool(win_t) and not t["win"],
            "side": "蓝方" if t["key"] == "BLUE" else "红方", "kills": _i(t.get("kills")), "gold": big(t.get("gold")),
            "obj": [{"icon": gd.obj_icon(kind, t["key"] == "RED"), "label": label, "n": _i(t.get(key))}
                    for key, label, kind in (("towers", "塔", "tower"), ("dragons", "龙", "dragon"), ("barons", "男爵", "baron"))],
            "gold_icon": gd.obj_icon("gold"),
            "champs": [{"icon": gd.champ_icon(gd.champion(p["champion_id"])), "name": p["game_name"],
                        "mvp": p is mvp} for p in t["players"]],
        }

    row = {
        "no": f"第{n.get('set')}局", "dur": duration(n.get("duration")), "live": not n.get("finished"),
        "home": side(ht), "away": side(at),
        "mvp": {"icon": gd.champ_icon(gd.champion(mvp["champion_id"])), "name": mvp["game_name"],
                "kda": f"{_i(mvp.get('kills'))}/{_i(mvp.get('deaths'))}/{_i(mvp.get('assists'))}"} if mvp else None,
    }
    line = (f"{row['no']} {row['dur']}{' 进行中' if row['live'] else ''}  {ht['label']}({row['home']['side'][0]}) "
            f"{row['home']['kills']} : {row['away']['kills']} {at['label']}({row['away']['side'][0]})")
    if win_t:
        line += f"  {win_t['label']} 胜"
    if mvp:
        line += f" · MVP {mvp['game_name']}（{mvp['champion_name']} {row['mvp']['kda']}，KDA {kda(mvp.get('kills'), mvp.get('deaths'), mvp.get('assists'))}）"
    picks = lambda t: "、".join(p["champion_name"] for p in t["players"])
    line += f"\n    {ht['label']}：{picks(ht)}\n    {at['label']}：{picks(at)}"
    return row, line


def build_series_view(g: dict, games: list[dict], gd: GameData, tz: str, created: float,
                      headline: str = "", key: str = ""):
    from .doc import Doc
    from .fmt import freshness
    from .constants import stage_zh
    from .service import View, league_theme, versus_row

    home, away = g.get("homeTeam") or {}, g.get("awayTeam") or {}
    h, a = home.get("acronym") or home.get("name") or "待定", away.get("acronym") or away.get("name") or "待定"
    stage = stage_zh(str(g.get("name") or "").split(":")[0].strip())
    doc = Doc(title=f"{h} vs {a}", theme="esports", width=920)
    info = league_theme(doc, g.get("league"))
    doc.subtitle = " · ".join(x for x in [info["full"], stage] if x)
    if not doc.icon:
        doc.icon, doc.icon_logo = home.get("image_url"), True
    if headline:
        doc.badge(headline, "accent")
    row, line = versus_row(g, tz)
    doc.add_block("", "versus", {"rows": [row]}, [line])
    rows, lines = [], []
    for gm in games:
        try:
            r, ln = series_row(normalize_esports_game(gm, gd), g, gd)
        except UpstreamError:
            continue
        rows.append(r)
        lines.append(ln)
    if rows:
        doc.add_block("每局数据", "series", {"rows": rows}, lines)
        ref = key or h
        doc.tip(f"/lol 赛况 {ref} 1", "第 1 局双方 10 人数据")
    elif str(g.get("status") or "").upper() == "FINISHED":
        doc.add_text("每局数据", "OP.GG 暂未提供该场比赛的单局数据")
    else:
        doc.add_text("每局数据", "比赛尚未开始" if row["score_cls"] == "todo" else "第一局数据暂未更新")
    doc.tip("/lol 赛果", "其他比赛结果")
    doc.tip(f"/lol 订阅 赛事 {info['short'] if info['color'] else 'S赛 LPL'}", "开赛提醒与赛果推送")
    doc.footer = freshness(created)
    doc.link = g.get("details") or ""
    return View("lol_esports_match", {"match": g, "games": games}, doc, created)


def build_esports_game_view(g: dict, game: dict, gd: GameData, tz: str, created: float):
    from .constants import stage_zh
    from .fmt import duration, local_time
    from .game import build_game_view
    from .service import league_theme

    n = normalize_esports_game(game, gd)
    view = build_game_view(n, gd, "esports", tz, created)
    home = g.get("homeTeam") or {}
    ht, at = _pick_sides(n, str(home.get("id") or ""), home.get("acronym") or "")
    win_t = ht if ht["win"] else at if at["win"] else None
    head = f"{win_t['label']} 胜" if win_t else "进行中"
    stage = stage_zh(str(g.get("name") or "").split(":")[0].strip())
    info = league_theme(view.doc, g.get("league"))
    view.doc.title = f"{head} · 第{n.get('set')}局 · {duration(n.get('duration'))}"
    view.doc.subtitle = " · ".join(x for x in [
        f"{ht['label']} vs {at['label']}", info["full"], stage, local_time(n.get("created_at"), tz, "%Y-%m-%d %H:%M"),
    ] if x)
    if not info["logo"]:
        view.doc.icon, view.doc.icon_logo = (win_t or ht).get("logo"), True
    view.doc.tips = []
    ref = home.get("acronym") or ht["label"]
    view.doc.tip(f"/lol 赛况 {ref}", "返回整场每局数据")
    if int(g.get("homeScore") or 0) + int(g.get("awayScore") or 0) > int(n.get("set") or 0):
        view.doc.tip(f"/lol 赛况 {ref} {int(n.get('set') or 0) + 1}", "下一局")
    best = max((p for t in n["teams"] for p in t["players"]), key=lambda p: float(p.get("score") or -1), default=None)
    if best and best.get("champion_name"):
        view.doc.tip(f"/lol 英雄 {best['champion_name']}", f"{best['game_name']} 所用英雄的出装")
    view.doc.link = g.get("details") or ""
    view.kind = "lol_esports_game"
    return view
