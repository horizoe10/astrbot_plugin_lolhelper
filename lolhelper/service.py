"""Query layer: fetch OP.GG data, normalise it and build Doc views."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from .cache import TTLCache
from .constants import (
    GAME_MODES,
    OPGG_REGIONS,
    POSITION_FROM_API,
    POSITIONS,
)
from .doc import Cell, Doc
from .dsl import dig
from .errors import NotFoundError, UpstreamError, UsageError
from .fmt import (
    STAT_MODS,
    ago,
    big,
    duration,
    freshness,
    kda,
    kda_cls,
    local_time,
    num,
    pct,
    queue_type,
    rank_cls,
    rate_cls,
    tier_emblem,
    tier_pill,
    tier_text,
)
from .gamedata import GameData
from .mcp_client import OpggMcpClient


@dataclass
class ServiceConfig:
    lang: str = "zh_CN"
    timezone: str = "Asia/Shanghai"
    default_region: str = "kr"
    match_count: int = 8
    match_details: bool = True  # fetch each game's detail for the match list (10 players, KP, damage share)
    ttl_meta: float = 3 * 3600
    ttl_build: float = 3600
    ttl_profile: float = 300
    ttl_matches: float = 120
    ttl_esports: float = 300
    ttl_tft: float = 3600


@dataclass
class View:
    kind: str
    data: dict[str, Any]
    doc: Doc
    created_at: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


RESULT_ZH = {"WIN": ("胜", "win"), "LOSE": ("负", "lose")}

F_CHAMPION = [
    "data.summary.average_stats.{play,win_rate,pick_rate,ban_rate,kda,tier,rank}",
    "data.summary.positions[].name",
    "data.summary.positions[].stats.{role_rate,win_rate}",
    "data.runes.{primary_page_id,primary_page_name,primary_rune_ids[],primary_rune_names[],secondary_page_id,secondary_page_name,secondary_rune_ids[],secondary_rune_names[],stat_mod_ids[],play,win,pick_rate}",
    "data.starter_items.{ids[],ids_names[],pick_rate}",
    "data.core_items.{ids[],ids_names[],pick_rate,play,win}",
    "data.boots.{ids[],ids_names[],pick_rate}",
    "data.fourth_items[].{ids[],ids_names[],pick_rate}",
    "data.fifth_items[].{ids[],ids_names[],pick_rate}",
    "data.sixth_items[].{ids[],ids_names[],pick_rate}",
    "data.skills.{order[],pick_rate}",
    "data.skill_masteries.{ids[],pick_rate}",
    "data.summoner_spells.{ids[],pick_rate}",
    "data.weak_counters[].{champion_id,champion_name,my_win_rate,play}",
    "data.strong_counters[].{champion_id,champion_name,my_win_rate,play}",
]

F_PROFILE = [
    "data.summoner.{game_name,tagline,level,profile_image_url}",
    "data.summoner.league_stats[].{game_type,win,lose,is_hot_streak}",
    "data.summoner.league_stats[].tier_info.{tier,division,lp,tier_image_url}",
    "data.summoner.ladder_rank.{rank,total}",
    "data.summoner.most_champions.champion_stats[].{id,champion_name,play,win,lose,kill,death,assist}",
]

F_MATCHES = [
    "data.game_history[].{id,created_at,game_type,game_length_second,game_map}",
    "data.game_history[].average_tier_info.{tier,division}",
    "data.game_history[].participants[].{champion_id,champion_name,position,items[],spells[]}",
    "data.game_history[].participants[].rune.{primary_rune_id,secondary_page_id}",
    "data.game_history[].participants[].stats.{kill,death,assist,champion_level,result,minion_kill,neutral_minion_kill,op_score,op_score_rank,total_damage_dealt_to_champions}",
]


def _lane_fields(positions: list[str]) -> list[str]:
    return [
        f"data.positions.{p}[].{{champion,tier,rank,rank_prev,win_rate,pick_rate,ban_rate,kda,role_rate}}"
        for p in positions
    ]


LIVE_STATUS = ("RUNNING", "IN_PROGRESS", "LIVE", "STARTED")
LEAGUE_LABEL = {"worlds": "Worlds", "first stand": "First Stand", "fst": "First Stand", "lta south": "LTA South", "lta north": "LTA North"}


def league_label(key: str) -> str:
    return LEAGUE_LABEL.get(key, key.upper())


def versus_row(g: dict, tz: str) -> tuple[dict, str]:
    """One esports match as a symmetric home / score / away row plus a plain-text line."""
    home, away = g.get("homeTeam") or {}, g.get("awayTeam") or {}
    hs, as_ = g.get("homeScore"), g.get("awayScore")
    status = g.get("status") or ""
    home_win = status == "FINISHED" and (hs or 0) > (as_ or 0)
    away_win = status == "FINISHED" and (as_ or 0) > (hs or 0)
    if status == "FINISHED":
        score, score_cls, state = f"{hs} : {as_}", "done", "已结束"
    elif status in LIVE_STATUS:
        score, score_cls, state = f"{hs or 0} : {as_ or 0}", "live", "进行中"
    else:
        score, score_cls, state = "VS", "todo", "未开始"
    h_name = home.get("acronym") or home.get("name") or "待定"
    a_name = away.get("acronym") or away.get("name") or "待定"
    when = local_time(g.get("scheduledAt"), tz)
    bo = f"BO{g.get('numberOfGames')}" if g.get("numberOfGames") else ""
    stage = str(g.get("name") or "").split(":")[0].strip()
    row = {
        "time": when, "league": g.get("league") or "", "stage": stage, "bo": bo, "state": state,
        "score": score, "score_cls": score_cls,
        "home": {"name": h_name, "logo": home.get("image_url"), "win": home_win, "lose": away_win},
        "away": {"name": a_name, "logo": away.get("image_url"), "win": away_win, "lose": home_win},
    }
    return row, " ".join(x for x in [when, g.get("league") or "", h_name, score, a_name, bo, state] if x)


def rank_tile(queue: str, tier_s: str, wins: int, losses: int, emblem: str | None) -> Cell:
    total = wins + losses
    wr = pct(wins / total) if total else "-"
    return Cell(tier_s, img=emblem, cls="rank", sub=f"{queue} · {wins}胜{losses}负 · 胜率 {wr}")


class OpggService:
    def __init__(
        self,
        mcp: OpggMcpClient,
        gamedata: GameData,
        config: ServiceConfig | None = None,
        logger: logging.Logger | None = None,
    ):
        self.mcp = mcp
        self.gd = gamedata
        self.cfg = config or ServiceConfig()
        self.logger = logger or logging.getLogger("lolhelper")
        self.cache = TTLCache(1024)
        self.esp = None  # EsportsClient, set by the facade

    async def _cached(self, key: str, ttl: float, tool: str, args: dict) -> tuple[Any, float]:
        return await self.cache.get_or_load(key, ttl, lambda: self.mcp.call(tool, args))

    def _foot(self, created: float, extra: str = "") -> str:
        parts = [freshness(created)]
        if extra:
            parts.append(extra)
        return " · ".join(parts)

    # ================================================================ LoL meta
    async def lane_meta(self, positions: list[str] | None = None) -> tuple[dict[str, list[dict]], float]:
        positions = positions or list(POSITIONS)
        pos_arg = positions[0] if len(positions) == 1 else "all"
        key = f"lane:{pos_arg}"
        raw, created = await self._cached(
            key,
            self.cfg.ttl_meta,
            "lol_list_lane_meta_champions",
            {"lang": self.cfg.lang, "position": pos_arg, "desired_output_fields": _lane_fields(positions)},
        )
        out = {p: list(dig(raw, f"data.positions.{p}", []) or []) for p in positions}
        return out, created

    async def primary_position(self, champ: dict) -> str:
        try:
            meta, _ = await self.lane_meta()
        except Exception:
            return "mid"
        best, best_rate = "mid", -1.0
        for pos, rows in meta.items():
            for r in rows:
                c = self.gd.champion_by_name(r.get("champion", ""))
                if c and c["id"] == champ["id"]:
                    rate = float(r.get("role_rate") or r.get("pick_rate") or 0)
                    if rate > best_rate:
                        best, best_rate = pos, rate
        return best

    async def tier(self, position: str | None = None, limit: int = 15) -> View:
        await self.gd.ensure_lol()
        positions = [position] if position else list(POSITIONS)
        meta, created = await self.lane_meta(positions)
        per = limit if position else min(limit, 5)
        title = f"{POSITIONS[position]}梯队" if position else "版本梯队"
        doc = Doc(title=title, subtitle=f"OP.GG 单双排 · 版本 {self.gd.version}", theme="lol")
        data: dict[str, Any] = {"positions": {}}
        for pos in positions:
            rows = sorted(meta.get(pos, []), key=lambda r: r.get("rank") or 999)
            data["positions"][pos] = rows
            table = []
            for r in rows[:per]:
                name = r.get("champion", "?")
                trend, trend_cls = "", ""
                if r.get("rank_prev") and r.get("rank"):
                    diff = int(r["rank_prev"]) - int(r["rank"])
                    if diff:
                        trend, trend_cls = (f"↑{diff}", "up inl") if diff > 0 else (f"↓{-diff}", "down inl")
                t = r.get("tier")
                table.append([
                    Cell(str(r.get("rank", "")), cls=rank_cls(r.get("rank")), sub=trend, sub_cls=trend_cls),
                    Cell(name, img=self.gd.champ_icon_by_name(name)),
                    Cell(("OP" if str(t) == "0" else f"T{t}") if t is not None else "-", cls=tier_pill(t)),
                    Cell(pct(r.get("win_rate")), cls=rate_cls(r.get("win_rate"), 0.52, 0.48)),
                    pct(r.get("pick_rate")),
                    pct(r.get("ban_rate")),
                ])
            doc.add_table(
                POSITIONS[pos] if not position else "",
                ["#", "英雄", "梯队", "胜率", "登场", "禁用"],
                table,
            )
        doc.footer = self._foot(created)
        doc.link = "https://op.gg/lol/champions" + (f"?position={position}" if position else "")
        return View("lol_tier", data, doc, created)

    async def champion(self, query: str, position: str | None = None, mode: str | None = None) -> View:
        await self.gd.ensure_lol()
        champ = self.gd.resolve_champion(query)
        mode = mode or "ranked"
        # OP.GG has no separate ARAM Mayhem build data: builds come from ARAM, plus Mayhem augments.
        api_mode = "aram" if mode == "mayhem" else mode
        if api_mode in ("urf", "nexus_blitz"):
            api_pos = "none"
        elif api_mode == "aram":
            api_pos = "mid"  # ARAM ignores lanes but the API needs a real one
        else:
            api_pos = position or await self.primary_position(champ)
        key = f"champ:{champ['key']}:{api_pos}:{api_mode}"
        try:
            build_task = self._cached(
                key,
                self.cfg.ttl_build,
                "lol_get_champion_analysis",
                {
                    "game_mode": api_mode,
                    "champion": champ["key"].upper(),
                    "position": api_pos,
                    "lang": self.cfg.lang,
                    "desired_output_fields": F_CHAMPION,
                },
            )
            if mode == "mayhem":
                (raw, created), augments = await asyncio.gather(build_task, self.mayhem_augments(champ))
            else:
                raw, created = await build_task
                augments = None
        except UpstreamError as exc:
            if api_pos == "none" and "position" in str(exc).lower() or "位置" in str(exc):
                raise UsageError(f"OP.GG 暂不提供{GAME_MODES.get(mode, mode)}的英雄数据") from exc
            raise
        d = dig(raw, "data", {}) or {}
        stats = dig(d, "summary.average_stats", {}) or {}
        pos_label = POSITIONS.get(api_pos, "") if mode in ("ranked", "flex") else ""
        title = self.gd.display_name(champ)
        subtitle = " · ".join(x for x in [pos_label, GAME_MODES.get(mode, mode), f"版本 {self.gd.version}"] if x)
        doc = Doc(title=title, subtitle=subtitle, icon=self.gd.champ_icon(champ), theme="lol")
        t = stats.get("tier")
        if t is not None:
            doc.badge("OP" if str(t) == "0" else f"T{t}", tier_pill(t))
        doc.badge(f"胜率 {pct(stats.get('win_rate'))}", rate_cls(stats.get("win_rate")))
        doc.badge(f"登场 {pct(stats.get('pick_rate'))}")
        if stats.get("ban_rate") is not None:
            doc.badge(f"禁用 {pct(stats.get('ban_rate'))}")
        if stats.get("kda") is not None:
            doc.badge(f"KDA {num(stats.get('kda'))}")
        if stats.get("rank"):
            doc.badge(f"排名 #{stats.get('rank')}")

        runes = d.get("runes") or {}
        if runes:
            self._add_runes(doc, runes)

        def tiles_of(block: dict | None, label: str = "") -> list[Cell]:
            if not block:
                return []
            ids = block.get("ids") or []
            names = block.get("ids_names") or []
            out = []
            for i, name in enumerate(names or [self.gd.item_name(x) for x in ids]):
                iid = ids[i] if i < len(ids) else None
                out.append(Cell(text=name, img=self.gd.item_icon(iid) if iid else None, sub=label if i == 0 else ""))
            return out

        core = d.get("core_items") or {}
        core_tiles = tiles_of(d.get("starter_items"), "出门") + tiles_of(core, "核心") + tiles_of(d.get("boots"), "鞋子")
        if core_tiles:
            note = ""
            if core.get("play"):
                note = f"核心三件套选用率 {pct(core.get('pick_rate'))} · 胜率 {pct(core['win'] / core['play'])}"
            doc.add_tiles("推荐出装", core_tiles, note)
        rows, lines = [], []
        for slot, label in (("fourth_items", "第四件"), ("fifth_items", "第五件"), ("sixth_items", "第六件")):
            opts = []
            for opt in (d.get(slot) or [])[:3]:
                ids = opt.get("ids") or []
                names = opt.get("ids_names") or [self.gd.item_name(x) for x in ids]
                if names:
                    opts.append({"icon": self.gd.item_icon(ids[0]) if ids else None, "name": names[0], "sub": pct(opt.get("pick_rate"))})
            if opts:
                rows.append({"label": label, "items": opts})
                lines.append(f"{label}：" + " / ".join(f"{o['name']}({o['sub']})" for o in opts))
        doc.add_block("后续装备", "iconrows", {"rows": rows}, lines)
        await self._add_skills(doc, champ, d)
        counter_rows, counter_lines = [], []
        for key_name, label, cls in (("weak_counters", "难对付", "lose"), ("strong_counters", "好打", "win")):
            items = []
            for c in (d.get(key_name) or [])[:5]:
                name = c.get("champion_name", "?")
                icon = self.gd.champ_icon(self.gd.champion(c.get("champion_id"))) if c.get("champion_id") else self.gd.champ_icon_by_name(name)
                items.append({"icon": icon, "name": name, "sub": f"胜率 {pct(c.get('my_win_rate'))} · {big(c.get('play'))}场", "cls": cls})
            if items:
                counter_rows.append({"label": label, "items": items})
                counter_lines.append(f"{label}：" + " / ".join(f"{i['name']}({i['sub']})" for i in items))
        doc.add_block("对线克制", "iconrows", {"rows": counter_rows}, counter_lines, note="胜率为本英雄对阵该英雄时的胜率")
        doc.footer = self._foot(created, f"样本 {big(stats.get('play'))} 场")
        if augments is not None:
            self._add_augments(doc, augments)
            doc.footer += " · 出装与符文为极地大乱斗数据"
        lane = api_pos if api_pos in POSITIONS else ""
        doc.link = f"https://op.gg/lol/champions/{champ['key'].lower()}/build" + (f"/{lane}" if lane else "")
        data = {"champion": champ, "position": api_pos, "mode": mode, "analysis": d}
        return View("lol_champion", data, doc, created)

    # ============================================================== summoners
    def _add_runes(self, doc: Doc, runes: dict) -> None:
        """Rune page like the in-game editor: trees, keystone, minor runes and stat shards."""
        gd = self.gd

        def runes_of(ids_key: str, names_key: str) -> list[dict]:
            ids = runes.get(ids_key) or []
            names = runes.get(names_key) or []
            out = []
            for i, rid in enumerate(ids or [None] * len(names)):
                name = names[i] if i < len(names) else gd.rune_name(rid)
                out.append({"icon": gd.rune_icon(rid), "name": name or gd.rune_name(rid)})
            return out

        primary = runes_of("primary_rune_ids", "primary_rune_names")
        secondary = runes_of("secondary_rune_ids", "secondary_rune_names")
        shards = [{"icon": gd.rune_icon(m), "name": STAT_MODS.get(int(m), gd.rune_name(m)) if str(m).isdigit() else str(m)}
                  for m in runes.get("stat_mod_ids") or []]
        wr = runes.get("win") / runes["play"] if runes.get("play") else None
        block = {
            "primary": {"icon": gd.rune_icon(runes.get("primary_page_id")), "name": runes.get("primary_page_name", ""),
                        "keystone": primary[0] if primary else None, "runes": primary[1:]},
            "secondary": {"icon": gd.rune_icon(runes.get("secondary_page_id")), "name": runes.get("secondary_page_name", ""), "runes": secondary},
            "shards": shards,
            "stats": " · ".join(x for x in [f"使用率 {pct(runes.get('pick_rate'))}", f"胜率 {pct(wr)}" if wr is not None else ""] if x),
        }
        lines = [
            f"主系 · {block['primary']['name']}：" + " / ".join(r["name"] for r in primary),
            f"副系 · {block['secondary']['name']}：" + " / ".join(r["name"] for r in secondary),
            "属性碎片：" + " / ".join(s["name"] for s in shards),
            block["stats"],
        ]
        doc.add_block("符文", "runes", block, lines)

    async def _add_skills(self, doc: Doc, champ: dict, d: dict) -> None:
        """Summoner spells, max order with skill icons and the 15-level skill grid."""
        skills = d.get("skills") or {}
        mast = d.get("skill_masteries") or {}
        spells = d.get("summoner_spells") or {}
        icons = {s["key"]: s for s in await self.gd.champion_spells(champ)}
        order = [str(x).upper() for x in (skills.get("order") or [])[:18]]
        grid = []
        for key in "QWER":
            meta = icons.get(key, {})
            grid.append({"key": key, "icon": meta.get("icon"), "name": meta.get("name", key),
                         "levels": [i + 1 if k == key else 0 for i, k in enumerate(order)]})
        block = {
            "spells": [{"icon": self.gd.spell_icon(s), "name": self.gd.spell_name(s)} for s in spells.get("ids") or []],
            "spells_pick": pct(spells.get("pick_rate")) if spells.get("pick_rate") is not None else "",
            "max": [{"key": k, "icon": icons.get(str(k).upper(), {}).get("icon")} for k in mast.get("ids") or []],
            "max_pick": pct(mast.get("pick_rate")) if mast.get("pick_rate") is not None else "",
            "grid": grid,
            "length": len(order),
        }
        lines = [
            "召唤师技能：" + " + ".join(s["name"] for s in block["spells"]) + (f"（{block['spells_pick']}）" if block["spells_pick"] else ""),
            "主升顺序：" + " > ".join(m["key"] for m in block["max"]),
            "加点：" + " ".join(order[:15]),
        ]
        doc.add_block("技能", "skills", block, lines)

    async def mayhem_augments(self, champ: dict) -> list[dict]:
        """ARAM Mayhem augment stats for one champion (OP.GG) merged with names / rarity / icons (CommunityDragon)."""
        try:
            raw, _ = await self._cached(
                f"mayhem:{champ['id']}",
                self.cfg.ttl_build,
                "lol_list_aram_augments",
                {
                    "champion_id": int(champ["id"]),
                    "lang": self.cfg.lang,
                    "desired_output_fields": ["data.augments[].{id,name,tier,performance,popular}"],
                },
            )
        except UpstreamError as exc:
            self.logger.info(f"[lolhelper] 海克斯强化数据获取失败：{exc}")
            return []
        out = []
        for a in dig(raw, "data.augments", []) or []:
            meta = self.gd.augment(a.get("id"))
            name = a.get("name") or meta.get("name")
            if not name:
                continue
            out.append({
                "id": a.get("id"),
                "name": name,
                "rarity": meta.get("rarity", ""),
                "icon": meta.get("icon"),
                "performance": float(a.get("performance") or 0),
                "popular": float(a.get("popular") or 0),
            })
        return out

    def _add_augments(self, doc: Doc, augments: list[dict], per_group: int = 6, min_pick: float = 0.02) -> None:
        """Group by in-game rarity, rank by OP.GG performance score, skip tiny samples."""
        groups = (("prismatic", "棱彩"), ("gold", "黄金"), ("silver", "白银"))
        eligible = [a for a in augments if a["popular"] >= min_pick]
        if not eligible:
            doc.add_text("海克斯强化推荐", "OP.GG 暂无该英雄的海克斯大乱斗强化数据")
            return
        for rarity, label in groups:
            picks = sorted((a for a in eligible if a["rarity"] == rarity), key=lambda a: a["performance"], reverse=True)[:per_group]
            doc.add_tiles(
                f"海克斯强化 · {label}",
                [Cell(a["name"], img=a["icon"], cls=f"aug aug-{rarity}", sub=f"强度 {a['performance']:.0f} · {pct(a['popular'], 0)}") for a in picks],
            )
        others = sorted((a for a in eligible if a["rarity"] not in ("prismatic", "gold", "silver")), key=lambda a: a["performance"], reverse=True)[:per_group]
        doc.add_tiles("海克斯强化", [Cell(a["name"], img=a["icon"], cls="aug", sub=f"强度 {a['performance']:.0f} · {pct(a['popular'], 0)}") for a in others])
        if doc.sections:
            doc.sections[-1].note = doc.sections[-1].note or f"强度为 OP.GG 强度分，百分比为选取率；已排除选取率低于 {pct(min_pick, 0)} 的强化"

    # ============================================================== summoners
    async def _profile(self, name: str, tag: str, region: str, ttl: float | None = None) -> tuple[dict, float]:
        raw, created = await self._cached(
            f"profile:{region}:{name.lower()}#{tag.lower()}",
            self.cfg.ttl_profile if ttl is None else ttl,
            "lol_get_summoner_profile",
            {"game_name": name, "tag_line": tag, "region": region, "lang": self.cfg.lang, "desired_output_fields": F_PROFILE},
        )
        summ = dig(raw, "data.summoner")
        if not summ:
            raise NotFoundError(f"找不到召唤师 {name}#{tag}（{OPGG_REGIONS.get(region, region)}）")
        return summ, created

    async def recent_matches(self, name: str, tag: str, region: str, count: int = 8, ttl: float | None = None) -> tuple[list[dict], float]:
        count = max(1, min(int(count or 8), 20))
        raw, created = await self._cached(
            f"matches:{region}:{name.lower()}#{tag.lower()}:{count}",
            self.cfg.ttl_matches if ttl is None else ttl,
            "lol_list_summoner_matches",
            {"game_name": name, "tag_line": tag, "region": region, "lang": self.cfg.lang, "limit": count, "desired_output_fields": F_MATCHES},
        )
        games = dig(raw, "data.game_history", []) or []
        return [self._norm_match(g) for g in games], created

    def _norm_match(self, g: dict) -> dict:
        from .game import MAPS

        p = (g.get("participants") or [{}])[0] or {}
        st = p.get("stats") or {}
        rune = p.get("rune") or {}
        avg = g.get("average_tier_info") or {}
        return {
            "id": g.get("id"),
            "created_at": g.get("created_at"),
            "queue": queue_type(g.get("game_type")),
            "map": MAPS.get(g.get("game_map"), ""),
            "duration": g.get("game_length_second"),
            "champion_id": p.get("champion_id"),
            "champion_name": p.get("champion_name") or self.gd.display_name(self.gd.champion(p.get("champion_id"))),
            "position": POSITION_FROM_API.get(str(p.get("position") or "").upper()),
            "items": p.get("items") or [],
            "spells": p.get("spells") or [],
            "keystone": rune.get("primary_rune_id"),
            "sub_style": rune.get("secondary_page_id"),
            "level": st.get("champion_level"),
            "kills": st.get("kill"),
            "deaths": st.get("death"),
            "assists": st.get("assist"),
            "cs": (st.get("minion_kill") or 0) + (st.get("neutral_minion_kill") or 0),
            "result": st.get("result"),
            "score": st.get("op_score"),
            "score_rank": st.get("op_score_rank"),
            "damage": st.get("total_damage_dealt_to_champions"),
            "avg_tier": tier_text(avg.get("tier"), avg.get("division")) if avg.get("tier") else "",
        }

    def match_rows(self, matches: list[dict]) -> list[list[Cell]]:
        rows = []
        for m in matches:
            res, cls = RESULT_ZH.get(str(m.get("result") or "").upper(), ("重开", "muted"))
            champ = self.gd.champion(m.get("champion_id")) if m.get("champion_id") else self.gd.champion_by_name(m.get("champion_name") or "")
            score = m.get("score")
            rank = m.get("score_rank")
            rows.append([
                Cell(res, cls=cls, sub=duration(m.get("duration"))),
                Cell(
                    m.get("champion_name") or "?",
                    img=self.gd.champ_icon(champ),
                    sub=POSITIONS.get(m.get("position") or "", ""),
                    imgs=[u for u in (self.gd.item_icon(i) for i in (m.get("items") or [])[:7]) if u],
                ),
                Cell(
                    f"{m.get('kills', 0)}/{m.get('deaths', 0)}/{m.get('assists', 0)}",
                    sub=f"KDA {kda(m.get('kills'), m.get('deaths'), m.get('assists'))}",
                    sub_cls=kda_cls(m.get("kills"), m.get("deaths"), m.get("assists")),
                ),
                Cell(str(m.get("cs") or 0), sub=f"{big(m.get('damage'))} 伤害" if m.get("damage") else ""),
                Cell(num(score, 1) if score is not None else "-", cls="gold" if rank == 1 else "", sub=f"第{rank}名" if rank else ""),
                Cell(m.get("queue") or "-", sub=ago(m.get("created_at"))),
            ])
        return rows

    @staticmethod
    def match_summary(matches: list[dict]) -> str:
        if not matches:
            return ""
        wins = sum(1 for m in matches if str(m.get("result")).upper() == "WIN")
        losses = sum(1 for m in matches if str(m.get("result")).upper() == "LOSE")
        k = sum(int(m.get("kills") or 0) for m in matches)
        d = sum(int(m.get("deaths") or 0) for m in matches)
        a = sum(int(m.get("assists") or 0) for m in matches)
        total = wins + losses
        wr = pct(wins / total) if total else "-"
        return f"近 {len(matches)} 场 {wins}胜{losses}负 · 胜率 {wr} · KDA {kda(k, d, a)}"

    async def summoner(self, name: str, tag: str, region: str | None = None, count: int | None = None, detail: bool = False) -> View:
        await self.gd.ensure_lol()
        region = region or self.cfg.default_region
        count = count or (3 if detail else self.cfg.match_count)
        results = await asyncio.gather(
            self._profile(name, tag, region),
            self.recent_matches(name, tag, region, count),
            return_exceptions=True,
        )
        if isinstance(results[0], BaseException):
            raise results[0]
        summ, created = results[0]
        matches: list[dict] = []
        if isinstance(results[1], BaseException):
            self.logger.warning(f"[lolhelper] 获取对局失败：{results[1]}")
        else:
            matches = results[1][0]
        riot_id = f"{summ.get('game_name') or name}#{summ.get('tagline') or tag}"
        doc = Doc(
            title=riot_id,
            subtitle=f"{OPGG_REGIONS.get(region, region)} · Lv.{summ.get('level', '?')}",
            icon=summ.get("profile_image_url"),
            theme="lol",
        )
        ranked_tiles = []
        for ls in summ.get("league_stats") or []:
            ti = ls.get("tier_info") or {}
            if not ti.get("tier"):
                continue
            w, l = int(ls.get("win") or 0), int(ls.get("lose") or 0)
            tier_s = tier_text(ti.get("tier"), ti.get("division"), ti.get("lp"))
            q = queue_type(ls.get("game_type"))
            img = ti.get("tier_image_url")
            if not img or img.endswith(".svg"):
                img = tier_emblem(ti.get("tier"))
            ranked_tiles.append(rank_tile(q, tier_s, w, l, img))
        ladder = summ.get("ladder_rank") or {}
        if ladder.get("rank"):
            top = pct(ladder["rank"] / ladder["total"], 2) if ladder.get("total") else ""
            doc.badge(f"天梯 #{ladder['rank']}" + (f"（前 {top}）" if top else ""))
        if ranked_tiles:
            doc.add_tiles("段位", ranked_tiles)
        else:
            doc.badge("本赛季未定级", "muted")
        if detail:
            most = (summ.get("most_champions") or {}).get("champion_stats") or []
            rows = []
            for c in most[:8]:
                play = int(c.get("play") or 0)
                rows.append([
                    Cell(c.get("champion_name") or "?", img=self.gd.champ_icon(self.gd.champion(c.get("id")))),
                    f"{play}场",
                    Cell(pct((c.get("win") or 0) / play) if play else "-", cls=rate_cls((c.get("win") or 0) / play, 0.55, 0.45) if play else ""),
                    Cell(kda(c.get("kill"), c.get("death"), c.get("assist")), cls=kda_cls(c.get("kill"), c.get("death"), c.get("assist"))),
                ])
            doc.add_table("本赛季常用英雄", ["英雄", "场次", "胜率", "KDA"], rows)
        if matches:
            from .game import add_match_cards, target_entry

            games = await self.games_for(matches, name, tag, region)
            entries = [target_entry(g) or m for g, m in zip(games, matches)]
            doc.width = 880
            add_match_cards(
                doc,
                f"最近 {len(matches)} 场",
                games,
                self.gd,
                note=self.match_summary(entries) + " · 发送「/lol 对局 序号」查看单局详情",
            )
        doc.footer = self._foot(created)
        doc.link = f"https://op.gg/lol/summoners/{region}/{quote(riot_id.replace('#', '-'))}"
        data = {"region": region, "summoner": summ, "matches": matches}
        return View("lol_summoner", data, doc, created)

    async def _game_raw(self, region: str, m: dict, name: str, tag: str) -> tuple[dict, float]:
        from .game import F_GAME

        raw, created = await self._cached(
            f"game:{region}:{m['id']}:{name.lower()}#{tag.lower()}",
            24 * 3600,
            "lol_get_summoner_game_detail",
            {
                "region": region,
                "lang": self.cfg.lang,
                "game_id": m["id"],
                "created_at": m["created_at"],
                "focus_riot_id": f"{name}#{tag}",
                "desired_output_fields": F_GAME,
            },
        )
        g = dig(raw, "data.game_detail")
        if not g:
            raise NotFoundError("没有拿到这局的详细数据")
        return g, created

    async def games_for(self, matches: list[dict], name: str, tag: str, region: str) -> list[dict]:
        """Full game dicts for match list entries; falls back to list-only data per game."""
        from .game import minimal_game, normalize_opgg_game

        if not self.cfg.match_details:
            return [minimal_game(m) for m in matches]
        results = await asyncio.gather(*(self._game_raw(region, m, name, tag) for m in matches), return_exceptions=True)
        games = []
        for m, res in zip(matches, results):
            if isinstance(res, BaseException):
                self.logger.info(f"[lolhelper] 对局 {m.get('id')} 详情获取失败：{res}")
                games.append(minimal_game(m))
                continue
            game = normalize_opgg_game(res[0], self.gd)
            if not any(p.get("is_target") for t in game["teams"] for p in t["players"]):
                games.append(minimal_game(m))
            else:
                game["map"] = game.get("map") or m.get("map", "")
                games.append(game)
        return games

    async def game_detail(self, name: str, tag: str, region: str | None = None, index: int = 1) -> View:
        from .game import build_game_view, normalize_opgg_game

        await self.gd.ensure_lol()
        region = region or self.cfg.default_region
        matches, _ = await self.recent_matches(name, tag, region, max(index, 5))
        if not 1 <= index <= len(matches):
            raise UsageError(f"只找到最近 {len(matches)} 场对局，序号需要在 1~{len(matches)} 之间")
        m = matches[index - 1]
        g, created = await self._game_raw(region, m, name, tag)
        game = normalize_opgg_game(g, self.gd)
        game["map"] = game.get("map") or m.get("map", "")
        view = build_game_view(game, self.gd, "lol", self.cfg.timezone, created)
        view.doc.link = f"https://op.gg/lol/summoners/{region}/{quote(name + '-' + tag)}"
        return view

    # ================================================================ esports
    async def esports_games(self, mode: str, leagues: list[str] | None) -> tuple[list[dict], float]:
        """Schedules or results for several leagues merged into one list (newest first for results)."""
        async def one(league: str | None):
            args: dict[str, Any] = {"mode": mode, "limit": 50}
            if league:
                args["league"] = league
            raw, created = await self._cached(f"esports:{mode}:{league}", self.cfg.ttl_esports, "lol_esports_list_schedules", args)
            return (raw if isinstance(raw, list) else (dig(raw, "data", []) or [])), created

        parts = await asyncio.gather(*(one(lg) for lg in (leagues or [None])))
        seen, games = set(), []
        for gs, _ in parts:
            for g in gs:
                if g.get("id") in seen:
                    continue
                seen.add(g.get("id"))
                games.append(g)
        games.sort(key=lambda g: g.get("scheduledAt") or "", reverse=mode == "result")
        return games, min(c for _, c in parts)

    async def esports(self, mode: str = "schedule", league: str | list[str] | None = None, limit: int = 10) -> View:
        leagues = [league] if isinstance(league, str) else list(league or [])
        games, created = await self.esports_games(mode, leagues)
        title = ("赛果" if mode == "result" else "赛程") + (" · " + " / ".join(league_label(x) for x in leagues) if leagues else "")
        doc = Doc(title=f"英雄联盟{title}", subtitle="时间为北京时间" if self.cfg.timezone == "Asia/Shanghai" else self.cfg.timezone, theme="esports")
        rows, vs_rows, lines = [], [], []
        for i, g in enumerate(games[:limit], 1):
            row, line = versus_row(g, self.cfg.timezone)
            if mode == "result":
                row["no"] = i
                line = f"#{i} {line}"
            vs_rows.append(row)
            lines.append(line)
        if not vs_rows:
            doc.add_text("", "暂无" + ("近期赛果" if mode == "result" else "后续赛程"))
        else:
            doc.add_block("", "versus", {"rows": vs_rows}, lines,
                          note="发送「/lol 赛况 序号」查看每局阵容与数据" if mode == "result" else "")
        doc.footer = self._foot(created)
        doc.link = "https://esports.op.gg/schedules"
        return View("lol_esports", {"mode": mode, "league": leagues[0] if len(leagues) == 1 else None, "leagues": leagues, "games": games}, doc, created)

    async def esports_series_games(self, g: dict) -> tuple[list[dict], float]:
        if self.esp is None:
            raise UpstreamError("赛事数据接口未初始化")
        status = str(g.get("status") or "").upper()
        live = status in LIVE_STATUS
        played = int(g.get("homeScore") or 0) + int(g.get("awayScore") or 0) + (1 if live else 0)
        if played <= 0:
            return [], time.time()
        ttl = 60 if live else 6 * 3600
        return await self.cache.get_or_load(f"esgames:{g.get('id')}:{played}", ttl, lambda: self.esp.games(g.get("id"), played))

    async def esports_find(self, leagues: list[str] | None, index: int | None = None, team: str | None = None,
                           match_id: str | None = None) -> dict:
        if match_id:
            from .esports import match_to_schedule

            m = await self.esp.match(match_id) if self.esp else None
            if not m:
                raise NotFoundError(f"没有找到比赛 {match_id}")
            return match_to_schedule(m)
        results, _ = await self.esports_games("result", leagues)
        if index:
            if not 1 <= index <= len(results):
                raise UsageError(f"最近只有 {len(results)} 场赛果，序号需要在 1~{len(results)} 之间")
            return results[index - 1]
        schedule, _ = await self.esports_games("schedule", leagues)
        pool = [g for g in schedule if str(g.get("status") or "").upper() in LIVE_STATUS] + results
        if team:
            t = team.lower()
            pool = [g for g in pool if any(t in (str(x.get("acronym") or "").lower(), str(x.get("name") or "").lower())
                                           for x in (g.get("homeTeam") or {}, g.get("awayTeam") or {}))]
            if not pool:
                raise NotFoundError(f"最近没有找到 {team.upper()} 的比赛")
        if not pool:
            raise NotFoundError("最近没有比赛")
        return pool[0]

    async def esports_match(self, g: dict, game_no: int | None = None, headline: str = "", key: str = "") -> View:
        from .esports import build_esports_game_view, build_series_view

        games, created = await self.esports_series_games(g)
        if game_no:
            game = next((x for x in games if x.get("set") == game_no), None)
            if not game:
                raise NotFoundError(f"第 {game_no} 局暂无数据（本场共有 {len(games)} 局数据）")
            return build_esports_game_view(g, game, self.gd, self.cfg.timezone, created)
        return build_series_view(g, games, self.gd, self.cfg.timezone, created, headline, key)

    async def standings(self, league: str) -> View:
        raw, created = await self._cached(f"standings:{league}", self.cfg.ttl_esports, "lol_esports_list_team_standings", {"short_name": league})
        if not isinstance(raw, dict) or not raw.get("standings"):
            raise NotFoundError(f"没有找到 {league.upper()} 的积分榜")
        lg, serie, tour = raw.get("league") or {}, raw.get("serie") or {}, raw.get("tournament") or {}
        doc = Doc(
            title=f"{lg.get('shortName') or league.upper()} 积分榜",
            subtitle=" ".join(str(x) for x in [serie.get("year"), serie.get("season"), tour.get("name")] if x),
            icon=lg.get("imageUrl"),
            theme="esports",
        )
        rows = []
        for s in raw["standings"]:
            team = s.get("team") or {}
            rows.append([
                str(s.get("position", "")),
                Cell(team.get("acronym") or team.get("name", "?"), img=team.get("imageUrl"), sub=team.get("name", ""), cls="logo"),
                f"{s.get('win', 0)}-{s.get('lose', 0)}",
                f"{s.get('setWin', 0)}-{s.get('setLose', 0)}",
                str(s.get("point", "")),
            ])
        doc.add_table("", ["#", "战队", "大场", "小场", "积分"], rows)
        doc.footer = self._foot(created)
        return View("lol_standings", raw, doc, created)

