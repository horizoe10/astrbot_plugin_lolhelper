"""Match data shared by the match list cards and the single match detail view.

Both OP.GG (global) and SGP (China) games are normalised into one "game"
dict: {id, created_at, queue, map, duration, avg_tier, teams: [{key, win,
kills, gold, towers, dragons, barons, hordes, bans, players: [...]}]}. The
match list card (League Akari style) and the team tables are built from it.
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any

from .constants import POSITION_FROM_API, POSITIONS, QUEUE_ID_ZH
from .doc import Doc
from .fmt import ago, big, duration, freshness, kda, kda_cls, local_time, pct, queue_type, score_cls, tier_text
from .gamedata import GameData
from .service import View

F_GAME = [
    "data.game_detail.{id,created_at,game_type,game_length_second,game_map}",
    "data.game_detail.average_tier_info.{tier,division}",
    "data.game_detail.teams[].{key,banned_champions[]}",
    "data.game_detail.teams[].game_stat.{is_win,champion_kill,gold_earned,tower_kill,dragon_kill,baron_kill,horde_kill}",
    "data.game_detail.teams[].participants[].{champion_id,champion_name,is_target,items[],spells[],position,role_bound_item}",
    "data.game_detail.teams[].participants[].rune.{primary_rune_id,secondary_page_id}",
    "data.game_detail.teams[].participants[].summoner.{game_name,tagline}",
    "data.game_detail.teams[].participants[].stats.{kill,death,assist,champion_level,minion_kill,neutral_minion_kill,gold_earned,total_damage_dealt_to_champions,total_damage_taken,ward_place,op_score,op_score_rank,result,time_ccing_others,total_heal,largest_multi_kill}",
]

POS_ORDER = {"top": 0, "jungle": 1, "mid": 2, "adc": 3, "support": 4}
SIDE = {"BLUE": "蓝队", "RED": "红队"}
SIDE_KEY = {"BLUE": "blue", "RED": "red"}
MAPS = {"SUMMONERS_RIFT": "召唤师峡谷", "HOWLING_ABYSS": "嚎哭深渊", "ARENA": "斗魂竞技场", "NEXUS_BLITZ": "极限闪击",
        11: "召唤师峡谷", 12: "嚎哭深渊", 30: "斗魂竞技场", 21: "极限闪击"}
TRINKETS = {3340, 3363, 3364, 3330, 3513}
# role quest rewards (“上路/打野/中路/下路/辅助任务奖励”), shown in their own slot after the trinket
ROLE_ITEMS = {1205, 1206, 1207, 1208, 1209, 1210, 1220, 1221, 1222}


def is_role_item(i) -> bool:
    n = _int(i)
    return n in ROLE_ITEMS or 1200 <= n < 1230
MULTI = {3: "三杀", 4: "四杀", 5: "五杀"}
TAG_CLS = {"MVP": "tag-mvp", "ACE": "tag-ace", "参团": "tag-blue", "护盾": "tag-blue", "控制": "tag-purple",
           "治疗": "tag-green", "输出": "tag-orange", "承伤": "tag-gray", "三杀": "tag-red", "四杀": "tag-red", "五杀": "tag-red"}


def _sort_players(players: list[dict]) -> list[dict]:
    return sorted(players, key=lambda p: POS_ORDER.get(p.get("position") or "", 9))


def _int(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


# ------------------------------------------------------------------ normalizers
def normalize_opgg_game(g: dict, gd: GameData) -> dict[str, Any]:
    teams = []
    for t in g.get("teams") or []:
        st = t.get("game_stat") or {}
        players = []
        for p in t.get("participants") or []:
            s = p.get("stats") or {}
            summ = p.get("summoner") or {}
            rune = p.get("rune") or {}
            players.append({
                "champion_id": p.get("champion_id"),
                "champion_name": p.get("champion_name") or gd.display_name(gd.champion(p.get("champion_id"))),
                "game_name": summ.get("game_name") or "",
                "tag_line": summ.get("tagline") or "",
                "is_target": bool(p.get("is_target")),
                "position": POSITION_FROM_API.get(str(p.get("position") or "").upper()),
                "items": [i for i in p.get("items") or [] if i],
                "role_item": p.get("role_bound_item") or None,
                "spells": p.get("spells") or [],
                "keystone": rune.get("primary_rune_id"),
                "sub_style": rune.get("secondary_page_id"),
                "kills": s.get("kill"), "deaths": s.get("death"), "assists": s.get("assist"),
                "level": s.get("champion_level"),
                "cs": _int(s.get("minion_kill")) + _int(s.get("neutral_minion_kill")),
                "gold": s.get("gold_earned"),
                "damage": s.get("total_damage_dealt_to_champions"),
                "taken": s.get("total_damage_taken"),
                "wards": s.get("ward_place"),
                "cc": s.get("time_ccing_others"),
                "heal": s.get("total_heal"),
                "shield": None,
                "multikill": s.get("largest_multi_kill"),
                "score": s.get("op_score"),
                "score_rank": s.get("op_score_rank"),
                "result": s.get("result"),
            })
        teams.append({
            "key": t.get("key"),
            "win": bool(st.get("is_win")),
            "kills": st.get("champion_kill"),
            "gold": st.get("gold_earned"),
            "towers": st.get("tower_kill"),
            "dragons": st.get("dragon_kill"),
            "barons": st.get("baron_kill"),
            "hordes": st.get("horde_kill"),
            "bans": [b for b in t.get("banned_champions") or [] if b],
            "players": _sort_players(players),
        })
    avg = g.get("average_tier_info") or {}
    return {
        "id": g.get("id"),
        "created_at": g.get("created_at"),
        "queue": queue_type(g.get("game_type")),
        "map": MAPS.get(g.get("game_map"), ""),
        "duration": g.get("game_length_second"),
        "avg_tier": tier_text(avg.get("tier"), avg.get("division")) if avg.get("tier") else "",
        "teams": teams,
        "source": "opgg",
    }


def _sgp_runes(p: dict) -> tuple[Any, Any]:
    styles = ((p.get("perks") or {}).get("styles")) or []
    primary = next((s for s in styles if s.get("description") == "primaryStyle"), styles[0] if styles else {})
    secondary = next((s for s in styles if s.get("description") == "subStyle"), styles[1] if len(styles) > 1 else {})
    selections = primary.get("selections") or []
    keystone = selections[0].get("perk") if selections else None
    return keystone or p.get("perk0"), secondary.get("style") or p.get("perkSubStyle")


def normalize_sgp_game(raw: dict, puuid: str, gd: GameData) -> dict[str, Any]:
    j = (raw or {}).get("json") or raw or {}
    team_meta = {t.get("teamId"): t for t in j.get("teams") or []}
    by_team: dict[int, list[dict]] = {}
    for p in j.get("participants") or []:
        champ = gd.champion(p.get("championId"))
        keystone, sub = _sgp_runes(p)
        by_team.setdefault(p.get("teamId"), []).append({
            "champion_id": p.get("championId"),
            "champion_name": (champ or {}).get("name_zh") or str(p.get("championId")),
            "game_name": p.get("riotIdGameName") or p.get("summonerName") or "",
            "tag_line": p.get("riotIdTagline") or "",
            "is_target": p.get("puuid") == puuid,
            "position": POSITION_FROM_API.get(str(p.get("teamPosition") or p.get("individualPosition") or "").upper()),
            "items": [p.get(f"item{i}") for i in range(7) if p.get(f"item{i}")], "role_item": p.get("roleBoundItem") or None,
            "spells": [s for s in (p.get("summoner1Id"), p.get("summoner2Id")) if s],
            "keystone": keystone,
            "sub_style": sub,
            "kills": p.get("kills"), "deaths": p.get("deaths"), "assists": p.get("assists"),
            "level": p.get("champLevel"),
            "cs": _int(p.get("totalMinionsKilled")) + _int(p.get("neutralMinionsKilled")),
            "gold": p.get("goldEarned"),
            "damage": p.get("totalDamageDealtToChampions"),
            "taken": p.get("totalDamageTaken"),
            "wards": p.get("wardsPlaced"),
            "cc": p.get("timeCCingOthers"),
            "heal": p.get("totalHeal"),
            "shield": p.get("totalDamageShieldedOnTeammates"),
            "multikill": p.get("largestMultiKill"),
            "score": None,
            "score_rank": None,
            "result": "REMAKE" if p.get("gameEndedInEarlySurrender") else ("WIN" if p.get("win") else "LOSE"),
            "win": p.get("win"),
        })
    teams = []
    for team_id in sorted(by_team):
        players = by_team[team_id]
        meta = team_meta.get(team_id) or {}
        obj = meta.get("objectives") or {}
        teams.append({
            "key": "BLUE" if team_id == 100 else ("RED" if team_id == 200 else str(team_id)),
            "win": bool(meta.get("win")) if meta else any(p.get("win") for p in players),
            "kills": (obj.get("champion") or {}).get("kills", sum(_int(p.get("kills")) for p in players)),
            "gold": sum(_int(p.get("gold")) for p in players),
            "towers": (obj.get("tower") or {}).get("kills"),
            "dragons": (obj.get("dragon") or {}).get("kills"),
            "barons": (obj.get("baron") or {}).get("kills"),
            "hordes": (obj.get("horde") or {}).get("kills"),
            "bans": [b.get("championId") for b in meta.get("bans") or [] if (b.get("championId") or -1) > 0],
            "players": _sort_players(players),
        })
    qid = _int(j.get("queueId")) if j.get("queueId") is not None else -1
    return {
        "id": str(j.get("gameId")),
        "created_at": j.get("gameCreation") or j.get("gameStartTimestamp"),
        "queue": QUEUE_ID_ZH.get(qid, f"模式{qid}"),
        "queue_id": qid,
        "map": MAPS.get(j.get("mapId"), ""),
        "duration": j.get("gameDuration"),
        "avg_tier": "",
        "teams": teams,
        "source": "sgp",
    }


def minimal_game(m: dict) -> dict[str, Any]:
    """Game dict built from a match list entry when the full detail is unavailable."""
    p = {
        **{k: m.get(k) for k in ("champion_id", "champion_name", "position", "items", "spells", "keystone", "sub_style",
                                 "kills", "deaths", "assists", "level", "cs", "damage", "score", "score_rank", "result")},
        "is_target": True, "game_name": "", "tag_line": "",
    }
    return {
        "id": m.get("id"), "created_at": m.get("created_at"), "queue": m.get("queue"), "map": m.get("map", ""),
        "duration": m.get("duration"), "avg_tier": m.get("avg_tier", ""), "source": "list",
        "teams": [{"key": None, "win": str(m.get("result")).upper() == "WIN", "players": [p], "partial": True}],
    }


def target_entry(game: dict) -> dict | None:
    """Flatten a game into the per-player match entry used by summaries and push."""
    for t in game.get("teams") or []:
        for p in t["players"]:
            if p.get("is_target"):
                team_kills = _int(t.get("kills")) or sum(_int(x.get("kills")) for x in t["players"])
                kp = min(1.0, (_int(p.get("kills")) + _int(p.get("assists"))) / team_kills) if team_kills and not t.get("partial") else None
                return {
                    **{k: p.get(k) for k in ("champion_id", "champion_name", "position", "items", "spells", "keystone",
                                             "sub_style", "kills", "deaths", "assists", "level", "cs", "damage", "score",
                                             "score_rank", "result", "multikill", "role_item")},
                    "id": game.get("id"), "created_at": game.get("created_at"), "queue": game.get("queue"),
                    "duration": game.get("duration"), "kill_participation": kp,
                    "result": p.get("result") or ("WIN" if t.get("win") else "LOSE"),
                    "_game": game,
                }
    return None


# --------------------------------------------------------------------- helpers
def split_role_item(items: list | None, role) -> tuple[list, Any]:
    """Remove the role-bound slot from a full inventory list (esports lists it among the items)."""
    items = [i for i in items or [] if i]
    if not _int(role):
        return items, None
    idx = next((k for k, i in enumerate(items) if _int(i) == _int(role)), None)
    if idx is None:  # the list may carry another variant of the same quest reward
        idx = next((k for k, i in enumerate(items) if is_role_item(i)), None)
    if idx is not None:
        items.pop(idx)
    return items, role


def _slots(items: list, gd: GameData, role_item=None) -> list:
    """Six item slots, the trinket slot, then the role-bound slot.

    The role-bound slot holds the role quest reward, or what the reward moved there:
    boots for bot lane, control wards for support. Sources that report it separately
    pass it as role_item; for the others the reward is recognised by its item id.
    """
    items = [i for i in items or [] if i]
    trinket = next((i for i in items if _int(i) in TRINKETS), None)
    if trinket is not None:
        items.remove(trinket)
    role = role_item if _int(role_item) else None
    if role is None:
        role = next((i for i in items if is_role_item(i)), None)
        if role is not None:
            items.remove(role)
    rest = items[:6]
    icons = [gd.item_icon(i) for i in rest] + [None] * (6 - len(rest))
    icons.append(gd.item_icon(trinket) if trinket else None)
    if role:
        icons.append(gd.item_icon(role))
    return icons


def _sr(p: dict, gd: GameData) -> list:
    spells = (p.get("spells") or []) + [None, None]
    return [gd.spell_icon(spells[0]) if spells[0] else None, gd.rune_icon(p.get("keystone")),
            gd.spell_icon(spells[1]) if spells[1] else None, gd.rune_icon(p.get("sub_style"))]


def compute_tags(game: dict) -> dict[int, list[dict]]:
    """Highlight tags per player (keyed by id()), similar to League Akari."""
    out: dict[int, list[dict]] = {}
    teams = [t for t in game.get("teams") or [] if not t.get("partial")]
    everyone = [p for t in teams for p in t["players"]]
    if not everyone:
        return out

    def add(p, text):
        kind = {"MVP": "mvp", "ACE": "ace"}.get(text) or ("star" if text.startswith("★") else "multi" if text in MULTI.values() else "")
        out.setdefault(id(p), []).append({"text": text, "cls": TAG_CLS.get(text.lstrip("★"), "tag-gray"), "k": kind})

    has_score = any(p.get("score") is not None for p in everyone)
    for t in teams:
        tk = _int(t.get("kills")) or sum(_int(x.get("kills")) for x in t["players"])
        if has_score and t["players"]:
            best = max(t["players"], key=lambda p: float(p.get("score") or -1))
            add(best, "MVP" if t["win"] else "ACE")
        for key, label in (("kp", "参团"), ("damage", "输出"), ("taken", "承伤"), ("cc", "控制"), ("heal", "治疗"), ("shield", "护盾")):
            def val(p):
                if key == "kp":
                    return (_int(p.get("kills")) + _int(p.get("assists"))) / tk if tk else 0
                return _int(p.get(key))
            vals = [val(p) for p in t["players"]]
            if not vals or max(vals) <= 0:
                continue
            top = t["players"][vals.index(max(vals))]
            game_best = max(val(p) for p in everyone) == max(vals)
            add(top, ("★" if game_best else "") + label)
    for p in everyone:
        mk = _int(p.get("multikill"))
        if mk >= 3:
            add(p, MULTI.get(min(mk, 5)))
    return out


def _fmt_int(v) -> str:
    return f"{_int(v):,}"


# ------------------------------------------------------------------ banners
def rank_side(queues: list[dict]) -> dict | None:
    """Banner rank block from [{queue, tier, division, lp, w, l, emblem}], solo queue first, a second queue below."""
    queues = [q for q in queues if q.get("tier")]
    if not queues:
        return None
    q, rest = queues[0], queues[1:]
    w, l = _int(q.get("w")), _int(q.get("l"))
    side = {
        "kind": "rank", "emblem": q.get("emblem"), "queue": q.get("queue") or "",
        "tier": tier_text(q.get("tier"), q.get("division")), "lp": str(_int(q.get("lp"))) if q.get("lp") is not None else "",
        "wl": f"{w}胜 {l}负" + (f" · 胜率 {w / (w + l) * 100:.1f}%" if w + l else ""),
        "wr": f"{w / (w + l) * 100:.1f}" if w + l else "",
    }
    if rest:
        r = rest[0]
        lp = f" · {_int(r.get('lp'))}LP" if r.get("lp") is not None else ""
        side["alt"] = {"img": r.get("emblem"), "text": f"{r.get('queue') or ''} {tier_text(r.get('tier'), r.get('division'))}{lp}".strip()}
    return side


def form_stats(entries: list[dict], gd: GameData, streak: int = 10) -> tuple[list[dict], dict | None]:
    """Banner stat strip for a match list (record, win rate, KDA, rating, streak, mains) and the most played champion."""
    if not entries:
        return [], None
    res = [str(m.get("result") or "").upper() for m in entries]
    wins, losses = res.count("WIN"), res.count("LOSE")
    k = sum(_int(m.get("kills")) for m in entries)
    d = sum(_int(m.get("deaths")) for m in entries)
    a = sum(_int(m.get("assists")) for m in entries)
    scores = []
    for m in entries:
        try:
            if float(m.get("score") or 0) > 0:
                scores.append(float(m["score"]))
        except (TypeError, ValueError):
            pass
    counts = Counter(m.get("champion_id") for m in entries if m.get("champion_id"))
    top = [gd.champion(cid) for cid, _ in counts.most_common(3)]
    stats = [{"parts": [[str(wins), "胜"], [str(losses), "负"]], "k": f"近 {len(entries)} 场"},
             {"v": f"{round(wins / max(wins + losses, 1) * 100)}%", "k": "胜率"},
             {"v": kda(k, d, a), "k": "KDA"}]
    if scores:
        stats.append({"v": f"{sum(scores) / len(scores):.1f}", "k": "平均评分"})
    stats.append({"streak": ["w" if r == "WIN" else "l" if r == "LOSE" else "r" for r in res][:streak], "k": "从新到旧"})
    icons = [gd.champ_icon(c) for c in top if c]
    if icons:
        stats.append({"icons": icons, "k": "常用英雄"})
    return stats, (top[0] if top else None)


def game_hero(game: dict, gd: GameData, tz: str = "Asia/Shanghai", focus: tuple[str, str] | None = None) -> dict:
    """Personal banner for one game: the target player's champion art, result and line.
    focus = (game name, tag) in lower case picks that player when several are marked as targets."""
    teams = game.get("teams") or []

    def hit(p: dict) -> bool:
        if focus:
            return (str(p.get("game_name") or "").lower(), str(p.get("tag_line") or "").lower()) == focus
        return bool(p.get("is_target"))

    team = next((t for t in teams if any(hit(p) for p in t["players"])), None)
    if team is None and focus:
        return game_hero(game, gd, tz)
    if team is None:
        return {}
    me = next(p for p in team["players"] if hit(p))
    secs = _int(game.get("duration"))
    secs = secs // 1000 if secs > 100000 else secs
    minutes = max(secs / 60, 1)
    k, d, a = _int(me.get("kills")), _int(me.get("deaths")), _int(me.get("assists"))
    tk = _int(team.get("kills")) or sum(_int(p.get("kills")) for p in team["players"])
    team_dmg = sum(_int(p.get("damage")) for p in team["players"])
    res = str(me.get("result") or "").upper()
    if secs and secs < 300 and res != "WIN":
        word, cls = "重开", "remake"
    else:
        win = res == "WIN" if res in ("WIN", "LOSE") else bool(team.get("win"))
        word, cls = ("胜利", "win") if win else ("失败", "lose")
    champ = gd.champion(me.get("champion_id"))
    stats = [{"kda": [k, d, a], "k": f"KDA {kda(k, d, a)}"}]
    full = not team.get("partial")  # list-only games carry just the target, so team shares would be 100%
    if tk and full:
        stats.append({"v": pct(min(1.0, (k + a) / tk), 0), "k": "参团率"})
    if me.get("damage") is not None:
        share = f" · 占比 {pct(_int(me.get('damage')) / team_dmg, 0)}" if team_dmg and full else ""
        stats.append({"v": _fmt_int(me.get("damage")), "k": f"伤害{share}"})
    if me.get("cs") is not None:
        stats.append({"v": str(_int(me.get("cs"))), "k": f"补兵 · {_int(me.get('cs')) / minutes:.1f}/分" if secs else "补兵"})
    if me.get("gold"):
        stats.append({"v": _fmt_int(me.get("gold")), "k": "经济"})
    if me.get("score") not in (None, "") and float(me.get("score") or 0) > 0:
        rank = f" · 第 {me['score_rank']}" if me.get("score_rank") else ""
        stats.append({"v": f"{float(me['score']):.1f}", "k": f"评分{rank}", "cls": "gold" if me.get("score_rank") == 1 else ""})
    chips = [{"text": game.get("queue") or "对局", "cls": "acc"}]
    for x in (local_time(game.get("created_at"), tz, "%m-%d %H:%M"), game.get("map"),
              f"平均段位 {game['avg_tier']}" if game.get("avg_tier") else ""):
        if x and x != "-":
            chips.append({"text": x})
    return {
        "art": gd.splash(champ), "icon": gd.champ_icon(champ), "level": str(me.get("level") or ""),
        "name": me.get("game_name") or (champ or {}).get("name_zh") or "?", "tag": f"#{me['tag_line']}" if me.get("tag_line") else "",
        "chips": chips, "side": {"big": word, "cls": cls, "sub": f"时长 {duration(secs)}"},
        "stats": stats,
    }


# ------------------------------------------------------------- match list card
def match_card(game: dict, gd: GameData) -> tuple[dict, str] | None:
    entry = target_entry(game)
    if not entry:
        return None
    team = next(t for t in game["teams"] if any(p.get("is_target") for p in t["players"]))
    me = next(p for p in team["players"] if p.get("is_target"))
    secs = _int(game.get("duration"))
    secs = secs // 1000 if secs > 100000 else secs
    minutes = max(secs / 60, 1)
    team_dmg = sum(_int(p.get("damage")) for p in team["players"])
    res = str(entry.get("result") or "").upper()
    if res not in ("WIN", "LOSE") or secs and secs < 300 and res != "WIN":
        cls, word = ("remake", "重开") if secs and secs < 300 else (("win", "胜利") if team.get("win") else ("lose", "失败"))
    else:
        cls, word = ("win", "胜利") if res == "WIN" else ("lose", "失败")
    kp = entry.get("kill_participation")
    share = _int(me.get("damage")) / team_dmg if team_dmg and not team.get("partial") else None
    tags = compute_tags(game).get(id(me), [])
    players = []
    if not team.get("partial"):
        for t in game["teams"]:
            players.append([
                {"icon": gd.champ_icon(gd.champion(p.get("champion_id"))), "name": p.get("game_name") or "?", "me": bool(p.get("is_target"))}
                for p in t["players"]
            ])
    foot = " · ".join(x for x in [game.get("queue"), duration(secs), ago(game.get("created_at")), game.get("map")] if x)
    score = me.get("score")
    card = {
        "cls": f"mc-{cls}", "result": word,
        "champ": gd.champ_icon(gd.champion(me.get("champion_id"))), "level": me.get("level") or "",
        "sr": _sr(me, gd),
        "k": _int(me.get("kills")), "d": _int(me.get("deaths")), "a": _int(me.get("assists")),
        "ratio": kda(me.get("kills"), me.get("deaths"), me.get("assists")),
        "ratio_cls": kda_cls(me.get("kills"), me.get("deaths"), me.get("assists")),
        "kp": pct(kp, 0) if kp is not None else "",
        "share": pct(share, 0) if share is not None else "",
        "share_w": min(100, round(share * 200)) if share is not None else 0,  # 50% of the team's damage fills the bar
        "damage": _fmt_int(me.get("damage")),
        "cs": _int(me.get("cs")), "cspm": f"{_int(me.get('cs')) / minutes:.1f}",
        "items": _slots(me.get("items"), gd, me.get("role_item")),
        "tags": tags,
        "foot": foot,
        "dur": duration(secs), "queue": game.get("queue") or "", "ago": ago(game.get("created_at")),
        "score": f"{float(score):.1f}" if score not in (None, "") and float(score) > 0 else "",
        "score_cls": score_cls(score), "score_rank": me.get("score_rank") or "",
        "teams": players,
    }
    line = " ".join(x for x in [
        f"[{word}]", me.get("champion_name") or "?", f"{card['k']}/{card['d']}/{card['a']}", f"KDA {card['ratio']}",
        f"参团{card['kp']}" if card["kp"] else "", f"伤害{card['share']}" if card["share"] else "",
        f"{card['cs']}补兵", "·".join(t["text"] for t in tags), "|", foot,
    ] if x)
    return card, line


def add_match_cards(doc: Doc, title: str, games: list[dict], gd: GameData, note: str = "") -> None:
    cards, lines = [], []
    for g in games:
        built = match_card(g, gd)
        if built:
            cards.append(built[0])
            lines.append(built[1])
    if cards:
        doc.add_cards(title, cards, lines, note)


# ---------------------------------------------------------------- team tables
def team_panel(t: dict, game: dict, gd: GameData, max_dmg: int, max_taken: int) -> tuple[dict, list[str]]:
    secs = _int(game.get("duration"))
    secs = secs // 1000 if secs > 100000 else secs
    minutes = max(secs / 60, 1)
    tk = _int(t.get("kills")) or sum(_int(p.get("kills")) for p in t["players"])
    deaths = sum(_int(p.get("deaths")) for p in t["players"])
    assists = sum(_int(p.get("assists")) for p in t["players"])
    tags = compute_tags(game)
    side = SIDE.get(t.get("key"), "")
    rows, lines = [], []
    head = f"{'胜利' if t['win'] else '失败'} {t.get('label') or side}".strip()
    lines.append(f"{head} {tk}/{deaths}/{assists} · {big(t.get('gold'))} 金")
    for p in t["players"]:
        k, d, a = _int(p.get("kills")), _int(p.get("deaths")), _int(p.get("assists"))
        kp = min(1.0, (k + a) / tk) if tk else None
        ptags = tags.get(id(p), [])
        row = {
            "me": bool(p.get("is_target")),
            "champ": gd.champ_icon(gd.champion(p.get("champion_id"))), "level": p.get("level") or "",
            "sr": _sr(p, gd),
            "name": p.get("game_name") or "?", "tag": f"#{p['tag_line']}" if p.get("tag_line") else "",
            "name_zh": any(ord(ch) >= 0x2E80 for ch in str(p.get("game_name") or "")),
            "pos": POSITIONS.get(p.get("position") or "", ""),
            "role": gd.role_icon(p.get("position")),
            "kda": f"{k}/{d}/{a}", "kp": pct(kp, 0) if kp is not None else "",
            "k": k, "d": d, "a": a,
            "ratio": kda(k, d, a), "ratio_cls": kda_cls(k, d, a),
            "dmg": _fmt_int(p.get("damage")), "dmg_w": round(100 * _int(p.get("damage")) / max_dmg) if max_dmg else 0,
            "taken": _fmt_int(p.get("taken")), "taken_w": round(100 * _int(p.get("taken")) / max_taken) if max_taken else 0,
            "cs": _int(p.get("cs")), "cspm": f"{_int(p.get('cs')) / minutes:.1f}",
            "gold": big(p.get("gold")), "goldpm": f"{_int(p.get('gold')) / minutes:.1f}",
            "gold_full": _fmt_int(p.get("gold")),
            "items": _slots(p.get("items"), gd, p.get("role_item")),
            "tags": ptags,
            "mvp": any(x["k"] == "mvp" for x in ptags), "ace": any(x["k"] == "ace" for x in ptags),
            "score": f"{float(p['score']):.1f}" if p.get("score") is not None else "",
            "score_cls": score_cls(p.get("score")),
        }
        rows.append(row)
        lines.append(" ".join(x for x in [
            ("▶" if row["me"] else " "), p.get("champion_name") or "?", row["name"], row["kda"],
            f"({row['kp']})" if row["kp"] else "", f"伤害{row['dmg']}", f"{row['cs']}补兵", f"{row['gold']}金",
            f"评分{row['score']}" if row["score"] else "", "·".join(x["text"] for x in ptags),
        ] if x))
    obj = [(label, t.get(key)) for key, label in (("towers", "塔"), ("dragons", "龙"), ("barons", "男爵"), ("hordes", "巢虫")) if t.get(key) is not None]
    red = t.get("key") == "RED"
    obj_i = [{"icon": gd.obj_icon(kind, red), "label": label, "n": t.get(key)}
             for key, label, kind in (("towers", "塔", "tower"), ("dragons", "龙", "dragon"), ("barons", "男爵", "baron"), ("hordes", "巢虫", ""))
             if t.get(key) is not None]
    # OP.GG lists distinct bans only (skipped / duplicate bans are dropped), so pad to five slots.
    bans = [gd.champ_icon(gd.champion(b)) for b in (t.get("bans") or [])[:5]]
    bans += [None] * (5 - len(bans))
    if obj:
        lines[0] += " · " + " ".join(f"{a}{b}" for a, b in obj)
    ban_names = [gd.display_name(gd.champion(b)).split(" ")[0] for b in t.get("bans") or [] if gd.champion(b)]
    if ban_names:
        lines[0] += " · 禁用 " + "、".join(ban_names)
    panel = {
        "cls": "tm-win" if t["win"] else "tm-lose", "title": head, "logo": t.get("logo"), "kda": f"{tk}/{deaths}/{assists}",
        "gold": big(t.get("gold")), "gold_icon": gd.obj_icon("gold"), "obj": obj, "obj_i": obj_i, "bans": bans, "rows": rows,
        "win": bool(t["win"]), "side": SIDE_KEY.get(t.get("key"), ""), "side_zh": side,
        "name": t.get("label") or side or "?", "name_zh": not t.get("label"),
        "gold_full": _fmt_int(t.get("gold") or sum(_int(p.get("gold")) for p in t["players"])),
    }
    return panel, lines


def build_game_view(game: dict, gd: GameData, theme: str = "lol", tz: str = "Asia/Shanghai", created: float | None = None) -> View:
    teams = game.get("teams") or []
    players = [p for t in teams for p in t["players"]]
    target = next((p for p in players if p.get("is_target")), None)
    target_team = next((t for t in teams if target in t["players"]), None) if target else None
    head = ("胜利" if target_team["win"] else "失败") if target_team is not None else "对局详情"
    doc = Doc(
        title=f"{head} · {game.get('queue') or '-'} · {duration(game.get('duration'))}",
        subtitle=" · ".join(x for x in [
            f"{target['game_name']}#{target['tag_line']}" if target and target.get("game_name") else "",
            local_time(game.get("created_at"), tz, "%Y-%m-%d %H:%M"),
            game.get("map") or "",
            f"平均段位 {game['avg_tier']}" if game.get("avg_tier") else "",
        ] if x),
        icon=gd.champ_icon(gd.champion(target["champion_id"])) if target else None,
        theme=theme,
        width=920,
    )
    doc.hero = game_hero(game, gd, tz)
    max_dmg = max([_int(p.get("damage")) for p in players] or [0])
    max_taken = max([_int(p.get("taken")) for p in players] or [0])
    for i, t in enumerate(teams):
        panel, lines = team_panel(t, game, gd, max_dmg, max_taken)
        doc.add_team(panel, lines)
        if i == 0 and len(teams) > 1:
            doc.sections[-1].title = "双方数据"
    doc.footer = freshness(created or time.time()) + (" · 数据来自国服客户端" if game.get("source") == "sgp" else "")
    if target:
        name = str(target.get("champion_name") or "").split(" ")[0]
        if name:
            doc.tip(f"/lol 英雄 {name}", "本局英雄的出装与克制")
        doc.tip("/lol 对局 2", "看上一场")
        doc.tip("/lol 战绩", "返回战绩列表")
    return View("lol_game", game, doc, created or time.time())

