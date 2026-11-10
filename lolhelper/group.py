"""Group play: roster, match ledger, daily / weekly boards with titles, live broadcasts
(duo queue / highlights / flops / streaks), rank changes, esports betting, champion quiz,
random pick and balanced team split.

Everything is keyed by the group session (unified message origin). Members are users who
bound an account or queried with their own binding in the group; they can leave with
/lol 下榜. Match data comes from the members' recent match lists and is kept in a small
per-account ledger so boards never need extra requests.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import itertools
import random
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from .constants import POSITIONS
from .doc import Cell, Doc
from .errors import LolHelperError, NotFoundError, UsageError
from .fmt import get_tz, local_time, parse_time, tier_emblem, tier_text
from .gamedata import norm
from .log import default_logger
from .storage import account_key, account_label

TIER_ORDER = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]
DIVS = {"IV": 0, "III": 1, "II": 2, "I": 3, 4: 0, 3: 1, 2: 2, 1: 3, "4": 0, "3": 1, "2": 2, "1": 3}
ROMAN = ["IV", "III", "II", "I"]
DEFAULT_SCORE = 1600  # 铂金 IV, used for players without a known rank
ROUND_SLOW = 3 * 3600  # poll interval for groups with broadcasts off (boards only)
BET_WIN, BET_SCORE = 10, 20
QUIZ_KINDS = {"技能": "skill", "技能图标": "skill", "原画": "splash", "皮肤": "splash", "技能名": "names"}

SendView = Callable[[str, Any], Awaitable[None]]
SendChain = Callable[[str, str, str | None], Awaitable[None]]


def _i(v) -> int:
    try:
        return int(float(v or 0))
    except (TypeError, ValueError):
        return 0


def _f(v) -> float | None:
    try:
        return None if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ ranks
def rank_score(r: dict | None) -> int | None:
    if not r or not r.get("tier"):
        return None
    t = str(r["tier"]).upper()
    if t not in TIER_ORDER:
        return None
    i = TIER_ORDER.index(t)
    lp = _i(r.get("lp"))
    if i >= 7:
        return 2800 + lp
    return i * 400 + DIVS.get(r.get("division"), 0) * 100 + min(lp, 100)


def score_label(score: float) -> str:
    """Approximate rank name of a score (used for team averages)."""
    s = int(score)
    if s >= 2800:
        return "大师+"
    t = TIER_ORDER[max(0, min(s // 400, 6))]
    return tier_text(t, ROMAN[max(0, min((s % 400) // 100, 3))])


def rank_label(r: dict | None) -> str:
    if not r or not r.get("tier"):
        return "未定级"
    apex = str(r["tier"]).upper() in ("MASTER", "GRANDMASTER", "CHALLENGER")
    return tier_text(r.get("tier"), r.get("division"), r.get("lp") if apex else None)


# ------------------------------------------------------------------ ledger rows
def ledger_row(m: dict) -> dict | None:
    dt = parse_time(m.get("created_at"))
    if not m.get("id") or not dt:
        return None
    secs = _i(m.get("duration"))
    secs = secs // 1000 if secs > 100000 else secs
    res = str(m.get("result") or "").upper()
    return {
        "id": str(m["id"]), "ts": dt.timestamp(), "res": res, "win": res == "WIN",
        "k": _i(m.get("kills")), "d": _i(m.get("deaths")), "a": _i(m.get("assists")),
        "cid": m.get("champion_id"), "champ": str(m.get("champion_name") or "").split(" ")[0],
        "pos": m.get("position"), "score": _f(m.get("score")), "rank": _i(m.get("score_rank")) or None,
        "multi": _i(m.get("multikill")), "queue": str(m.get("queue") or ""), "secs": secs, "cs": _i(m.get("cs")),
    }


def counts(row: dict) -> bool:
    """Boards and broadcasts skip remakes, Arena and games without a win / loss."""
    return row.get("secs", 0) >= 300 and row.get("res") in ("WIN", "LOSE") and not any(
        x in row.get("queue", "") for x in ("斗魂", "竞技场", "Arena"))


def highlights(row: dict) -> list[tuple[str, str]]:
    """(cls, text) events worth telling the group about."""
    k, d, a = row["k"], row["d"], row["a"]
    out: list[tuple[str, str]] = []
    if row.get("multi", 0) >= 5:
        out.append(("win", "五杀！"))
    elif row.get("multi", 0) == 4:
        out.append(("win", "四杀"))
    if k >= 15:
        out.append(("win", f"{k} 杀"))
    if d == 0 and k + a >= 15:
        out.append(("win", f"零死亡 {k}/{d}/{a}"))
    if d >= 12:
        out.append(("lose", f"阵亡 {d} 次"))
    elif d >= 10 and k + a <= 3:
        out.append(("lose", f"{k}/{d}/{a} 惨案"))
    return out


def current_streak(rows: list[dict]) -> tuple[int, bool | None]:
    n, kind = 0, None
    for r in sorted((r for r in rows if counts(r)), key=lambda r: r["ts"], reverse=True):
        if kind is None:
            kind = r["win"]
        if r["win"] != kind:
            break
        n += 1
    return n, kind


# ------------------------------------------------------------------ stats / boards
def member_stats(rows: list[dict], tz) -> dict:
    rows = sorted((r for r in rows if counts(r)), key=lambda r: r["ts"])
    st = {"games": len(rows), "wins": sum(r["win"] for r in rows)}
    st["losses"] = st["games"] - st["wins"]
    st["kills"], st["deaths"], st["assists"] = (sum(r[x] for r in rows) for x in ("k", "d", "a"))
    st["kda"] = (st["kills"] + st["assists"]) / max(st["deaths"], 1) if rows else 0
    st["winrate"] = st["wins"] / st["games"] if rows else 0
    scores = [r["score"] for r in rows if r.get("score")]
    st["score"] = sum(scores) / len(scores) if scores else None
    st["scored"] = len(scores)
    st["mvp"] = sum(1 for r in rows if r.get("rank") == 1 and r["win"])
    st["pentas"] = sum(1 for r in rows if r.get("multi", 0) >= 5)
    cspm = [r["cs"] / (r["secs"] / 60) for r in rows if r.get("secs") and r.get("pos") not in ("support", None)]
    st["cspm"] = sum(cspm) / len(cspm) if len(cspm) >= 2 else None
    st["night"] = sum(1 for r in rows if datetime.fromtimestamp(r["ts"], tz).hour < 5)
    best_w = best_l = cur = 0
    last = None
    for r in rows:
        cur = cur + 1 if r["win"] == last else 1
        last = r["win"]
        if r["win"]:
            best_w = max(best_w, cur)
        else:
            best_l = max(best_l, cur)
    st["win_streak"], st["lose_streak"] = best_w, best_l
    champs: dict[Any, int] = {}
    for r in rows:
        champs[r["cid"]] = champs.get(r["cid"], 0) + 1
    st["top_cid"] = max(champs, key=champs.get) if champs else None
    return st


BOARDS = [
    # key, title, value, text, needs min games
    ("games", "⏱️ 肝帝", lambda s: s["games"], lambda s: f"{s['games']} 局", False),
    ("winrate", "📈 胜率王", lambda s: s["winrate"], lambda s: f"{s['winrate'] * 100:.0f}% · {s['games']}局", True),
    ("kda", "⚔️ KDA 王", lambda s: s["kda"], lambda s: f"{s['kda']:.2f}", True),
    ("score", "⭐ 评分王", lambda s: s["score"] or 0, lambda s: f"{s['score']:.1f}" if s["score"] else "-", True),
    ("mvp", "👑 MVP 次数", lambda s: s["mvp"], lambda s: f"{s['mvp']} 次", False),
]
BOARD_HINTS = {"games": "对局数", "winrate": "胜率 · 局数", "kda": "平均 KDA", "score": "平均 OP 评分", "mvp": "全场最佳"}


def titles(members: list[dict], weekly: bool) -> list[dict]:
    m = 2.5 if weekly else 1
    rules = [
        ("🔥 连胜王", "win_streak", 3, "最长 {} 连胜", True),
        ("🗡️ 收割机", "kills", int(15 * m), "共 {} 杀", True),
        ("🛡️ 辅助之魂", "assists", int(25 * m), "共 {} 次助攻", True),
        ("🌟 五杀达人", "pentas", 1, "{} 次五杀", True),
        ("🦉 夜猫子", "night", 2, "凌晨打了 {} 局", True),
        ("🌾 补刀机器", "cspm", 8.0, "分均补刀 {:.1f}", True),
        ("💀 送分童子", "deaths", int(15 * m), "阵亡 {} 次", False),
        ("🧊 连败熔断", "lose_streak", 3, "最长 {} 连败", False),
    ]
    out = []
    for name, key, need, fmt, good in rules:
        cands = [x for x in members if x["stats"].get(key) is not None and x["stats"][key] >= need]
        if not cands:
            continue
        best = max(cands, key=lambda x: (x["stats"][key], -x["stats"]["games"]))
        out.append({"title": name, "member": best, "reason": fmt.format(best["stats"][key]), "good": good})
    return out


def board_rows(members: list[dict], key: str, weekly: bool) -> list[dict]:
    spec = next(b for b in BOARDS if b[0] == key)
    _, _, value, text, needs = spec
    min_games = 5 if weekly else 3
    pool = [x for x in members if not needs or x["stats"]["games"] >= min_games]
    if key in ("games", "mvp"):
        pool = [x for x in pool if value(x["stats"])]  # nobody wants "0 MVP" on a board
    if key == "score":
        pool = [x for x in pool if x["stats"]["scored"] >= min_games]
    pool.sort(key=lambda x: (value(x["stats"]), x["stats"]["winrate"], x["stats"]["games"]), reverse=True)
    return [{"member": x, "text": text(x["stats"])} for x in pool[:3]]


def brief_group(label: str, members: list[dict], ts: list[dict]) -> str:
    total = sum(x["stats"]["games"] for x in members)
    lines = [f"群榜单 {label}：{len(members)} 位群友共 {total} 局"]
    for x in sorted(members, key=lambda x: -x["stats"]["games"]):
        s = x["stats"]
        lines.append(f"·{x['label']}：{s['games']}局 {s['wins']}胜{s['losses']}负 胜率{s['winrate'] * 100:.0f}% KDA{s['kda']:.2f}"
                     + (f" 平均评分{s['score']:.1f}" if s["score"] else "") + f" MVP{s['mvp']}次 最长连胜{s['win_streak']} 最长连败{s['lose_streak']}"
                     + (f" 五杀{s['pentas']}次" if s["pentas"] else "") + (f" 常用 {x['top']}" if x.get("top") else ""))
    if ts:
        lines.append("称号：" + "；".join(f"{t['title']} {t['member']['label']}（{t['reason']}）" for t in ts))
    return "\n".join(lines)


# ------------------------------------------------------------------ team split
def split_teams(scores: list[float], rng: random.Random | None = None) -> tuple[list[int], list[int], float]:
    """Indices of two teams with the closest strength (sum for even sizes, average for odd)."""
    rng = rng or random.Random()
    n = len(scores)
    idx = list(range(n))
    best: list[tuple[float, tuple]] = []
    for combo in itertools.combinations(idx, n // 2):
        if n % 2 == 0 and 0 not in combo:
            continue  # mirror of another split
        a = list(combo)
        b = [i for i in idx if i not in combo]
        sa, sb = sum(scores[i] for i in a), sum(scores[i] for i in b)
        diff = abs(sa - sb) if n % 2 == 0 else abs(sa / len(a) - sb / len(b)) * (n / 2)
        best.append((diff, combo))
    lo = min(d for d, _ in best)
    pick = rng.choice([c for d, c in best if d <= lo + 60])  # some variety between close splits
    a = list(pick)
    b = [i for i in idx if i not in pick]
    diff = abs(sum(scores[i] for i in a) / len(a) - sum(scores[i] for i in b) / len(b))
    return a, b, diff


# ------------------------------------------------------------------ betting
def parse_bet(words: list[str]) -> tuple[int | None, str | None, tuple[int, int] | None]:
    import re

    index = team = score = None
    for w in words:
        m = re.fullmatch(r"(\d)\s*[:：比\-]\s*(\d)", w)
        if m:
            score = (int(m.group(1)), int(m.group(2)))
        elif w.isdigit() and index is None and int(w) < 50:
            index = int(w)
        elif w.lstrip("#").isdigit():
            index = int(w.lstrip("#"))
        else:
            team = w
    return index, team, score


def bet_result(bet: dict, home: str, away: str, hs: int, as_: int) -> tuple[bool, bool]:
    """(picked the winner, exact score). Scores in a bet are the picked team's : the opponent's."""
    win = home if hs > as_ else away if as_ > hs else ""
    team = str(bet.get("team") or "").upper()
    ok = bool(win) and team == win.upper()
    exact = False
    if ok and bet.get("score"):
        mine, theirs = (hs, as_) if team == home.upper() else (as_, hs)
        exact = tuple(bet["score"]) == (mine, theirs)
    return ok, exact


class GroupManager:
    def __init__(self, facade, send_view: SendView, send_chain: SendChain, logger: Any = None):
        self.f = facade
        self.send_view = send_view
        self.send_chain = send_chain
        self.logger = logger or default_logger
        # optional: async (kind, key, brief, style) -> review dict, used for weekly reports / broadcasts
        self.ai_comment: Callable[[str, str, str, str], Awaitable[dict | None]] | None = None
        self.quizzes: dict[str, dict] = {}
        self._last_round: dict[str, float] = {}
        self._polled: dict[str, float] = {}
        self._bets_checked = 0.0
        self._tasks: set[asyncio.Task] = set()
        self.keep_days = 16

    @property
    def s(self):
        return self.f.s

    # ------------------------------------------------------------ store
    @property
    def groups(self) -> dict:
        return self.f.store.data.setdefault("groups", {})

    @property
    def ledger(self) -> dict:
        return self.f.store.data.setdefault("ledger", {})

    @property
    def bets(self) -> dict:
        return self.f.store.data.setdefault("bets", {})

    def group(self, umo: str) -> dict:
        g = self.groups.setdefault(umo, {})
        for k, v in (("members", {}), ("optout", []), ("quiz", {}), ("bet", {}), ("sent", [])):
            g.setdefault(k, v.copy() if isinstance(v, (dict, list)) else v)
        return g

    def save(self) -> None:
        self.f.store._schedule_save()

    def touch(self, umo: str, user: str, nick: str = "") -> bool:
        """Add a user to the group's roster (default on). Returns True when newly added."""
        g = self.group(umo)
        if nick:
            g.setdefault("nicks", {})[user] = nick[:20]
        if user in g["optout"]:
            return False
        new = user not in g["members"]
        if new:
            g["members"][user] = {"joined": time.time()}
            self.save()
        return new

    def opt(self, umo: str, user: str, on: bool, nick: str = "") -> None:
        g = self.group(umo)
        if on:
            if user in g["optout"]:
                g["optout"].remove(user)
            self.touch(umo, user, nick)
        else:
            g["members"].pop(user, None)
            if user not in g["optout"]:
                g["optout"].append(user)
        self.save()

    def nick(self, umo: str, user: str) -> str:
        return (self.group(umo).get("nicks") or {}).get(user, "")

    def members(self, umo: str) -> list[tuple[str, dict]]:
        out = []
        for user in list(self.group(umo)["members"]):
            acc = self.f.store.default_account(user)
            if acc:
                out.append((user, acc))
        return out

    def broadcast_on(self, umo: str) -> bool:
        v = self.group(umo).get("broadcast")
        return self.s.group_broadcast if v is None else bool(v)

    def label(self, umo: str, user: str, acc: dict) -> str:
        nick = self.nick(umo, user)
        name = str(acc.get("game_name") or "?")
        return f"{nick}（{name}）" if nick and nick != name else name

    # ------------------------------------------------------------ polling
    async def poll(self, acc: dict) -> list[tuple[dict, dict]]:
        key = account_key(acc)
        led = self.ledger.setdefault(key, {"rows": [], "seen": []})
        first = "polled" not in led
        matches = await self.f.latest_matches(acc, 20 if first else 10)
        seen = set(led.get("seen") or [])
        known = {r["id"] for r in led["rows"]}
        new = []
        for m in matches:
            mid = str(m.get("id") or "")
            if not mid or mid in seen:
                continue
            row = ledger_row(m)
            if row and row["id"] not in known:
                led["rows"].append(row)
            new.append((m, row))
        ids = [str(m["id"]) for m in matches if m.get("id")]
        led["seen"] = (ids + [i for i in led.get("seen") or [] if i not in ids])[:80]
        cutoff = time.time() - self.keep_days * 86400
        led["rows"] = sorted((r for r in led["rows"] if r["ts"] >= cutoff), key=lambda r: r["ts"])[-300:]
        led["polled"] = time.time()
        return [] if first else new

    def _accounts(self, umos: list[str]) -> dict[str, tuple[dict, list[tuple[str, str]]]]:
        out: dict[str, tuple[dict, list[tuple[str, str]]]] = {}
        for umo in umos:
            for user, acc in self.members(umo):
                out.setdefault(account_key(acc), (acc, []))[1].append((umo, user))
        return out

    async def round(self, umos: list[str], fresh: float = 240, broadcast: bool = True) -> None:
        accounts = self._accounts(umos)
        results: dict[str, list] = {}
        sem = asyncio.Semaphore(3)

        async def one(key: str, acc: dict) -> None:
            if time.time() - self._polled.get(key, 0) < fresh:
                return
            async with sem:
                try:
                    results[key] = await self.poll(acc)
                except Exception as exc:
                    self.logger.info(f"[lolhelper] 群玩法检查 {account_label(acc)} 失败：{exc}")
                self._polled[key] = time.time()

        await asyncio.gather(*(one(k, a) for k, (a, _) in accounts.items()))
        self.save()
        if broadcast:
            for umo in umos:
                if self.broadcast_on(umo):
                    try:
                        await self.broadcast(umo, accounts, results)
                    except Exception as exc:
                        self.logger.warning(f"[lolhelper] 群友播报失败：{exc}")

    async def tick(self) -> None:
        s = self.s
        if not s.group_enabled:
            return
        now = time.time()
        due = []
        for umo in list(self.groups):
            if not self.group(umo)["members"]:
                continue
            every = s.group_broadcast_interval if self.broadcast_on(umo) else ROUND_SLOW
            if now - self._last_round.get(umo, 0) >= every:
                self._last_round[umo] = now
                due.append(umo)
        if due:
            await self.round(due)
        await self.check_boards()
        if now - self._bets_checked >= 300:
            self._bets_checked = now
            await self.check_bets()

    # ------------------------------------------------------------ broadcasts
    async def broadcast(self, umo: str, accounts: dict, results: dict) -> None:
        g = self.group(umo)
        sent = g["sent"]
        now = time.time()
        subscribed = {account_key((s.get("target") or {}).get("account") or {}) for s in self.f.store.subs(umo=umo, kind="match")}
        by_game: dict[str, list] = {}
        for key, (acc, owners) in accounts.items():
            for o_umo, user in owners:
                if o_umo != umo:
                    continue
                for m, row in results.get(key) or []:
                    if row and counts(row) and now - row["ts"] < 3 * 3600:
                        by_game.setdefault(row["id"], []).append((user, acc, m, row))
        pushed = 0
        for gid, ps in sorted(by_game.items(), key=lambda x: x[1][0][3]["ts"]):
            if gid in sent or pushed >= 3:
                continue
            events = []
            for user, acc, m, row in ps:
                name = self.label(umo, user, acc)
                events += [(cls, f"{name} {txt}") for cls, txt in highlights(row)]
                n, win = current_streak(self.ledger.get(account_key(acc), {}).get("rows") or [])
                if n in (5, 8, 10, 15) and self._latest(acc) == row["id"]:
                    events.append(("win" if win else "lose", f"{name} {n} 连{'胜' if win else '败'}"))
            if len(ps) < 2 and (not events or account_key(ps[0][1]) in subscribed):
                continue
            try:
                view = await self.game_card(umo, ps, events)
            except Exception as exc:
                self.logger.info(f"[lolhelper] 群友播报生成卡片失败：{exc}")
                continue
            sent.append(gid)
            pushed += 1
            await self.send_view(umo, view)
        g["sent"] = sent[-300:]
        self.save()

    def _latest(self, acc: dict) -> str | None:
        rows = [r for r in self.ledger.get(account_key(acc), {}).get("rows") or [] if counts(r)]
        return max(rows, key=lambda r: r["ts"])["id"] if rows else None

    async def full_game(self, acc: dict, m: dict) -> dict:
        if acc.get("kind") == "cn":
            return await self.f.cn.match_detail(acc, m)
        return (await self.f.svc.games_for([m], acc["game_name"], acc["tag_line"], acc["region"]))[0]

    async def game_card(self, umo: str, ps: list, events: list[tuple[str, str]]):
        from .game import build_game_view, game_hero
        from .render.html import strip_emoji

        user, acc, m, row = ps[0]
        game = await self.full_game(acc, m)
        ids = {(str(a.get("game_name") or "").lower(), str(a.get("tag_line") or "").lower()) for _, a, _, _ in ps}
        teams_of = {}
        for t in game.get("teams") or []:
            for p in t["players"]:
                if (str(p.get("game_name") or "").lower(), str(p.get("tag_line") or "").lower()) in ids:
                    p["is_target"] = True
                    teams_of[str(p.get("game_name")).lower()] = t
        view = build_game_view(game, self.f.gd, "cn" if acc.get("kind") == "cn" else "lol", self.s.timezone, time.time())
        doc = view.doc
        first = (str(acc.get("game_name") or "").lower(), str(acc.get("tag_line") or "").lower())
        doc.hero = game_hero(game, self.f.gd, self.s.timezone, focus=first) or doc.hero
        names = [self.label(umo, u, a) for u, a, _, _ in ps]
        if len(ps) >= 2:
            sides = {id(t) for t in teams_of.values()}
            if len(sides) >= 2:
                doc.title = "⚔️ 群友内战：" + " vs ".join(names)
            else:
                doc.title = f"🎮 {'、'.join(names)} 一起开黑 · {'赢了' if row['win'] else '输了'}"
        else:
            cls, txt = events[0]
            doc.title = ("✨ " if cls == "win" else "😱 ") + txt
        if doc.hero and len(ps) >= 2:
            doc.hero["sub"] = strip_emoji(doc.title)  # the single-player headline is already one of the event chips
        for cls, txt in events[:4]:
            doc.badges.insert(0, Cell(text=txt, cls=cls))
        doc.tips = []
        doc.tip("/lol 日榜", "今天的群榜单")
        if len(ps) >= 2:
            doc.tip("/lol 对比 @群友", "和群友比比近期数据")
        doc.tip("/lol 群播报 关", "关闭群友播报")
        view.kind = "group_broadcast"
        return view

    # ------------------------------------------------------------ boards
    def period(self, kind: str, offset: int = 0) -> tuple[datetime, datetime, str]:
        tz = get_tz(self.s.timezone)
        now = datetime.now(tz)
        day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if kind == "day":
            start = day0 - timedelta(days=offset)
            return start, start + timedelta(days=1), start.strftime("%m-%d")
        start = day0 - timedelta(days=day0.weekday()) - timedelta(weeks=offset)
        end = start + timedelta(days=7)
        return start, end, f"{start:%m-%d} ~ {end - timedelta(days=1):%m-%d}"

    def collect(self, umo: str, start: datetime, end: datetime) -> list[dict]:
        tz = get_tz(self.s.timezone)
        out = []
        for user, acc in self.members(umo):
            rows = [r for r in self.ledger.get(account_key(acc), {}).get("rows") or []
                    if start.timestamp() <= r["ts"] < end.timestamp()]
            st = member_stats(rows, tz)
            if not st["games"]:
                continue
            champ = self.f.gd.champion(st["top_cid"]) if st.get("top_cid") else None
            out.append({"user": user, "acc": acc, "stats": st, "label": self.label(umo, user, acc),
                        "name": str(acc.get("game_name") or "?"), "nick": self.nick(umo, user),
                        "short": self.nick(umo, user) or str(acc.get("game_name") or "?"),
                        "icon": self.f.gd.champ_icon(champ) if champ else None,
                        "art": self.f.gd.splash(champ) if champ else None,
                        "top": self.f.gd.display_name(champ).split(" ")[0] if champ else ""})
        return out

    def fav_champ(self, acc: dict | None) -> dict | None:
        """Most played champion in the games recorded for an account; None without records."""
        rows = (self.ledger.get(account_key(acc), {}).get("rows") or []) if acc else []
        cid = member_stats(rows, get_tz(self.s.timezone))["top_cid"] if rows else None
        return self.f.gd.champion(cid) if cid else None

    def podium_champs(self, umo: str, users: list[str]) -> list[dict | None]:
        """Most played champion of each user (group roster account, else their default binding)."""
        accs = dict(self.members(umo))
        return [self.fav_champ(accs.get(u) or self.f.store.default_account(u)) for u in users]

    async def solo_rank(self, acc: dict) -> dict | None:
        if acc.get("kind") == "cn":
            for q in await self.f.cn.ranked(acc):
                if "SOLO" in str(q.get("queue") or "").upper():
                    return {"tier": q.get("tier"), "division": q.get("division"), "lp": q.get("lp")}
            return None
        summ, _ = await self.f.svc._profile(acc["game_name"], acc["tag_line"], acc["region"], ttl=600)
        for ls in summ.get("league_stats") or []:
            ti = ls.get("tier_info") or {}
            if ls.get("game_type") == "SOLORANKED" and ti.get("tier"):
                return {"tier": ti["tier"], "division": ti.get("division"), "lp": ti.get("lp")}
        return None

    async def rank_changes(self, umo: str, since: float) -> list[dict]:
        sem = asyncio.Semaphore(3)
        members = self.members(umo)

        async def check(acc: dict) -> None:
            led = self.ledger.setdefault(account_key(acc), {"rows": [], "seen": []})
            if led.get("rank_ts", 0) >= since:
                return
            async with sem:
                try:
                    new = await self.solo_rank(acc)
                except Exception as exc:
                    self.logger.info(f"[lolhelper] 段位检查 {account_label(acc)} 失败：{exc}")
                    return
            old = led.get("rank")
            led["rank"], led["rank_ts"] = new, time.time()
            if old and new and (str(old.get("tier")), DIVS.get(old.get("division"), 0)) != (str(new.get("tier")), DIVS.get(new.get("division"), 0)):
                led["rank_change"] = {"old": old, "new": new, "ts": time.time()}

        await asyncio.gather(*(check(a) for _, a in members))
        self.save()
        out = []
        for user, acc in members:
            ch = self.ledger.get(account_key(acc), {}).get("rank_change")
            if ch and ch.get("ts", 0) >= since:
                up = (rank_score(ch["new"]) or 0) > (rank_score(ch["old"]) or 0)
                out.append({"label": self.label(umo, user, acc), "old": ch["old"], "new": ch["new"], "up": up})
        return out

    async def board_view(self, umo: str, kind: str = "day", offset: int = 0, refresh: bool = True,
                         with_ranks: bool = False, with_ai: bool = False):
        from .service import View

        if refresh:
            await self.round([umo], fresh=1200, broadcast=False)
        start, end, label = self.period(kind, offset)
        members = self.collect(umo, start, end)
        weekly = kind == "week"
        changes = await self.rank_changes(umo, start.timestamp()) if with_ranks else []
        if not members and not changes:
            return None
        word = "周榜" if weekly else "日榜"
        when = {("day", 0): "今日", ("day", 1): "昨日", ("week", 0): "本周", ("week", 1): "上周"}.get((kind, offset), "")
        doc = Doc(title=f"🏆 {when}群榜" if when else f"🏆 群{word}", subtitle=label, theme="group", width=820)
        doc.hero = {"layout": "center", "eyebrow": "WEEKLY RANKING" if weekly else "DAILY RANKING", "range": label}
        total = sum(x["stats"]["games"] for x in members)
        wins = sum(x["stats"]["wins"] for x in members)
        if members:
            doc.badge(f"{len(members)} 位群友上榜", "acc")
            doc.badge(f"共 {total} 局")
            doc.badge(f"总胜率 {wins / total * 100:.0f}%")
        ts = titles(members, weekly)
        if with_ai and self.ai_comment and members:
            try:
                out = await self.ai_comment("group", f"{umo}:{kind}:{start:%Y%m%d}", brief_group(f"{when} {label}", members, ts), "幽默")
                if out:
                    from .ai import attach
                    attach(doc, out)
                    doc.sections[-1].title = "AI 周报"
            except Exception as exc:
                self.logger.info(f"[lolhelper] AI 周报生成失败：{exc}")
        if members:
            pod = sorted(members, key=lambda x: (x["stats"]["wins"], x["stats"]["winrate"], x["stats"]["kda"]), reverse=True)[:3]
            doc.hero["art"] = pod[0].get("art")
            rows = [{"place": i + 1, "name": x["name"], "nick": x["nick"] if x["nick"] != x["name"] else "", "icon": x["icon"],
                     "value": f"{x['stats']['wins']} 胜", "sub": f"{x['stats']['wins']}胜{x['stats']['losses']}负 · KDA {x['stats']['kda']:.1f}"}
                    for i, x in enumerate(pod)]
            doc.add_block("胜场榜", "podium", {"rows": rows},
                          [f"{'🥇🥈🥉'[r['place'] - 1]} {r['name']}{('（' + r['nick'] + '）') if r['nick'] else ''} {r['sub']}" for r in rows])
            boards, lines = [], []
            for key, title, *_ in BOARDS:
                rs = board_rows(members, key, weekly)
                if not rs:
                    continue
                boards.append({"title": title, "hint": BOARD_HINTS.get(key, ""),
                               "rows": [{"name": r["member"]["short"], "icon": r["member"]["icon"], "value": r["text"]} for r in rs]})
                lines.append(f"{title}：" + "  ".join(f"{i + 1}.{r['member']['label']} {r['text']}" for i, r in enumerate(rs)))
            doc.add_block("分项榜", "boards", {"boards": boards}, lines)
            if ts:
                doc.add_tiles("称号", [Cell(text=t["title"], sub=f"{t['member']['short']} · {t['reason']}", img=t["member"]["icon"],
                                          cls="good" if t["good"] else "bad") for t in ts], style="medals")
        if changes:
            rows = [[c["label"], Cell(text=rank_label(c["old"]), img=tier_emblem(c["old"].get("tier"))),
                     Cell(text="⬆ 晋级" if c["up"] else "⬇ 掉段", cls="win" if c["up"] else "lose"),
                     Cell(text=rank_label(c["new"]), img=tier_emblem(c["new"].get("tier")), cls="win" if c["up"] else "lose")]
                    for c in changes]
            doc.add_table("段位变动（单双排）", ["群友", "原段位", "", "现段位"], rows, widths=["34%", "26%", "14%", "26%"])
        doc.footer = f"统计 {label} · 排除斗魂竞技场和重开局"
        doc.tip("/lol 周榜" if not weekly else "/lol 日榜", "本周榜单" if not weekly else "今天的榜单")
        doc.tip("/lol 对比 @群友", "和群友比比近期数据")
        doc.tip("/lol 下榜", "不想上榜")
        return View("group_board", {"umo": umo, "kind": kind}, doc, time.time())

    async def check_boards(self) -> None:
        tz = get_tz(self.s.timezone)
        now = datetime.now(tz)
        store = self.f.store
        for umo in list(self.groups):
            if not self.group(umo)["members"]:
                continue
            jobs = []
            if now.hour == self.s.group_daily_hour:
                jobs.append(("day", f"board:{umo}:d:{now:%Y%m%d}"))
            if now.weekday() == 6 and now.hour == self.s.group_weekly_hour:
                jobs.append(("week", f"board:{umo}:w:{now:%G%V}"))
            for kind, key in jobs:
                if store.state(key):
                    continue
                store.set_state(key, time.time())
                self._spawn(self.push_board(umo, kind))

    async def push_board(self, umo: str, kind: str) -> None:
        try:
            view = await self.board_view(umo, kind, with_ranks=kind == "day" and self.s.group_rank_change,
                                         with_ai=kind == "week" and self.s.group_ai_weekly)
            if view is not None:
                await self.send_view(umo, view)
        except Exception as exc:
            self.logger.warning(f"[lolhelper] 推送群榜单失败：{exc}")

    def _spawn(self, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------ betting
    async def upcoming(self, leagues: list[str] | None) -> list[dict]:
        games, _ = await self.f.svc.esports_games("schedule", leagues)
        now = time.time()
        out = []
        for g in games:
            st = parse_time(g.get("scheduledAt"))
            if not st or st.timestamp() <= now or st.timestamp() > now + 3 * 86400:
                continue
            if not (g.get("homeTeam") or {}).get("acronym") or not (g.get("awayTeam") or {}).get("acronym"):
                continue
            out.append(g)
        return sorted(out, key=lambda g: parse_time(g["scheduledAt"]).timestamp())[:10]

    def bet_list_view(self, umo: str, games: list[dict]):
        from .constants import league_backdrop, league_info
        from .render.html import rgba
        from .service import View, versus_row

        doc = Doc(title="🎲 赛事竞猜", subtitle="开赛前可以竞猜和修改", theme="group", width=820)
        doc.hero = {"layout": "center", "eyebrow": "PREDICTION", "title": "赛事竞猜", "range": "未来 3 天的比赛 · 开赛前可以竞猜和修改"}
        if games:  # poster of the first match's league
            info = league_info(games[0].get("league"))
            doc.hero.update(league_backdrop(info))
            doc.hero["vivid"] = bool(doc.hero.get("art"))
            if info["color"]:
                doc.hero["pc"] = rgba(info["color"], .34)
        rows, lines = [], []
        for i, g in enumerate(games, 1):
            b = (self.bets.get(str(g.get("id"))) or {}).get("umos", {}).get(umo, {})
            h, a = g["homeTeam"]["acronym"], g["awayTeam"]["acronym"]
            nh = sum(1 for x in b.values() if str(x["team"]).upper() == h.upper())
            na = sum(1 for x in b.values() if str(x["team"]).upper() == a.upper())
            v, line = versus_row(g, self.s.timezone)
            v["no"] = i
            v["extra"] = [{"k": "押注", "v": f"{nh} : {na}", "cls": "dark"}] if b else [{"k": "暂无押注", "cls": "dark"}]
            rows.append(v)
            lines.append(f"#{i} {line} · 押注 {nh}:{na}" if b else f"#{i} {line}")
        doc.badge(f"{len(games)} 场可竞猜", "acc")
        doc.add_block("", "versus", {"rows": rows}, lines)
        doc.footer = f"猜中胜方 +{BET_WIN} 分，比分也猜中再 +{BET_SCORE} 分"
        if games:
            g = games[0]
            doc.tip(f"/lol 竞猜 1 {g['homeTeam']['acronym']}", "押第 1 场的主队")
            doc.tip(f"/lol 竞猜 {g['homeTeam']['acronym']} 3:1", "连比分一起猜")
        doc.tip("/lol 竞猜榜", "本群竞猜积分")
        return View("group_bets", {"games": games}, doc, time.time())

    async def place_bet(self, umo: str, user: str, nick: str, words: list[str], leagues: list[str] | None) -> str:
        index, team, score = parse_bet(words)
        games = await self.upcoming(leagues if index else None) if index else await self.upcoming(None)
        if index:
            if not 1 <= index <= len(games):
                raise UsageError(f"没有第 {index} 场可竞猜的比赛，发送 /lol 竞猜 查看列表")
            g = games[index - 1]
        else:
            if not team:
                raise UsageError("用法：/lol 竞猜 <序号|战队> [战队] [比分]，例如 /lol 竞猜 BLG 3:1")
            g = next((x for x in games if team.upper() in (x["homeTeam"]["acronym"].upper(), x["awayTeam"]["acronym"].upper())), None)
            if not g:
                raise NotFoundError(f"未来 3 天没有找到 {team} 的比赛")
        h, a = g["homeTeam"]["acronym"], g["awayTeam"]["acronym"]
        team = team or ""
        if team.upper() not in (h.upper(), a.upper()):
            raise UsageError(f"这场是 {h} vs {a}，请写其中一队，例如 /lol 竞猜 {index or ''} {h}".replace("  ", " "))
        team = h if team.upper() == h.upper() else a
        need = int(g.get("numberOfGames") or 1) // 2 + 1
        if score and (max(score) != need or min(score) >= need or score[0] != need):
            raise UsageError(f"比分写你押的队在前，BO{g.get('numberOfGames') or '?'} 的胜方比分应为 {need}，例如 {need}:{max(need - 1, 0)}")
        st = parse_time(g["scheduledAt"]).timestamp()
        b = self.bets.setdefault(str(g["id"]), {"start": st, "home": h, "away": a, "league": g.get("league"),
                                                "name": g.get("name"), "home_id": g["homeTeam"].get("id"),
                                                "away_id": g["awayTeam"].get("id"), "umos": {}})
        b["start"] = st
        b["umos"].setdefault(umo, {})[user] = {"team": team, "score": list(score) if score else None, "nick": nick, "ts": time.time()}
        self.save()
        when = local_time(g["scheduledAt"], self.s.timezone)
        guess = f" {score[0]}:{score[1]}" if score else ""
        return f"✅ 已竞猜 {h} vs {a}（{when}）：{team} 胜{guess}。开赛前可以重新发送修改。"

    async def check_bets(self) -> None:
        from .esports import match_to_schedule

        if self.f.svc.esp is None:
            return
        now = time.time()
        for mid, b in list(self.bets.items()):
            if now < b.get("start", 0) + 1800:
                continue
            if now - b.get("start", 0) > 4 * 86400:
                self.bets.pop(mid, None)
                continue
            try:
                m = await self.f.svc.esp.match(mid)
            except Exception as exc:
                self.logger.info(f"[lolhelper] 竞猜结算获取比赛失败：{exc}")
                continue
            if not m or str(m.get("status") or "").lower() not in ("finished", "completed", "ended"):
                continue
            hs, as_ = _i(m.get("homeScore")), _i(m.get("awayScore"))
            for umo, users in (b.get("umos") or {}).items():
                await self.settle(umo, b, users, hs, as_, match_to_schedule(m))
            self.bets.pop(mid, None)
        self.save()

    async def settle(self, umo: str, b: dict, users: dict, hs: int, as_: int, match: dict | None = None) -> None:
        from .esports import series_hero
        from .service import View

        g = self.group(umo)
        hits, misses = [], 0
        for user, bet in users.items():
            ok, exact = bet_result(bet, b["home"], b["away"], hs, as_)
            rec = g["bet"].setdefault(user, {"pts": 0, "hit": 0, "total": 0})
            rec["total"] += 1
            rec["nick"] = bet.get("nick") or rec.get("nick", "")
            if ok:
                pts = BET_WIN + (BET_SCORE if exact else 0)
                rec["pts"] += pts
                rec["hit"] += 1
                pick = bet["team"] + (f" {bet['score'][0]}:{bet['score'][1]}" if bet.get("score") else "")
                hits.append((bet.get("nick") or user.split(":")[-1][-6:], pts, exact, pick))
            else:
                misses += 1
        doc = Doc(title=f"🎲 竞猜结算 · {b['home']} {hs}:{as_} {b['away']}", subtitle=f"{len(users)} 人参与", theme="group",
                  width=920 if match else 820)  # the scoreboard banner needs the width of the esports cards
        if match:
            doc.hero = series_hero(match, self.s.timezone, "竞猜结算")
        doc.badge(f"{len(users)} 人参与", "acc")
        doc.badge(f"猜中 {len(hits)} 人")
        if misses:
            doc.badge(f"猜错 {misses} 人")
        if hits:
            doc.add_table("猜中的群友", ["群友", "竞猜", "得分", ""],
                          [[n, pick, Cell(text=f"+{p}", cls="gold" if e else "accent"), "🎯 比分全中" if e else ""] for n, p, e, pick in hits],
                          widths=["34%", "26%", "16%", "24%"])
        else:
            doc.add_text("", "这场没有群友猜中")
        doc.tip("/lol 竞猜榜", "本群竞猜积分")
        doc.tip("/lol 竞猜", "继续竞猜下一场")
        await self.send_view(umo, View("group_bet_result", {}, doc, time.time()))

    def bet_board_view(self, umo: str):
        from .service import View

        g = self.group(umo)
        rows = sorted(g["bet"].items(), key=lambda x: (-x[1]["pts"], -x[1]["hit"]))[:15]
        if not rows:
            raise NotFoundError("本群还没有结算过的竞猜，发送 /lol 竞猜 开始")
        doc = Doc(title="🎲 竞猜积分榜", theme="group", width=760)
        doc.hero = {"layout": "center", "eyebrow": "PREDICTION RANKING", "range": f"猜中胜方 +{BET_WIN} 分 · 比分全中再 +{BET_SCORE} 分"}
        names = [r.get("nick") or u.split(":")[-1][-6:] for u, r in rows]
        doc.badge(f"{len(rows)} 位群友上榜", "acc")
        champs = self.podium_champs(umo, [u for u, _ in rows[:3]])
        self._podium_art(doc, champs)
        pod = [{"place": i + 1, "name": names[i], "nick": "", "icon": self.f.gd.champ_icon(champs[i]), "value": f"{r['pts']} 分",
                "sub": f"猜中 {r['hit']}/{r['total']}"}
               for i, (u, r) in enumerate(rows[:3])]
        doc.add_block("", "podium", {"rows": pod}, [f"{'🥇🥈🥉'[p['place'] - 1]} {p['name']} {p['value']} · {p['sub']}" for p in pod])
        doc.add_table("", ["#", "群友", "积分", "猜中"],
                      [[f"{i + 1}", names[i], str(r["pts"]), f"{r['hit']}/{r['total']}"] for i, (u, r) in enumerate(rows) if i >= 3],
                      widths=["12%", "48%", "20%", "20%"])
        doc.tip("/lol 竞猜", "继续竞猜")
        doc.tip("/lol 赛程", "近期赛程")
        doc.tip("/lol 榜单 猜英雄", "本群猜英雄积分")
        return View("group_bet_board", {}, doc, time.time())

    def _podium_art(self, doc: Doc, champs: list[dict | None]) -> None:
        """Banner art of a ranking: the splash of the winner's most played champion, else the plugin mark poster."""
        art = self.f.gd.splash(champs[0]) if champs and champs[0] else None
        doc.hero.update({"art": art} if art else {"wm": "mark"})

    # ------------------------------------------------------------ quiz
    async def _image(self, url: str, mode: str, tag: str) -> str:
        from PIL import Image

        got = await self.f._fetch_icon(url)
        if not got:
            raise LolHelperError("题目图片下载失败，请再试一次")
        im = Image.open(io.BytesIO(got[0])).convert("RGB")
        if mode == "crop":
            w, h = im.size
            cw, ch = int(w * 0.26), int(h * 0.32)
            x = random.randint(int(w * 0.18), max(int(w * 0.82) - cw, int(w * 0.18)))
            y = random.randint(int(h * 0.08), max(int(h * 0.72) - ch, int(h * 0.08)))
            im = im.crop((x, y, x + cw, y + ch)).resize((480, int(480 * ch / cw)), Image.LANCZOS)
        else:
            im = im.resize((220, 220), Image.LANCZOS)
            pad = Image.new("RGB", (260, 260), (32, 34, 38))
            pad.paste(im, (20, 20))
            im = pad
        out = self.f.data_dir / "render" / f"quiz_{tag}_{int(time.time() * 1000)}.jpg"
        out.parent.mkdir(parents=True, exist_ok=True)
        im.save(out, quality=88)
        return str(out)

    async def quiz_start(self, umo: str, kind_word: str | None = None) -> tuple[str, str | None]:
        if umo in self.quizzes:
            raise UsageError("本群已经有一道题在进行，直接发英雄名作答，或发送 /lol 不猜了 公布答案")
        gd = self.f.gd
        await gd.ensure_lol()
        champs = [c for c in (gd.lol.get("champions") or {}).values() if c.get("key")]
        if not champs:
            raise LolHelperError("英雄数据还没加载好，请稍后再试")
        kind = QUIZ_KINDS.get(kind_word or "") or random.choice(["skill", "skill", "splash", "names"])
        tag = hashlib.md5(umo.encode()).hexdigest()[:8]
        secs = self.s.group_quiz_seconds
        for _ in range(4):
            c = random.choice(champs)
            spells = await gd.champion_spells(c) if kind in ("skill", "names") else []
            if kind in ("skill", "names") and len(spells) < 4:
                continue
            break
        name = gd.display_name(c)
        title = str(c.get("title_zh") or name.split(" ")[-1])
        if kind == "skill":
            s = random.choice(spells)
            img = await self._image(s["icon"], "icon", tag)
            text = f"🧩 猜英雄：这是哪位英雄的 {s['key']} 技能？直接在群里发英雄名作答，{secs} 秒内有效"
            hint = f"💡 提示：技能名叫「{s['name']}」"
        elif kind == "splash":
            img = await self._image(f"{gd.ddragon}/cdn/img/champion/splash/{c['key']}_0.jpg", "crop", tag)
            text = f"🧩 猜英雄：这块原画局部是哪位英雄？直接在群里发英雄名作答，{secs} 秒内有效"
            hint = f"💡 提示：英雄名 {len(title)} 个字，第一个字是「{title[0]}」"
        else:
            img = None
            text = "🧩 猜英雄：这四个技能属于哪位英雄？\n" + "\n".join(f"{s['key']}「{s['name']}」" for s in spells[:4]) + f"\n直接在群里发英雄名作答，{secs} 秒内有效"
            hint = f"💡 提示：英雄名 {len(title)} 个字，第一个字是「{title[0]}」"
        q = {"cid": str(c.get("id")), "name": name, "icon": gd.champ_icon(c), "kind": kind, "start": time.time(),
             "splash": f"{gd.ddragon}/cdn/img/champion/splash/{c['key']}_0.jpg", "hint": hint, "files": [img] if img else []}
        self.quizzes[umo] = q
        q["task"] = asyncio.get_running_loop().create_task(self._quiz_timer(umo, q, secs))
        return text, img

    async def _quiz_timer(self, umo: str, q: dict, secs: int) -> None:
        try:
            await asyncio.sleep(secs / 2)
            if self.quizzes.get(umo) is q:
                await self.send_chain(umo, q["hint"], None)
            await asyncio.sleep(secs / 2)
            if self.quizzes.get(umo) is q:
                self.quiz_end(umo)
                await self.send_chain(umo, f"⌛ 时间到，没人猜出来。答案是 {q['name']}", q["icon"])
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.logger.info(f"[lolhelper] 猜英雄计时出错：{exc}")

    def quiz_end(self, umo: str) -> dict | None:
        q = self.quizzes.pop(umo, None)
        if q:
            task = q.get("task")
            if task and not task.done() and task is not asyncio.current_task():
                task.cancel()
            for p in q.get("files") or []:
                try:
                    import os
                    os.remove(p)
                except OSError:
                    pass
        return q

    def quiz_check(self, umo: str, text: str, user: str, nick: str) -> dict | None:
        """The quiz if the text names its champion exactly (names, titles, nicknames), else None."""
        q = self.quizzes.get(umo)
        if not q:
            return None
        cid = self.f.gd._champ_index.get(norm(text))
        if cid is None or str(cid) != q["cid"]:
            return None
        self.quiz_end(umo)
        rec = self.group(umo)["quiz"].setdefault(user, {"pts": 0})
        rec["pts"] += 1
        if nick:
            rec["nick"] = nick
        self.save()
        q["secs"] = round(time.time() - q["start"], 1)
        q["pts"] = rec["pts"]
        return q

    def quiz_board_view(self, umo: str):
        from .service import View

        rows = sorted(self.group(umo)["quiz"].items(), key=lambda x: -x[1]["pts"])[:15]
        if not rows:
            raise NotFoundError("本群还没人答对过，发送 /lol 猜英雄 出题")
        doc = Doc(title="🧩 猜英雄排行榜", theme="group", width=760)
        doc.hero = {"layout": "center", "eyebrow": "QUIZ RANKING", "range": "技能图标 · 原画 · 技能名"}
        names = [r.get("nick") or u.split(":")[-1][-6:] for u, r in rows]
        doc.badge(f"{len(rows)} 位群友上榜", "acc")
        doc.badge(f"共答对 {sum(r['pts'] for _, r in rows)} 题")
        champs = self.podium_champs(umo, [u for u, _ in rows[:3]])
        self._podium_art(doc, champs)
        pod = [{"place": i + 1, "name": names[i], "nick": "", "icon": self.f.gd.champ_icon(champs[i]), "value": f"{r['pts']} 题", "sub": "答对"}
               for i, (u, r) in enumerate(rows[:3])]
        doc.add_block("", "podium", {"rows": pod}, [f"{'🥇🥈🥉'[p['place'] - 1]} {p['name']} 答对 {p['value']}" for p in pod])
        doc.add_table("", ["#", "群友", "答对"], [[f"{i + 1}", names[i], str(r["pts"])] for i, (u, r) in enumerate(rows) if i >= 3],
                      widths=["12%", "60%", "28%"])
        doc.tip("/lol 猜英雄", "再来一题")
        doc.tip("/lol 猜英雄 原画", "看原画局部猜英雄")
        doc.tip("/lol 榜单 竞猜", "本群竞猜积分")
        return View("group_quiz_board", {}, doc, time.time())

    # ------------------------------------------------------------ random pick
    async def random_pick(self, position: str | None) -> tuple[str, str, dict]:
        pos = position or random.choice(list(POSITIONS))
        view = await self.f.svc.tier(pos, limit=15)
        rows = (view.data.get("positions") or {}).get(pos) or []
        cands = [r for r in rows if _i(r.get("tier")) in (1, 2)] or rows[:10]
        if not cands:
            raise NotFoundError("没有拿到梯队数据，请稍后再试")
        r = random.choice(cands)
        return r.get("champion") or "", pos, r

    # ------------------------------------------------------------ team split
    async def split_view(self, players: list[dict]):
        from .service import View

        if not 4 <= len(players) <= 10:
            raise UsageError("分队需要 4~10 人：/lol 分队 @甲 @乙 …，也可以直接写名字")
        sem = asyncio.Semaphore(3)

        async def rank(p: dict) -> None:
            if not p.get("acc"):
                return
            async with sem:
                try:
                    p["rank"] = await self.solo_rank(p["acc"])
                except Exception as exc:
                    self.logger.info(f"[lolhelper] 分队查询段位失败：{exc}")

        await asyncio.gather(*(rank(p) for p in players))
        known = sorted(s for s in (rank_score(p.get("rank")) for p in players) if s is not None)
        default = known[len(known) // 2] if known else DEFAULT_SCORE
        for p in players:
            s = rank_score(p.get("rank"))
            p["score"], p["guess"] = (s, False) if s is not None else (default, True)
        a, b, diff = split_teams([p["score"] for p in players])
        teams, arts = [], []
        for label, idx in (("🔵 蓝队", a), ("🔴 红队", b)):
            ms = sorted((players[i] for i in idx), key=lambda p: -p["score"])
            avg = sum(p["score"] for p in ms) / len(ms)
            teams.append({"label": label, "avg": f"平均 {score_label(avg)}", "members": [
                {"name": p["name"], "rank": rank_label(p.get("rank")) if not p["guess"] else f"按 {score_label(p['score'])} 估算",
                 "img": tier_emblem((p.get("rank") or {}).get("tier")), "guess": p["guess"]} for p in ms]})
            # banner art: the most played champion of the team's highest-ranked member who has recorded games
            champ = next((c for c in (self.fav_champ(p.get("acc")) for p in ms) if c), None)
            arts.append(self.f.gd.splash(champ) if champ else None)
        doc = Doc(title="⚖️ 内战分队", subtitle=f"{len(players)} 人 · 按单双排段位平衡", theme="group", width=760)
        doc.hero = {"layout": "center", "eyebrow": "TEAM SPLIT", "duo": True, "arts": arts}
        doc.badge(f"平均实力差 {diff:.0f} 分", "acc")
        lines = [f"{t['label']}（{t['avg']}）：" + "、".join(f"{m['name']}[{m['rank']}]" for m in t["members"]) for t in teams]
        doc.add_block("", "split", {"teams": teams}, lines)
        if any(p["guess"] for p in players):
            doc.footer = "未绑定或未定级的群友按其他人的中位数估算 · 每次分队会在实力相近的方案里随机挑一种"
        else:
            doc.footer = "每次分队会在实力相近的方案里随机挑一种"
        doc.tip("/lol 分队 …", "再分一次")
        doc.tip("/lol 对比 @甲 @乙", "两位群友的近期数据 PK")
        doc.tip("/lol 榜单", "今天的群榜单")
        return View("group_split", {}, doc, time.time())


# ------------------------------------------------------------------ PK
def pk_view(a: dict, b: dict, gd):
    """a / b: {"label", "acc", "rows", "st"} from Facade.ai_recent_brief."""
    from .ai import METRICS, fmt_metric
    from .service import View

    doc = Doc(title=f"⚔️ {a['label']} vs {b['label']}", subtitle=f"最近 {a['st']['n']} 局 vs {b['st']['n']} 局", theme="group", width=820)
    heads, sides = [], []
    for x in (a, b):
        st = x["st"]
        top = st["champs"][0][1] if st["champs"] else None
        champ = gd.champion(top["id"]) if top and top.get("id") else None
        heads.append({"name": x["label"], "sub": account_label(x["acc"]), "record": f"{st['wins']}胜{st['n'] - st['wins']}负",
                      "icon": gd.champ_icon(champ) if champ else None})
        sides.append({"name": x["label"], "sub": account_label(x["acc"]), "icon": heads[-1]["icon"], "art": gd.splash(champ),
                      "chips": [{"text": f"近 {st['n']} 局 {st['wins']}胜{st['n'] - st['wins']}负"}]})
    rows, lines, score = [], [], [0, 0]
    for key, label, top_v, higher, _ in METRICS:
        va, vb = a["st"]["metrics"][key][0], b["st"]["metrics"][key][0]
        if va is None or vb is None:
            continue
        win = ""
        if abs(va - vb) > 1e-9:
            win = "l" if (va > vb) == higher else "r"
            score[0 if win == "l" else 1] += 1
        rows.append({"label": label, "l": fmt_metric(key, va), "r": fmt_metric(key, vb), "win": win,
                     "lw": round(min(va / top_v, 1) * 100), "rw": round(min(vb / top_v, 1) * 100)})
        lines.append(f"{label}：{fmt_metric(key, va)} vs {fmt_metric(key, vb)}" + (" ◀" if win == "l" else " ▶" if win == "r" else ""))
    doc.badge(f"{a['label']} 胜 {score[0]} 项", "hx")
    doc.badge(f"{b['label']} 胜 {score[1]} 项", "hx")
    doc.hero = {"layout": "duel", "eyebrow": "PLAYER VS PLAYER", "l": sides[0], "r": sides[1], "score": score, "cap": "胜出项目"}
    doc.add_block("", "pk", {"heads": heads, "rows": rows}, lines)
    doc.tip("/lol 诊断", "看自己的近期诊断")
    doc.tip("/lol 日榜", "今天的群榜单")
    return View("group_pk", {}, doc, time.time())

