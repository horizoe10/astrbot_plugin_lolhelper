"""Per-game esports data from esports.op.gg (public GraphQL used by the website).

The OP.GG MCP only returns series scores; this module fills in each game's
picks, bans, players, items, runes and objectives.
"""

from __future__ import annotations

import asyncio
import time
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

LIVE_QUERY = """query LhLive($from: Date, $to: Date) { matches(from: $from, to: $to) {
  id name scheduledAt beginAt numberOfGames homeScore awayScore status
  homeTeam { %s } awayTeam { %s } tournament { name serie { league { shortName } } } } }""" % (_TEAM, _TEAM)

GAME_QUERY = """query LhGame($matchId: ID!, $set: Int) { gameByMatch(matchId: $matchId, set: $set) {
  id beginAt length finished status winner { id acronym }
  teams { side bans kills towerKills inhibitorKills dragonKills elderDrakeKills baronKills heraldKills atakhanKills goldEarned firstTower firstDragon firstBaron firstInhibitor team { %s } }
  players { side position championId kills deaths assists items roleBoundItem spells opScore level creepScore goldEarned
    totalDamageDealtToChampions totalDamageTaken totalHeal totalTimeCrowdControlDealt wardsPlaced wardsKilled largestMultiKill
    doubleKills tripleKills quadraKills pentaKills firstBloodKill firstTowerKill opScoreTimeline { minute score }
    perk { primary { style selections } sub { style selections } }
    player { nickName imageUrl } team { id acronym } } } }""" % _TEAM

TEAMS_QUERY = """query LhTeams($from: Date, $to: Date) { matches(from: $from, to: $to) {
  homeTeam { %s } awayTeam { %s } } }""" % (_TEAM, _TEAM)

POSITION = {"top": "top", "jun": "jungle", "jungle": "jungle", "mid": "mid", "adc": "adc", "bot": "adc", "sup": "support", "support": "support"}
POS_ORDER = {"top": 0, "jungle": 1, "mid": 2, "adc": 3, "support": 4}
POS_ZH = {"top": "上单", "jungle": "打野", "mid": "中单", "adc": "下路", "support": "辅助"}
LIVE = ("running", "in_progress", "live", "started")
# team colours of the scoreboard banner: series pips, logo glow, background tint, kill digits
SIDE_STYLE = {
    "blue": {"color": "#3d8bff", "glow": "rgba(61,139,255,.6)", "tint": "rgba(61,139,255,.42)", "text": "#7db5ff"},
    "red": {"color": "#ff4655", "glow": "rgba(255,70,85,.55)", "tint": "rgba(255,70,85,.36)", "text": "#ff7c87"},
}


class EsportsClient:
    def __init__(self, http: HttpClient, endpoint: str = GQL_ENDPOINT):
        self.http = http
        self.endpoint = endpoint
        self.teams: dict[str, dict] = {}  # team imageUrl -> {acronym, name, dark}, learned from every query
        self._logos_at = 0.0
        self._logos_task: asyncio.Future | None = None

    async def _query(self, query: str, variables: dict) -> dict:
        data = await self.http.request_json(
            "POST", self.endpoint, json={"query": query, "variables": variables},
            headers={"Content-Type": "application/json", "Referer": "https://esports.op.gg/"},
        )
        if not isinstance(data, dict):
            raise UpstreamError("赛事数据接口返回格式异常")
        if data.get("errors") and not data.get("data"):
            raise UpstreamError(f"赛事数据接口错误：{data['errors'][0].get('message', '')}"[:200])
        self._learn(data.get("data"))
        return data.get("data") or {}

    def _learn(self, node: Any) -> None:
        if isinstance(node, dict):
            url = node.get("imageUrl")
            if isinstance(url, str) and url and "acronym" in node:  # teams (players have imageUrl but no acronym)
                dark = node.get("imageUrlDarkMode")
                self.teams[url] = {"acronym": node.get("acronym") or "", "name": node.get("name") or "",
                                   "dark": dark if isinstance(dark, str) and dark != url else ""}
            for v in node.values():
                if isinstance(v, (dict, list)):
                    self._learn(v)
        elif isinstance(node, list):
            for v in node:
                self._learn(v)

    async def team_logos(self, wait: float = 6) -> dict[str, dict]:
        """Acronym, name and dark-mode logo of each team by imageUrl, used to pick the logo shown on image cards.
        The MCP schedules and standings only carry imageUrl, so the teams of the last five months are loaded
        every 12 hours in the background; a slow load does not hold the card up longer than wait seconds."""
        if (self._logos_task is None or self._logos_task.done()) and time.time() - self._logos_at > 12 * 3600:
            self._logos_at = time.time()
            self._logos_task = asyncio.ensure_future(self._load_logos())
        if self._logos_task is not None and not self._logos_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self._logos_task), wait)
            except Exception:
                pass
        return self.teams

    async def _load_logos(self) -> None:
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        try:
            await self._query(TEAMS_QUERY, {"from": (now - timedelta(days=150)).strftime(fmt), "to": (now + timedelta(days=14)).strftime(fmt)})
        except Exception:
            self._logos_at = time.time() - 12 * 3600 + 600  # retry in 10 minutes

    async def match(self, match_id: str | int) -> dict | None:
        return (await self._query(MATCH_QUERY, {"id": str(match_id)})).get("match")

    async def live(self, hours_back: float = 12) -> list[dict]:
        """Matches in progress. The MCP schedule lists only unstarted matches and the results only finished ones."""
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        data = await self._query(LIVE_QUERY, {"from": (now - timedelta(hours=hours_back)).strftime(fmt),
                                              "to": (now + timedelta(hours=1)).strftime(fmt)})
        return [m for m in data.get("matches") or [] if str(m.get("status") or "").lower() in ("running", "started", "live", "in_progress")]

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
    from .game import split_role_item

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
                "items": split_role_item(p.get("items"), p.get("roleBoundItem"))[0],
                "role_item": p.get("roleBoundItem") or None,
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
                "wards_killed": p.get("wardsKilled"),
                "multi": {"double": p.get("doubleKills"), "triple": p.get("tripleKills"),
                          "quadra": p.get("quadraKills"), "penta": p.get("pentaKills")},
                "first_blood": bool(p.get("firstBloodKill")),
                "first_tower": bool(p.get("firstTowerKill")),
                "score_tl": [x.get("score") for x in sorted(p.get("opScoreTimeline") or [], key=lambda x: _i(x.get("minute")))],
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
            "first_tower": t.get("firstTower"), "first_dragon": t.get("firstDragon"),
            "first_baron": t.get("firstBaron"), "first_inhibitor": t.get("firstInhibitor"),
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
def has_scores(players: list[dict]) -> bool:
    """OP.GG fills opScore some time after a game ends; until then every player has 0 / None."""
    return any(float(p.get("score") or 0) > 0 for p in players)


def pick_mvp(players: list[dict]) -> dict | None:
    """Highest OP.GG rating; before ratings exist, rate by KDA, kill participation and damage share."""
    if not players:
        return None
    if has_scores(players):
        return max(players, key=lambda p: float(p.get("score") or 0))
    team_kills = sum(_i(p.get("kills")) for p in players) or 1
    team_damage = sum(_i(p.get("damage")) for p in players) or 1

    def rate(p: dict) -> float:
        k, d, a = _i(p.get("kills")), _i(p.get("deaths")), _i(p.get("assists"))
        return min((k + a) / max(d, 1), 15) / 15 * 4 + (k + a) / team_kills * 3 + _i(p.get("damage")) / team_damage * 3

    return max(players, key=rate)


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
    mvp = pick_mvp(win_t["players"]) if win_t else None

    def side(t: dict) -> dict:
        return {
            "label": t["label"], "logo": t["logo"], "win": t["win"], "lose": bool(win_t) and not t["win"],
            "side": "蓝队" if t["key"] == "BLUE" else "红队", "color": "blue" if t["key"] == "BLUE" else "red",
            "kills": _i(t.get("kills")), "gold": f"{_i(t.get('gold')):,}",
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


# ---------------------------------------------------------------- banners and panels
def _home_won(gm: dict, home: dict) -> bool | None:
    win = gm.get("winner") or {}
    wid, wacr = str(win.get("id") or ""), str(win.get("acronym") or "")
    if not wid and not wacr:
        return None
    return wid == str(home.get("id") or "") if wid else wacr.lower() == str(home.get("acronym") or "").lower()


def series_at(g: dict, games: list[dict] | None, set_no: int) -> tuple[int, int]:
    """Series score (home, away) right after game set_no; the match score when the games are unknown."""
    if not games:
        return _i(g.get("homeScore")), _i(g.get("awayScore"))
    h = a = 0
    for gm in games:
        if _i(gm.get("set")) <= set_no and gm.get("finished", True):
            won = _home_won(gm, g.get("homeTeam") or {})
            if won is True:
                h += 1
            elif won is False:
                a += 1
    return h, a


def _vteam(name: str, logo: str | None, style: dict, series: int, need: int, chips: list[dict], cls: str) -> dict:
    return {"name": name, "logo": logo, "cls": cls, "series": series, "pips": [i < series for i in range(need)] if need else [],
            "chips": chips, **style}


def series_hero(g: dict, tz: str, headline: str = "") -> dict:
    """Scoreboard banner of a whole series: team logos, series score and pips, state, on the league poster."""
    from .constants import league_backdrop, league_info, stage_zh
    from .fmt import local_time
    from .render.html import mix, rgba

    home, away = g.get("homeTeam") or {}, g.get("awayTeam") or {}
    hs, as_ = _i(g.get("homeScore")), _i(g.get("awayScore"))
    status = str(g.get("status") or "").upper()
    done, live = status == "FINISHED", status in ("RUNNING", "IN_PROGRESS", "LIVE", "STARTED")
    info = league_info(g.get("league"))
    acc = info["color"] or "#8b5cf6"
    bo = _i(g.get("numberOfGames"))
    need = bo // 2 + 1 if bo else 0

    def side(t: dict, score: int, won: bool, lost: bool, left: bool) -> dict:
        if lost:
            style = {"color": "#5b6270", "glow": "rgba(0,0,0,.45)", "tint": "rgba(120,128,140,.10)", "text": "#8a919c"}
        else:
            style = {"color": acc, "glow": rgba(acc, .55), "tint": rgba(acc, .32 if won else .2), "text": mix(acc, "#ffffff", .45)}
        chips = [{"text": "胜利", "cls": "win"}] if won else [{"text": "失败", "cls": "lose"}] if lost else []
        return _vteam(t.get("acronym") or t.get("name") or "待定", t.get("image_url"), style, score, need, chips,
                      "win" if won else "lose" if lost else "")

    meta = [{"k": headline, "cls": "acc"}] if headline else []
    if live:
        meta.append({"k": f"第 {hs + as_ + 1} 局进行中", "cls": "live"})
    elif done:
        meta.append({"k": f"共 {hs + as_} 局", "cls": "dark"})
    return {
        "layout": "versus", "logo": info["logo"], "league": info["full"] or str(g.get("league") or "英雄联盟赛事"),
        "stage": stage_zh(str(g.get("name") or "").split(":")[0].strip()),
        "when": local_time(g.get("scheduledAt"), tz, "%Y-%m-%d %H:%M"), "bo": f"BO{bo}" if bo else "",
        "label": "已结束" if done else "进行中" if live else "未开始",
        "l": side(home, hs, done and hs > as_, done and hs < as_, True),
        "r": side(away, as_, done and as_ > hs, done and as_ < hs, False),
        "score": [str(hs), str(as_)] if done or live else None, "cap": "大比分" if done or live else "", "meta": meta,
        **league_backdrop(info),
    }


def momentum(blue_tl: dict, red_tl: dict, b_label: str, r_label: str, w: int = 440, h: int = 236) -> dict | None:
    """Gap of the two teams' average OP.GG live rating per minute as an SVG area chart, plus lead summaries."""
    from html import escape

    ms = sorted(set(blue_tl) & set(red_tl))
    if len(ms) < 3:
        return None
    blue, red = SIDE_STYLE["blue"]["color"], SIDE_STYLE["red"]["color"]
    diff = [(m, blue_tl[m] - red_tl[m]) for m in ms]
    x0, x1, top, bot = 4, w - 36, 12, h - 20
    vals = [d for _, d in diff]
    lo, hi = min(min(vals), 0), max(max(vals), 0)
    rng = (hi - lo) or 2
    hi, lo = max(hi, rng * .25), min(lo, -rng * .25)  # room on the side that never led
    pad = (hi - lo) * .08
    hi, lo = hi + pad, lo - pad
    X = lambda m: x0 + (m - ms[0]) / max(ms[-1] - ms[0], 1) * (x1 - x0)  # noqa: E731
    Y = lambda d: top + (hi - d) / (hi - lo) * (bot - top)  # noqa: E731
    mid = Y(0)
    pts = " ".join(f"{X(m):.1f},{Y(d):.1f}" for m, d in diff)
    area = f"{X(ms[0]):.1f},{mid:.1f} {pts} {X(ms[-1]):.1f},{mid:.1f}"
    flip, lead = None, 0
    for m, d in diff:
        s = 1 if d > .3 else -1 if d < -.3 else 0
        if s and s != lead:
            if lead:
                flip = (m, s)
            lead = s
    grid = "".join(f'<line x1="{x0}" y1="{y:.1f}" x2="{x1}" y2="{y:.1f}" stroke="rgba(255,255,255,.07)" stroke-dasharray="3 4"/>'
                   for y in ((top + mid) / 2, (mid + bot) / 2))
    ticks = "".join(f'<text x="{X(m):.1f}" y="{h - 4}" fill="#8e96a3" font-size="11" text-anchor="middle">{m}′</text>'
                    for m in ms if m % 5 == 0)
    mark = ""
    if flip:
        m, s = flip
        who = escape(b_label if s > 0 else r_label)
        tx, anchor = (X(m) - 7, "end") if X(m) > w * .6 else (X(m) + 7, "start")
        mark = (f'<line x1="{X(m):.1f}" y1="{top}" x2="{X(m):.1f}" y2="{bot}" stroke="#ffcc4d" stroke-width="1.5" stroke-dasharray="4 3"/>'
                f'<text x="{tx:.1f}" y="{top + 30}" fill="#ffcc4d" font-size="12.5" font-weight="800" text-anchor="{anchor}">{m}′ {who} 反超</text>')
    em, ed = diff[-1]
    endmark = (f'<circle cx="{X(em):.1f}" cy="{Y(ed):.1f}" r="3.5" fill="#fff"/>'
               f'<text x="{X(em) + 7:.1f}" y="{Y(ed) + 4:.1f}" fill="{blue if ed >= 0 else red}" font-size="13" font-weight="800">{abs(ed):.1f}</text>')
    svg = (f'<svg viewBox="0 0 {w} {h}" xmlns="http://www.w3.org/2000/svg" font-family="HarmonyOS Sans SC, Microsoft YaHei UI, Noto Sans CJK SC, sans-serif">'
           f'<defs><clipPath id="up"><rect x="0" y="0" width="{w}" height="{mid:.1f}"/></clipPath><clipPath id="dn"><rect x="0" y="{mid:.1f}" width="{w}" height="{h}"/></clipPath>'
           f'<linearGradient id="gu" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{blue}" stop-opacity=".6"/><stop offset="1" stop-color="{blue}" stop-opacity=".08"/></linearGradient>'
           f'<linearGradient id="gd" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{red}" stop-opacity=".08"/><stop offset="1" stop-color="{red}" stop-opacity=".6"/></linearGradient></defs>'
           f'{grid}<line x1="{x0}" y1="{mid:.1f}" x2="{x1}" y2="{mid:.1f}" stroke="rgba(255,255,255,.25)"/>'
           f'<polygon points="{area}" fill="url(#gu)" clip-path="url(#up)"/><polygon points="{area}" fill="url(#gd)" clip-path="url(#dn)"/>'
           f'<polyline points="{pts}" fill="none" stroke="#f2f4f7" stroke-width="2" stroke-linejoin="round"/>{mark}{endmark}{ticks}'
           f'<text x="{x0 + 2}" y="{top + 11}" fill="{blue}" font-size="12" font-weight="800">▲ 蓝队领先</text>'
           f'<text x="{x0 + 2}" y="{bot - 3}" fill="{red}" font-size="12" font-weight="800">▼ 红队领先</text></svg>')
    bm, rm = max(diff, key=lambda x: x[1]), min(diff, key=lambda x: x[1])
    return {"svg": svg,
            "blue": {"min": sum(1 for _, d in diff if d > .3), "max": f"+{bm[1]:.1f} 分" if bm[1] > .3 else "—", "at": bm[0] if bm[1] > .3 else None},
            "red": {"min": sum(1 for _, d in diff if d < -.3), "max": f"+{-rm[1]:.1f} 分" if rm[1] < -.3 else "—", "at": rm[0] if rm[1] < -.3 else None}}


def _curve(t: dict) -> dict[int, float]:
    lines = [p.get("score_tl") or [] for p in t["players"]]
    out = {}
    for m in range(1, max((len(x) for x in lines), default=0) + 1):
        v = [x[m - 1] for x in lines if len(x) >= m and x[m - 1] is not None]
        if v:
            out[m] = sum(v) / len(v)
    return out


def game_panels(n: dict, gd: GameData) -> tuple[dict, list[str]]:
    """Team comparison bars, first objectives and the momentum chart of one game (blue left, red right)."""
    blue = next(t for t in n["teams"] if t["key"] == "BLUE")
    red = next(t for t in n["teams"] if t["key"] == "RED")
    tsum = lambda t, k: sum(_i(p.get(k)) for p in t["players"])  # noqa: E731
    cmp, parts = [], []
    for label, a, b in (("经济", blue.get("gold") or tsum(blue, "gold"), red.get("gold") or tsum(red, "gold")),
                        ("伤害", tsum(blue, "damage"), tsum(red, "damage")),
                        ("推塔", blue.get("towers"), red.get("towers")), ("小龙", blue.get("dragons"), red.get("dragons")),
                        ("男爵", blue.get("barons"), red.get("barons")),
                        ("插眼", tsum(blue, "wards"), tsum(red, "wards")), ("排眼", tsum(blue, "wards_killed"), tsum(red, "wards_killed"))):
        a, b = _i(a), _i(b)
        cmp.append({"label": label, "l": f"{a:,}", "r": f"{b:,}", "lp": round(a / (a + b) * 100) if a + b else 50,
                    "lead": "l" if a > b else "r" if b > a else ""})
        parts.append(f"{label} {a:,}:{b:,}")
    firsts = []
    players = [p for t in (blue, red) for p in t["players"]]
    fb = next((p for p in players if p.get("first_blood")), None)
    if fb:
        ft = blue if fb in blue["players"] else red
        firsts.append({"label": "一血", "who": f"{ft['label']} · {fb['game_name']}", "side": "blue" if ft is blue else "red",
                       "icon": gd.champ_icon(gd.champion(fb["champion_id"]))})
    for key, label, kind in (("first_tower", "一塔", "tower"), ("first_dragon", "一龙", "dragon"),
                             ("first_baron", "一男爵", "baron"), ("first_inhibitor", "一水晶", "inhibitor")):
        t = next((x for x in (blue, red) if x.get(key)), None)
        if t:
            firsts.append({"label": label, "who": t["label"], "side": "blue" if t is blue else "red", "icon": gd.obj_icon(kind, t is red)})
    block = {"cmp": cmp, "firsts": firsts, "mom": momentum(_curve(blue), _curve(red), blue["label"], red["label"])}
    lines = [f"{blue['label']} vs {red['label']}：" + " · ".join(parts)]
    if firsts:
        lines.append(" · ".join(f"{f['label']} {f['who']}" for f in firsts))
    return block, lines


def mvp_block(n: dict, gd: GameData) -> tuple[dict, list[str]] | None:
    win_t = next((t for t in n["teams"] if t.get("win")), None)
    mvp = pick_mvp(win_t["players"]) if win_t else None
    if not mvp:
        return None
    champ = gd.champion(mvp["champion_id"])
    tk = _i(win_t.get("kills")) or sum(_i(p.get("kills")) for p in win_t["players"])
    td = sum(_i(p.get("damage")) for p in win_t["players"])
    k, d, a = _i(mvp.get("kills")), _i(mvp.get("deaths")), _i(mvp.get("assists"))
    cname = gd.display_name(champ).split(" ")[0] if champ else ""
    stats = [{"k": "KDA", "v": f"{k}/{d}/{a}"}, {"k": "参团率", "v": f"{(k + a) / max(tk, 1) * 100:.0f}%"},
             {"k": "伤害占比", "v": f"{_i(mvp.get('damage')) / max(td, 1) * 100:.0f}%"}, {"k": "经济", "v": f"{_i(mvp.get('gold')):,}"}]
    if float(mvp.get("score") or 0) > 0:
        stats.append({"k": "评分", "v": f"{float(mvp['score']):.1f}"})
    blue = win_t["key"] == "BLUE"
    block = {"name": mvp["game_name"], "team": win_t["label"], "logo": win_t["logo"], "color": "blue" if blue else "red",
             "side_zh": "蓝队" if blue else "红队", "desc": " · ".join(x for x in [POS_ZH.get(mvp.get("position") or "", ""), cname] if x),
             "art": gd.splash(champ), "stats": stats}
    return block, [f"MVP {mvp['game_name']}（{win_t['label']} {block['desc']}）" + " · ".join(f"{s['k']} {s['v']}" for s in stats)]


def game_hero(g: dict, n: dict, games: list[dict] | None, tz: str) -> dict:
    """Scoreboard banner of one game: blue team left, red right, kills in the middle, series pips under the logos."""
    from .constants import league_backdrop, league_info, stage_zh
    from .fmt import duration, local_time

    home = g.get("homeTeam") or {}
    blue = next(t for t in n["teams"] if t["key"] == "BLUE")
    red = next(t for t in n["teams"] if t["key"] == "RED")
    hs, as_ = series_at(g, games, _i(n.get("set")))
    blue_home = (home.get("id") and blue["team_id"] == str(home.get("id"))) or blue["label"].lower() == str(home.get("acronym") or "").lower()
    bs, rs = (hs, as_) if blue_home else (as_, hs)
    bo = _i(g.get("numberOfGames"))
    need = bo // 2 + 1 if bo else 0
    done = bool(n.get("finished")) and (blue["win"] or red["win"])
    info = league_info(g.get("league"))

    def side(t: dict, key: str, score: int, left: bool) -> dict:
        chips = [{"text": "蓝队" if key == "blue" else "红队", "cls": key}]
        if done:
            res = {"text": "胜利" if t["win"] else "失败", "cls": "win" if t["win"] else "lose"}
            chips = chips + [res] if left else [res] + chips
        return _vteam(t["label"], t["logo"], SIDE_STYLE[key], score, need, chips, ("win" if t["win"] else "lose") if done else "")

    tsum = lambda t: _i(t.get("kills")) or sum(_i(p.get("kills")) for p in t["players"])  # noqa: E731
    return {
        "layout": "versus", "logo": info["logo"], "league": info["full"] or str(g.get("league") or ""),
        "stage": stage_zh(str(g.get("name") or "").split(":")[0].strip()),
        "when": local_time(n.get("created_at"), tz, "%Y-%m-%d %H:%M"), "bo": f"BO{bo}" if bo else "",
        "label": f"第 {_i(n.get('set'))} 局", "l": side(blue, "blue", bs, True), "r": side(red, "red", rs, False),
        "score": [str(tsum(blue)), str(tsum(red))], "cap": "击杀",
        "meta": [{"k": "时长", "v": duration(n.get("duration")), "cls": "dark"}] if done else [{"k": "进行中", "cls": "live"}],
        **league_backdrop(info),
    }


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
        doc.badge(headline, "accent hx")
    doc.hero = series_hero(g, tz, headline)
    row, line = versus_row(g, tz)
    doc.add_block("", "versus", {"rows": [row]}, [line], style="hide")  # the scoreboard banner shows it
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


def build_esports_game_view(g: dict, game: dict, gd: GameData, tz: str, created: float, games: list[dict] | None = None):
    """One game of a series; games (the whole series) give the series score at this game for the banner."""
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
    win_t = next((t for t in n["teams"] if t.get("win")), None)
    best = pick_mvp(win_t["players"] if win_t else [p for t in n["teams"] for p in t["players"]])
    if best and best.get("champion_name"):
        view.doc.tip(f"/lol 英雄 {best['champion_name']}", f"{best['game_name']} 所用英雄的出装")
    view.doc.link = g.get("details") or ""
    view.kind = "lol_esports_game"
    doc = view.doc
    if len(n["teams"]) == 2 and {t["key"] for t in n["teams"]} == {"BLUE", "RED"}:
        doc.hero = game_hero(g, n, games, tz)
        teams = [s for s in doc.sections if s.kind == "team"]
        if teams:
            teams[0].title = "选手数据"
        extra = []
        block, lines = game_panels(n, gd)
        extra.append(("esstats", block, lines))
        mv = mvp_block(n, gd)
        if mv:
            extra.append(("mvp", mv[0], mv[1]))
        head = []
        for kind, b, ls in extra:
            doc.add_block("", kind, b, ls)
            head.append(doc.sections.pop())
        doc.sections = head + doc.sections
    doc.width = 920
    return view
