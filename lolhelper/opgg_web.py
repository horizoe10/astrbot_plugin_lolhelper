"""Champion statistics from OP.GG's website API (lol-api-champion.op.gg).

Unlike the MCP tools, this API takes a region (global / kr ...) and a rank range
(emerald_plus ...). Results are converted into the same shapes the MCP data uses
so the card builders in service.py work with either source.
"""

from __future__ import annotations

from typing import Any

from .errors import UpstreamError
from .gamedata import GameData
from .net import HttpClient

BASE = "https://lol-api-champion.op.gg/api"
HEADERS = {"Referer": "https://op.gg/", "Accept": "application/json"}

META_REGIONS: dict[str, str] = {"global": "全球", "kr": "韩服"}
# key -> (label, emblem tier used for the badge icon)
META_TIERS: dict[str, tuple[str, str]] = {
    "all": ("全部段位", ""),
    "gold_plus": ("黄金+", "GOLD"),
    "platinum_plus": ("铂金+", "PLATINUM"),
    "emerald_plus": ("翡翠+", "EMERALD"),
    "diamond_plus": ("钻石+", "DIAMOND"),
    "master_plus": ("大师+", "MASTER"),
    "master": ("超凡大师", "MASTER"),
    "grandmaster": ("傲世宗师", "GRANDMASTER"),
    "challenger": ("最强王者", "CHALLENGER"),
}
REGION_ALIASES = {"全球": "global", "global": "global", "全服": "global", "国际": "global",
                  "韩服": "kr", "kr": "kr", "韩国": "kr"}
TIER_ALIASES = {
    "全部": "all", "全段位": "all", "所有段位": "all", "all": "all",
    "黄金+": "gold_plus", "金+": "gold_plus", "黄金以上": "gold_plus",
    "铂金+": "platinum_plus", "白金+": "platinum_plus", "铂金以上": "platinum_plus",
    "翡翠+": "emerald_plus", "翠+": "emerald_plus", "翡翠以上": "emerald_plus",
    "钻石+": "diamond_plus", "钻+": "diamond_plus", "钻石以上": "diamond_plus",
    "大师+": "master_plus", "大师以上": "master_plus",
    "大师": "master", "超凡大师": "master", "宗师": "grandmaster", "傲世宗师": "grandmaster",
    "王者": "challenger", "最强王者": "challenger",
}
for _k in META_TIERS:
    TIER_ALIASES.setdefault(_k, _k)
# game modes that the website API serves; ARAM / URF have no rank split
WEB_MODES = {"ranked": "ranked", "flex": "flex", "aram": "aram", "mayhem": "aram", "urf": "urf"}
NO_TIER_MODES = {"aram", "urf"}
POS_FROM_WEB = {"TOP": "top", "JUNGLE": "jungle", "MID": "mid", "ADC": "adc", "SUPPORT": "support"}
POS_TO_WEB = {v: k.lower() for k, v in POS_FROM_WEB.items()}


def pick_scope(words: list[str]) -> tuple[str | None, str | None, list[str]]:
    """Take a region / rank range out of the command words, e.g. ["中单", "韩服", "钻石+"]."""
    region = tier = None
    rest = []
    for w in words:
        k = w.strip().lower().replace("＋", "+")
        if region is None and k in REGION_ALIASES:
            region = REGION_ALIASES[k]
        elif tier is None and k in TIER_ALIASES:
            tier = TIER_ALIASES[k]
        else:
            rest.append(w)
    return region, tier, rest


def scope_label(region: str, tier: str) -> str:
    return f"{META_REGIONS.get(region, region.upper())} · {META_TIERS.get(tier, (tier, ''))[0]}"


class OpggWebClient:
    def __init__(self, http: HttpClient):
        self.http = http

    async def _get(self, path: str, tier: str) -> tuple[Any, dict]:
        try:
            data = await self.http.get_json(f"{BASE}/{path}", params={"tier": tier}, headers=HEADERS, retries=1)
        except UpstreamError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            raise UpstreamError(f"OP.GG 网站数据请求失败：{exc}") from exc
        if not isinstance(data, dict) or "data" not in data:
            raise UpstreamError("OP.GG 网站数据格式异常")
        return data["data"], data.get("meta") or {}

    async def meta(self, region: str, mode: str, tier: str) -> tuple[list[dict], dict]:
        m = WEB_MODES.get(mode, mode)
        return await self._get(f"{region}/champions/{m}", "all" if m in NO_TIER_MODES else tier)

    async def champion(self, region: str, mode: str, champ_id: int | str, position: str | None, tier: str) -> tuple[dict, dict]:
        m = WEB_MODES.get(mode, mode)
        pos = "none" if m in NO_TIER_MODES or not position else POS_TO_WEB.get(position, position)
        return await self._get(f"{region}/champions/{m}/{champ_id}/{pos}", "all" if m in NO_TIER_MODES else tier)


# ------------------------------------------------------------------ converters
def _name(gd: GameData, cid) -> str:
    c = gd.champion(cid)
    return gd.display_name(c).split(" ")[0] if c else str(cid)


def lane_rows(data: list[dict], gd: GameData) -> dict[str, list[dict]]:
    """Website list -> {position: rows} in the MCP lane-meta row shape."""
    out: dict[str, list[dict]] = {p: [] for p in POS_FROM_WEB.values()}
    for c in data or []:
        for p in c.get("positions") or []:
            pos = POS_FROM_WEB.get(str(p.get("name")).upper())
            st = p.get("stats") or {}
            td = st.get("tier_data") or {}
            if not pos or not td.get("rank"):
                continue
            out[pos].append({
                "champion_id": c.get("id"), "champion": _name(gd, c.get("id")),
                "rank": td.get("rank"), "rank_prev": td.get("rank_prev"), "tier": td.get("tier"),
                "win_rate": st.get("win_rate"), "pick_rate": st.get("pick_rate"), "ban_rate": st.get("ban_rate"),
                "role_rate": st.get("role_rate"), "play": st.get("play"),
            })
    for rows in out.values():
        rows.sort(key=lambda r: r["rank"])
    return out


def _first(v) -> dict:
    return (v or [{}])[0] if isinstance(v, list) else (v or {})


def analysis(data: dict, position: str | None, gd: GameData) -> dict:
    """Website champion detail -> the MCP lol_get_champion_analysis "data" shape."""
    summary = data.get("summary") or {}
    stats = dict(summary.get("average_stats") or {})
    for p in summary.get("positions") or []:
        if position and POS_FROM_WEB.get(str(p.get("name")).upper()) == position:
            stats.update(p.get("stats") or {})
    td = stats.get("tier_data") or {}
    stats["tier"], stats["rank"] = td.get("tier", stats.get("tier")), td.get("rank", stats.get("rank"))

    runes = dict(_first(data.get("runes")))
    if runes:
        runes["primary_page_name"] = gd.rune_name(runes.get("primary_page_id"))
        runes["secondary_page_name"] = gd.rune_name(runes.get("secondary_page_id"))
    core, boots, starter = _first(data.get("core_items")), _first(data.get("boots")), _first(data.get("starter_items"))
    used = set(core.get("ids") or []) | set(boots.get("ids") or []) | set(starter.get("ids") or [])
    later = [x for x in data.get("last_items") or [] if x.get("ids") and x["ids"][0] not in used][:6]

    counters = [
        {"champion_id": c.get("champion_id"), "champion_name": _name(gd, c.get("champion_id")),
         "my_win_rate": (c.get("win") or 0) / c["play"], "play": c.get("play")}
        for c in data.get("counters") or [] if c.get("play")
    ]
    top = max((c["play"] for c in counters), default=0)
    counters = [c for c in counters if c["play"] >= max(20, top * 0.1)]
    return {
        "summary": {"average_stats": stats},
        "runes": runes,
        "core_items": core, "boots": boots, "starter_items": starter,
        "later_items": later,
        "summoner_spells": _first(data.get("summoner_spells")),
        "skills": _first(data.get("skills")),
        "skill_masteries": _first(data.get("skill_masteries")),
        "weak_counters": sorted(counters, key=lambda c: c["my_win_rate"])[:5],
        "strong_counters": sorted(counters, key=lambda c: -c["my_win_rate"])[:5],
    }
