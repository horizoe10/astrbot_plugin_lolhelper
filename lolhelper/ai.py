"""AI reviews: compact data briefs -> configured LLM -> structured review block.

The LLM call is injected (`complete(system, prompt, umo) -> (text, model)`) so this module
does not depend on AstrBot. Every brief only contains numbers the plugin really has; the
system prompt forbids the model from inventing anything beyond them.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Awaitable, Callable

from .cache import TTLCache
from .constants import POSITIONS
from .errors import LolHelperError
from .fmt import big, duration
from .log import default_logger

DEFAULT_STYLE = "专业"
STYLES = {
    "专业": "解说与教练口吻，客观冷静，用数据说话，问题要具体，建议要可执行。",
    "幽默": "轻松幽默，可以用游戏梗和俏皮的比喻调侃，但不贬低任何人。",
    "锐评": "直接犀利、一针见血、不绕弯子，可以尖锐地批评数据和决策；不做人身攻击，不说脏话，不涉及外貌、地域等与比赛无关的内容。",
}
STYLE_ALIASES = {
    "专业": "专业", "pro": "专业", "professional": "专业", "正经": "专业",
    "幽默": "幽默", "搞笑": "幽默", "fun": "幽默", "funny": "幽默",
    "锐评": "锐评", "犀利": "锐评", "毒舌": "锐评", "sharp": "锐评",
}

KINDS = {
    # kind: (role, what the three fields mean, labels shown on the card)
    "game": ("英雄联盟教练", "highlights 写点评对象这局的亮点，issues 写问题，advice 写一条下局就能用的改进建议",
             ("亮点", "问题", "建议")),
    "recent": ("英雄联盟教练", "highlights 写近期的优势，issues 写短板，advice 写一条训练或选英雄建议",
               ("优势", "短板", "建议")),
    "esports_game": ("英雄联盟赛事解说", "highlights 写本局的关键表现（点名选手），issues 写败方或个人的问题，advice 写本局胜负手",
                     ("亮点", "问题", "胜负手")),
    "pk": ("英雄联盟解说兼裁判", "summary 直接判定谁近期更强，highlights 写胜方的优势（点名），issues 写败方的短板（点名），advice 给败方一条追赶建议",
           ("优势", "短板", "裁判建议")),
    "group": ("英雄联盟群聊周报编辑", "summary 用一句话概括本群这段时间的战况，highlights 点名表扬 2-3 位群友，issues 点名调侃 1-2 位群友的翻车，advice 写一句给全群的寄语",
              ("表扬", "翻车", "寄语")),
    "series": ("英雄联盟赛事解说", "highlights 写整场系列赛的关键表现（点名选手），issues 写败方的问题，advice 写整场总结",
               ("关键表现", "问题", "总结")),
}

CompleteFn = Callable[[str, str, Any], Awaitable[tuple[str, str]]]
GateFn = Callable[[], None]


class AiError(LolHelperError):
    pass


def pick_style(words: list[str]) -> tuple[str | None, list[str]]:
    style, rest = None, []
    for w in words:
        s = STYLE_ALIASES.get(w.lower())
        if s and style is None:
            style = s
        else:
            rest.append(w)
    return style, rest


# ------------------------------------------------------------------ helpers
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


def game_secs(game: dict) -> int:
    s = _i(game.get("duration"))
    return s // 1000 if s > 100000 else s


def _ratio(a: float, b: float) -> float | None:
    return a / b if b else None


def _p(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def _pos(p: dict) -> str:
    return POSITIONS.get(p.get("position") or "", "")


def _name(p: dict) -> str:
    return p.get("game_name") or p.get("champion_name") or "?"


def _champ(p: dict) -> str:
    return str(p.get("champion_name") or "").split(" ")[0]


def _team_label(t: dict) -> str:
    return t.get("label") or {"BLUE": "蓝方", "RED": "红方"}.get(str(t.get("key") or "").upper(), "")


def _team_totals(t: dict) -> dict:
    ps = t.get("players") or []
    return {
        "kills": _i(t.get("kills")) or sum(_i(p.get("kills")) for p in ps),
        "deaths": sum(_i(p.get("deaths")) for p in ps),
        "damage": sum(_i(p.get("damage")) for p in ps),
        "taken": sum(_i(p.get("taken")) for p in ps),
        "gold": _i(t.get("gold")) or sum(_i(p.get("gold")) for p in ps),
    }


def _multi(p: dict) -> str:
    m = p.get("multi") or {}
    parts = [f"{label}{m[k]}" for k, label in (("penta", "五杀×"), ("quadra", "四杀×"), ("triple", "三杀×"), ("double", "双杀×")) if _i(m.get(k))]
    if not parts and _i(p.get("multikill")) >= 2:
        parts.append({2: "双杀", 3: "三杀", 4: "四杀", 5: "五杀"}.get(_i(p.get("multikill")), "多杀"))
    return " ".join(parts)


def player_line(p: dict, t: dict, mins: float, gd=None, mark: bool = False, items: bool = False) -> str:
    tot = _team_totals(t)
    k, d, a = _i(p.get("kills")), _i(p.get("deaths")), _i(p.get("assists"))
    bits = [f"{k}/{d}/{a}", f"参团{_p(_ratio(k + a, tot['kills']))}"]
    if p.get("damage") is not None:
        bits.append(f"伤害{big(p.get('damage'))}({_p(_ratio(_i(p.get('damage')), tot['damage']))})")
    if p.get("taken") is not None:
        bits.append(f"承伤{big(p.get('taken'))}")
    if p.get("gold") is not None:
        bits.append(f"经济{big(p.get('gold'))}")
    if p.get("cs") is not None:
        bits.append(f"补刀{_i(p.get('cs'))}({_i(p.get('cs')) / mins:.1f}/分)")
    if p.get("wards") is not None:
        bits.append(f"插眼{_i(p.get('wards'))}" + (f"/排眼{_i(p.get('wards_killed'))}" if p.get("wards_killed") is not None else ""))
    s = _f(p.get("score"))
    if s is not None and (s or p.get("score_tl")):
        bits.append(f"评分{s:.1f}" + (f"(第{_i(p.get('score_rank'))})" if p.get("score_rank") else ""))
    extra = [x for x in [_multi(p), "一血" if p.get("first_blood") else "", "一塔" if p.get("first_tower") else ""] if x]
    if extra:
        bits.append("、".join(extra))
    its = [i for i in p.get("items") or [] if i] + ([p["role_item"]] if _i(p.get("role_item")) else [])
    if items and gd is not None and its:
        bits.append("出装：" + "/".join(gd.item_name(i) for i in its))
    head = f"{'★' if mark else '·'}{_pos(p) or '-'} {_name(p)} {_champ(p)}"
    return head + "：" + " ".join(bits)


def team_line(t: dict) -> str:
    tot = _team_totals(t)
    firsts = [label for key, label in (("first_tower", "一塔"), ("first_dragon", "一龙"), ("first_baron", "一男爵"),
                                       ("first_inhibitor", "一水晶")) if t.get(key)]
    bits = [f"击杀{tot['kills']}", f"经济{big(tot['gold'])}"]
    for key, label in (("towers", "推塔"), ("dragons", "小龙"), ("barons", "男爵"), ("hordes", "虚空巢虫")):
        if t.get(key) is not None:
            bits.append(f"{label}{_i(t.get(key))}")
    if firsts:
        bits.append("拿下" + "、".join(firsts))
    return f"【{'胜' if t.get('win') else '负'}方 {_team_label(t)}】" + " ".join(bits)


# ------------------------------------------------------------------ briefs
def brief_game(game: dict, gd=None, meta: dict | None = None, timeline: dict | None = None) -> str:
    teams = [t for t in game.get("teams") or [] if t.get("players")]
    secs = game_secs(game)
    mins = max(secs / 60, 1)
    players = [p for t in teams for p in t["players"]]
    target = next((p for p in players if p.get("is_target")), None)
    my_team = next((t for t in teams if target in t["players"]), None) if target else None
    lines = ["对局：" + " · ".join(x for x in [game.get("queue"), game.get("map"), f"时长 {duration(secs)}",
                                            f"平均段位 {game['avg_tier']}" if game.get("avg_tier") else ""] if x)]
    if target and my_team:
        lines.append(f"点评对象：{_name(target)}（{_pos(target) or '未知位置'} {_champ(target)}，{'胜' if my_team.get('win') else '负'}）")
    for t in teams:
        lines.append(team_line(t))
        for p in t["players"]:
            lines.append(player_line(p, t, mins, gd, mark=p is target, items=p is target))
    if target and my_team:
        opp_team = next((t for t in teams if t is not my_team), None)
        opp = next((p for p in (opp_team or {}).get("players") or [] if p.get("position") and p.get("position") == target.get("position")), None)
        if opp:
            diff = []
            for key, label in (("gold", "经济"), ("damage", "伤害"), ("cs", "补刀")):
                if target.get(key) is not None and opp.get(key) is not None:
                    v = _i(target.get(key)) - _i(opp.get(key))
                    diff.append(f"{label}{'+' if v >= 0 else ''}{v}")
            lines.append(f"对位：{_champ(target)} vs {_champ(opp)}（{_name(opp)}），差值 " + " ".join(diff))
    if meta:
        lines.append("版本参考（OP.GG 该英雄该位置）：" + "；".join(meta_lines(meta)))
    if timeline:
        lines.append("时间线（国服客户端）：" + "；".join(timeline_lines(timeline)))
    return "\n".join(lines)


def meta_lines(meta: dict) -> list[str]:
    out = []
    if meta.get("tier") is not None or meta.get("win_rate") is not None:
        out.append(f"梯队 T{meta.get('tier')} 胜率{_p(meta.get('win_rate'))}")
    if meta.get("core"):
        out.append("主流核心装 " + "/".join(meta["core"]) + (f"（胜率{_p(meta.get('core_wr'))}）" if meta.get("core_wr") else ""))
    if meta.get("boots"):
        out.append(f"主流鞋子 {meta['boots']}")
    if meta.get("matchup"):
        out.append(meta["matchup"])
    return out


def timeline_lines(tl: dict) -> list[str]:
    out = []
    gd = tl.get("gold_diff") or {}
    if gd:
        out.append("我方经济差 " + " ".join(f"{m}分{'+' if v >= 0 else ''}{v}" for m, v in sorted(gd.items())))
    me = tl.get("me") or {}
    if me:
        out.append("点评对象 " + " ".join(f"{m}分经济{v['gold']}补刀{v['cs']}" for m, v in sorted(me.items())))
    if tl.get("first_blood"):
        out.append(tl["first_blood"])
    if tl.get("objectives"):
        out.append("资源：" + "，".join(tl["objectives"][:8]))
    return out


def score_curve(players: list[dict]) -> dict[int, float]:
    curves = [p.get("score_tl") or [] for p in players]
    curves = [c for c in curves if c]
    if not curves:
        return {}
    n = max(len(c) for c in curves)
    out = {}
    for m in range(1, n + 1):
        vals = [c[m - 1] for c in curves if len(c) >= m and c[m - 1] is not None]
        if vals:
            out[m] = sum(vals) / len(vals)
    return out


def score_story(teams: list[dict]) -> list[str]:
    """OP.GG live rating (0-10) per minute -> a few readable checkpoints and lead changes."""
    if len(teams) < 2:
        return []
    a, b = teams[0], teams[1]
    ca, cb = score_curve(a["players"]), score_curve(b["players"])
    common = sorted(set(ca) & set(cb))
    if not common:
        return []
    last = common[-1]
    marks = [m for m in (5, 10, 15, 20, 25, 30, 35, 40) if m < last] + [last]
    la, lb = _team_label(a), _team_label(b)
    out = ["评分走势（OP.GG 实时评分，队伍平均，0-10）：" + "；".join(f"{m}分 {la} {ca[m]:.1f} : {lb} {cb[m]:.1f}" for m in marks if m in ca)]
    lead, flips = 0, []
    for m in common:
        d = ca[m] - cb[m]
        side = 1 if d > 0.4 else -1 if d < -0.4 else 0
        if side and side != lead:
            if lead:
                flips.append(f"第{m}分钟 {la if side > 0 else lb} 反超")
            lead = side
    if flips:
        out.append("局势变化：" + "，".join(flips[:4]))
    movers = []
    for t in teams:
        for p in t["players"]:
            tl = p.get("score_tl") or []
            if len(tl) >= 12:
                movers.append((tl[-1] - tl[9], p))
    movers.sort(key=lambda x: x[0])
    if movers:
        up, down = movers[-1], movers[0]
        out.append(f"10分钟后评分上升最多：{_name(up[1])}（{up[0]:+.1f}）；下降最多：{_name(down[1])}（{down[0]:+.1f}）")
    return out


def brief_esports_game(n: dict, g: dict, header: str, gd=None) -> str:
    teams = [t for t in n.get("teams") or [] if t.get("players")]
    secs = game_secs(n)
    mins = max(secs / 60, 1)
    lines = [header + f"，时长 {duration(secs)}"]
    for t in teams:
        lines.append(team_line(t) + ("  禁用：" + "/".join(gd.display_name(gd.champion(b)).split(" ")[0] for b in t.get("bans") or [] if b)
                                     if gd is not None and t.get("bans") else ""))
        for p in t["players"]:
            lines.append(player_line(p, t, mins, gd, items=False))
    lines += score_story(teams)
    return "\n".join(lines)


def brief_series(g: dict, games: list[dict], header: str, mvp_of: Callable[[dict], dict | None]) -> str:
    lines = [header]
    agg: dict[str, dict] = {}
    for n in games:
        teams = [t for t in n.get("teams") or [] if t.get("players")]
        if len(teams) < 2:
            continue
        win = next((t for t in teams if t.get("win")), None)
        tot = [_team_totals(t) for t in teams]
        mvp = mvp_of(n)
        lines.append(f"第{n.get('set')}局 {duration(game_secs(n))} {(_team_label(win) + ' 胜') if win else '进行中'}："
                     f"{_team_label(teams[0])} {tot[0]['kills']}杀 {big(tot[0]['gold'])} vs {_team_label(teams[1])} {tot[1]['kills']}杀 {big(tot[1]['gold'])}"
                     + (f"，MVP {_name(mvp)}（{_champ(mvp)} {_i(mvp.get('kills'))}/{_i(mvp.get('deaths'))}/{_i(mvp.get('assists'))}）" if mvp else ""))
        for t in teams:
            for p in t["players"]:
                a = agg.setdefault(f"{_team_label(t)} {_name(p)}", {"k": 0, "d": 0, "a": 0, "s": [], "c": [], "pos": _pos(p)})
                a["k"] += _i(p.get("kills"))
                a["d"] += _i(p.get("deaths"))
                a["a"] += _i(p.get("assists"))
                a["c"].append(_champ(p))
                if _f(p.get("score")):
                    a["s"].append(float(p["score"]))
    if agg:
        lines.append("选手整场合计：")
        for key, a in agg.items():
            avg = f" 平均评分{sum(a['s']) / len(a['s']):.1f}" if a["s"] else ""
            lines.append(f"·{key}（{a['pos']}）{a['k']}/{a['d']}/{a['a']}{avg} 英雄 {'/'.join(a['c'])}")
    return "\n".join(lines)


# ------------------------------------------------------------------ recent form
def recent_rows(games: list[dict]) -> list[dict]:
    rows = []
    for g in games:
        teams = [t for t in g.get("teams") or [] if t.get("players")]
        target = next((p for t in teams for p in t["players"] if p.get("is_target")), None)
        if not target:
            continue
        my = next(t for t in teams if target in t["players"])
        full = not my.get("partial") and len(my["players"]) >= 3
        tot = _team_totals(my) if full else {}
        mins = max(game_secs(g) / 60, 1)
        k, d, a = _i(target.get("kills")), _i(target.get("deaths")), _i(target.get("assists"))
        rows.append({
            "win": bool(my.get("win")) if my.get("win") is not None else str(target.get("result")).upper() == "WIN",
            "champion_id": target.get("champion_id"), "champion": _champ(target), "position": target.get("position"),
            "k": k, "d": d, "a": a, "mins": mins, "queue": g.get("queue"),
            "kp": _ratio(k + a, tot.get("kills", 0)) if full else None,
            "dmg_share": _ratio(_i(target.get("damage")), tot.get("damage", 0)) if full else None,
            "taken_share": _ratio(_i(target.get("taken")), tot.get("taken", 0)) if full and target.get("taken") is not None else None,
            "gold_share": _ratio(_i(target.get("gold")), tot.get("gold", 0)) if full and target.get("gold") is not None else None,
            "death_share": _ratio(d, tot.get("deaths", 0)) if full else None,
            "cspm": _i(target.get("cs")) / mins if target.get("cs") is not None else None,
            "dpm": _i(target.get("damage")) / mins if target.get("damage") is not None else None,
            "score": _f(target.get("score")) or None,
            "rank": _i(target.get("score_rank")) or None,
        })
    return rows


def _avg(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r.get(key) is not None]
    return sum(vals) / len(vals) if vals else None


def _kda(rows: list[dict]) -> float | None:
    if not rows:
        return None
    return (sum(r["k"] for r in rows) + sum(r["a"] for r in rows)) / max(sum(r["d"] for r in rows), 1)


METRICS = [
    # key, label, scale max, higher is better, percent?
    ("winrate", "胜率", 1.0, True, True),
    ("kda", "KDA", 6.0, True, False),
    ("kp", "参团率", 1.0, True, True),
    ("dmg_share", "伤害占比", 0.45, True, True),
    ("gold_share", "经济占比", 0.35, True, True),
    ("death_share", "死亡占比", 0.4, False, True),
    ("cspm", "分均补刀", 10.0, True, False),
    ("dpm", "分均伤害", 1200.0, True, False),
    ("score", "平均评分", 10.0, True, False),
]


def metric(rows: list[dict], key: str) -> float | None:
    if not rows:
        return None
    if key == "winrate":
        return sum(1 for r in rows if r["win"]) / len(rows)
    if key == "kda":
        return _kda(rows)
    return _avg(rows, key)


def fmt_metric(key: str, v: float | None) -> str:
    if v is None:
        return "-"
    if key in ("winrate", "kp", "dmg_share", "gold_share", "death_share", "taken_share"):
        return f"{v * 100:.0f}%"
    if key == "dpm":
        return f"{v:.0f}"
    return f"{v:.1f}" if key != "kda" else f"{v:.2f}"


def recent_stats(rows: list[dict]) -> dict:
    recent = rows[:5]
    streak, kind = 0, None
    for r in rows:
        if kind is None:
            kind = r["win"]
        if r["win"] != kind:
            break
        streak += 1
    champs: dict[str, dict] = {}
    for r in rows:
        c = champs.setdefault(r["champion"], {"id": r["champion_id"], "rows": []})
        c["rows"].append(r)
    pos: dict[str, int] = {}
    for r in rows:
        if r.get("position"):
            pos[r["position"]] = pos.get(r["position"], 0) + 1
    return {
        "n": len(rows), "wins": sum(1 for r in rows if r["win"]),
        "metrics": {k: (metric(rows, k), metric(recent, k)) for k, *_ in METRICS},
        "streak": (streak, kind), "champs": sorted(champs.items(), key=lambda x: -len(x[1]["rows"])),
        "positions": sorted(pos.items(), key=lambda x: -x[1]),
    }


def brief_recent(who: str, rows: list[dict], st: dict) -> str:
    lines = [f"玩家：{who}，最近 {st['n']} 局召唤师峡谷对局（从新到旧），{st['wins']} 胜 {st['n'] - st['wins']} 负"]
    s, kind = st["streak"]
    if s >= 2:
        lines.append(f"当前 {s} 连{'胜' if kind else '败'}")
    lines.append("整体 vs 最近5局：" + "；".join(
        f"{label} {fmt_metric(k, st['metrics'][k][0])} / {fmt_metric(k, st['metrics'][k][1])}"
        for k, label, *_ in METRICS if st["metrics"][k][0] is not None))
    if st["positions"]:
        lines.append("位置分布：" + " ".join(f"{POSITIONS.get(p, p)}{n}局" for p, n in st["positions"]))
    lines.append("英雄：" + "；".join(
        f"{name} {len(c['rows'])}局 胜率{fmt_metric('winrate', metric(c['rows'], 'winrate'))} KDA{fmt_metric('kda', metric(c['rows'], 'kda'))}"
        for name, c in st["champs"][:6]))
    lines.append("逐局：")
    for i, r in enumerate(rows, 1):
        bits = [f"{i}. {'胜' if r['win'] else '负'} {POSITIONS.get(r.get('position') or '', '-')} {r['champion']} {r['k']}/{r['d']}/{r['a']}"]
        if r.get("kp") is not None:
            bits.append(f"参团{_p(r['kp'])} 伤害占比{_p(r['dmg_share'])} 死亡占比{_p(r['death_share'])}")
        if r.get("cspm") is not None:
            bits.append(f"分均补刀{r['cspm']:.1f}")
        if r.get("score"):
            bits.append(f"评分{r['score']:.1f}" + (f"(第{r['rank']})" if r.get("rank") else ""))
        lines.append(" ".join(bits))
    return "\n".join(lines)


# ------------------------------------------------------------------ timeline (China client)
def summarize_timeline(raw: dict, my_side: str, my_puuid: str = "") -> dict:
    """Riot timeline (SGP v5 or LCU) -> gold diff at 10/15/20, first blood, objectives, my gold / cs."""
    info = raw.get("info") if isinstance(raw.get("info"), dict) else raw
    frames = info.get("frames") or []
    if not frames:
        return {}
    my_ids = set(range(1, 6)) if str(my_side).upper() != "RED" else set(range(6, 11))
    me_id = None
    for p in info.get("participants") or []:
        if my_puuid and p.get("puuid") == my_puuid:
            me_id = _i(p.get("participantId"))
    out: dict[str, Any] = {"gold_diff": {}, "me": {}, "objectives": []}
    for minute in (10, 15, 20, 25):
        if minute >= len(frames):
            break
        pf = frames[minute].get("participantFrames") or {}
        mine = sum(_i(v.get("totalGold")) for k, v in pf.items() if _i(k) in my_ids)
        theirs = sum(_i(v.get("totalGold")) for k, v in pf.items() if _i(k) and _i(k) not in my_ids)
        if mine or theirs:
            out["gold_diff"][minute] = mine - theirs
        if me_id and str(me_id) in pf:
            v = pf[str(me_id)]
            out["me"][minute] = {"gold": _i(v.get("totalGold")), "cs": _i(v.get("minionsKilled")) + _i(v.get("jungleMinionsKilled"))}
    names = {"DRAGON": "小龙", "BARON_NASHOR": "男爵", "RIFTHERALD": "峡谷先锋", "HORDE": "虚空巢虫", "ATAKHAN": "阿塔坎", "ELDER_DRAGON": "远古龙"}
    for fr in frames:
        for e in fr.get("events") or []:
            t = _i(e.get("timestamp")) // 60000
            if e.get("type") == "CHAMPION_KILL" and "first_blood" not in out:
                ours = _i(e.get("killerId")) in my_ids
                out["first_blood"] = f"一血 第{t}分钟 由{'我方' if ours else '敌方'}拿下"
            elif e.get("type") == "ELITE_MONSTER_KILL":
                team = _i(e.get("killerTeamId"))
                ours = (team == 100) == (my_ids == set(range(1, 6))) if team else _i(e.get("killerId")) in my_ids
                label = names.get(str(e.get("monsterType") or "").upper(), str(e.get("monsterType") or "野怪"))
                out["objectives"].append(f"{t}分{'我方' if ours else '敌方'}{label}")
    return {k: v for k, v in out.items() if v}


# ------------------------------------------------------------------ prompt / parse / render
def system_prompt(style: str, kind: str) -> str:
    role, fields, _ = KINDS[kind]
    return (
        f"你是{role}。根据用户给出的数据写一段点评。\n"
        "规则：\n"
        "1. 只使用数据里出现的事实和数字；数据没有的内容（对线细节、团战过程、具体操作）不要编造，可以根据数字推断但要说明依据。\n"
        "2. 每条亮点和问题都至少引用一个具体数字。\n"
        "3. 用简体中文，选手用游戏名称呼，英雄用中文名。\n"
        f"4. {fields}。\n"
        '5. 只输出一个 JSON 对象，不要代码块和其他文字：{"summary": "一句话总评，40字以内", '
        '"highlights": ["2-3条，每条40字以内"], "issues": ["1-2条，每条40字以内"], "advice": "50字以内"}\n'
        f"风格：{STYLES.get(style, STYLES[DEFAULT_STYLE])}"
    )


def _clip(s: Any, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def parse_review(text: str) -> dict:
    raw = (text or "").strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.I | re.M).strip()
    data = None
    a, b = raw.find("{"), raw.rfind("}")
    if a >= 0 and b > a:
        try:
            data = json.loads(raw[a: b + 1])
        except ValueError:
            data = None
    if not isinstance(data, dict):
        return {"summary": _clip(raw, 300), "highlights": [], "issues": [], "advice": ""}

    def items(v, n):
        if isinstance(v, str):
            v = [x for x in re.split(r"[\n；;]+", v) if x.strip()]
        return [_clip(x, 90) for x in (v or []) if str(x).strip()][:n]

    return {"summary": _clip(data.get("summary"), 120), "highlights": items(data.get("highlights"), 3),
            "issues": items(data.get("issues"), 3), "advice": _clip(data.get("advice"), 140)}


def review_block(out: dict) -> tuple[dict, list[str]]:
    r = out["review"]
    labels = KINDS.get(out.get("kind") or "game", KINDS["game"])[2]
    block = {"summary": r["summary"], "highlights": r["highlights"], "issues": r["issues"], "advice": r["advice"],
             "labels": list(labels), "style": out.get("style") or DEFAULT_STYLE, "model": out.get("model") or ""}
    lines = [f"🤖 AI 点评（{block['style']}）"]
    if r["summary"]:
        lines.append(f"总评：{r['summary']}")
    lines += [f"✅ {labels[0]}：{x}" for x in r["highlights"]]
    lines += [f"⚠️ {labels[1]}：{x}" for x in r["issues"]]
    if r["advice"]:
        lines.append(f"💡 {labels[2]}：{r['advice']}")
    lines.append(f"（{block['model'] or 'AI'} 生成，仅供参考）")
    return block, lines


def attach(doc, out: dict) -> None:
    block, lines = review_block(out)
    doc.add_block("AI 点评", "ai", block, lines)


class AiReviewer:
    def __init__(self, complete: CompleteFn, logger: Any = None, timeout: float = 90, ttl: float = 7 * 86400):
        self.complete = complete
        self.logger = logger or default_logger
        self.timeout = timeout
        self.ttl = ttl
        self.cache = TTLCache(256)

    def cached(self, kind: str, key: str, style: str) -> dict | None:
        hit = self.cache.get(f"{kind}:{key}:{style}")
        return hit[0] if hit else None

    async def review(self, kind: str, key: str, brief: str, style: str | None = None, umo: Any = None,
                     gate: GateFn | None = None) -> dict:
        style = style if style in STYLES else DEFAULT_STYLE
        hit = self.cached(kind, key, style)
        if hit:
            return hit
        if gate:
            gate()  # cooldown check, only when the model is really called
        t0 = time.time()
        try:
            text, model = await asyncio.wait_for(self.complete(system_prompt(style, kind), brief, umo), self.timeout)
        except asyncio.TimeoutError as exc:
            raise AiError("AI 点评超时，请稍后再试") from exc
        except LolHelperError:
            raise
        except Exception as exc:
            self.logger.warning(f"[lolhelper] AI 点评调用模型失败：{type(exc).__name__}: {exc}")
            raise AiError(f"调用模型失败：{type(exc).__name__}") from exc
        review = parse_review(text)
        if not review["summary"] and not review["highlights"]:
            raise AiError("模型没有返回有效的点评内容")
        out = {"review": review, "model": model, "style": style, "kind": kind, "secs": round(time.time() - t0, 1)}
        self.cache.set(f"{kind}:{key}:{style}", out, self.ttl)
        return out

