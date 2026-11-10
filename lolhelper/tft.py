"""Teamfight Tactics views built on the OP.GG MCP tft_* tools."""

from __future__ import annotations

from typing import Any

from .doc import Cell, Doc
from .dsl import dig
from .errors import NotFoundError, UsageError
from .fmt import TFT_TIER_PILL, big, freshness, num, pct, rank_cls, relative_cls, strip_html
from .service import OpggService, View

AUGMENT_TIERS = {"silver": "白银", "gold": "黄金", "prismatic": "棱彩", "platinum": "棱彩"}
AUGMENT_PILL = {"silver": "tp ag-s", "gold": "tp ag-g", "prismatic": "tp ag-p", "platinum": "tp ag-p"}
OP_TIER_CLS = TFT_TIER_PILL
TRAIT_STYLE = {1: "铜", 2: "银", 3: "金", 4: "彩", 5: "彩", 6: "独"}
UNIT_NOTE = "★ 为核心棋子 · 边框颜色代表费用"


def _aug_tier(h: dict) -> str:
    return str(h.get("tier") or "").lower()


class TftService:
    def __init__(self, svc: OpggService):
        self.svc = svc
        self.gd = svc.gd
        self.mcp = svc.mcp

    async def _ready(self) -> None:
        await self.gd.ensure_tft(self.mcp)

    async def _art(self, api: str | None, banner: bool = True) -> str | None:
        """Banner art for a unit: its current-set art, else the League splash for League champions, else (banner)
        the season banner. League data is only needed for the splash, so a failure there just skips it."""
        art = self.gd.tft_art(api)
        if not art:
            try:
                await self.gd.ensure_lol()
                art = self.gd.tft_splash(api)
            except Exception:
                art = None
        return art or (self.gd.tft_banner()[0] if banner else None)

    def _season_banner(self, hero: dict) -> bool:
        """Put the season banner and Chinese season logo on a centered banner; False when the set has none."""
        bg, logo = self.gd.tft_banner()
        if bg:
            hero.update({"art": bg, "wide": True, "slogo": logo})
        return bool(bg)

    def _cost_cls(self, api: str | None, prefix: str = "u") -> str:
        cost = self.gd.tft_champion_cost(api) if api else None
        return f"{prefix}{min(int(cost), 5)}" if cost else ""

    def _enum(self, tool: str, prop: str) -> list[str] | None:
        t = self.mcp.tools.get(self.mcp.tool_name(tool))
        if not t:
            return None
        return dig(t, f"inputSchema.properties.{prop}.enum") or None

    def _unit_cell(self, unit_key: str, items: list[str] | None = None, core: bool = False, star: int | None = None) -> Cell:
        name = self.gd.tft_champion_name(unit_key)
        if star and star >= 3:
            name = "★" * min(star, 3) + name
        items = items or []
        sub = "、".join(self.gd.tft_item_name(i) for i in items)  # text / markdown output
        icons = [u for u in (self.gd.tft_item_icon(i) for i in items) if u]  # image output
        cls = " ".join(x for x in (self._cost_cls(unit_key), "core" if core else "") if x)
        return Cell(name, img=self.gd.tft_champion_icon(unit_key), cls=cls, sub=sub, imgs=icons)

    def _unit_icons(self, units: list[dict], key: str = "key") -> list:
        out = []
        for u in units:
            api = u.get(key) or u.get("characterId") or u.get("key")
            icon = self.gd.tft_champion_icon(api) if api else None
            if icon:
                cls = " ".join(x for x in (self._cost_cls(api, "c"), "core" if u.get("isCore") else "") if x)
                out.append({"src": icon, "cls": cls} if cls else icon)
        return out

    async def _decks(self) -> tuple[list[dict], float, dict]:
        raw, created = await self.svc._cached("tft:decks", self.svc.cfg.ttl_tft, "tft_list_meta_decks", {})
        decks = raw.get("data", []) if isinstance(raw, dict) else (raw or [])
        meta = raw.get("metadata", {}) if isinstance(raw, dict) else {}
        decks = sorted(decks, key=lambda d: float(dig(d, "stat.deck.avgPlacement", 9) or 9))
        return decks, created, meta

    def _deck_name(self, deck: dict) -> str:
        name = deck.get("name")
        if isinstance(name, dict):
            name = name.get("zh_CN") or name.get("en_US") or next(iter(name.values()), "?")
        return self.gd.tft_localize(str(name or "?"))

    async def meta(self, limit: int = 10) -> View:
        await self._ready()
        decks, created, meta = await self._decks()
        season = f"S{self.gd.tft.get('set', '?')} · {self.gd.tft.get('version') or ''}".strip(" ·")
        doc = Doc(title="云顶之弈 · 版本强势阵容", subtitle=season, theme="tft", width=820)
        doc.hero = {"layout": "center", "eyebrow": "TEAMFIGHT TACTICS", "title": "版本强势阵容", "range": season}
        if meta.get("gameStatCounts"):
            doc.badge(f"统计样本 {big(meta['gameStatCounts'])} 局", "acc")
        rows = []
        shown = decks[:limit]
        colours = relative_cls([dig(d, "stat.deck.avgPlacement") for d in shown])
        for i, d in enumerate(shown, 1):
            st = dig(d, "stat.deck", {}) or {}
            units = d.get("units") or []
            core = [u for u in units if u.get("isCore")]
            first = (core or units or [{}])[0].get("key")
            names = " ".join(("★" if u.get("isCore") else "") + self.gd.tft_champion_name(u["key"]) for u in units[:9] if u.get("key"))
            tier = dig(d, "stat.opTier", "") or ""
            rows.append([
                Cell(str(i), cls=rank_cls(i)),
                Cell(self._deck_name(d), sub=names, imgs=self._unit_icons(units[:10]), cls="iconsonly"),
                Cell(tier or "-", cls=OP_TIER_CLS.get(tier, "")),
                Cell(num(st.get("avgPlacement")), cls=colours[i - 1]),
                pct(st.get("top4Rate")),
                pct(st.get("winRate")),
            ])
        doc.add_table("", ["#", "阵容", "评级", "均名", "前四", "登顶"], rows, note=UNIT_NOTE, style="top3")
        if not self._season_banner(doc.hero) and shown:
            units = shown[0].get("units") or []
            carry = next((u for u in units if u.get("isCore")), units[0] if units else {})
            doc.hero.update({"art": await self._art(carry.get("key"), banner=False), "vivid": True})
        doc.tip("/tft 阵容 1", "第 1 名阵容的装备、羁绊与运营")
        top_units = [u for u in (shown[0].get("units") if shown else []) or [] if u.get("isCore")]
        if top_units:
            doc.tip(f"/tft 英雄 {self.gd.tft_champion_name(top_units[0]['key'])}", "核心英雄的最佳出装")
        doc.tip("/tft 强化 贷款", "按关键词搜索强化符文")
        doc.footer = freshness(created)
        doc.link = "https://op.gg/tft/meta-trends/comps"
        return View("tft_meta", {"decks": decks[:limit], "metadata": meta}, doc, created)

    async def deck(self, index: int) -> View:
        await self._ready()
        decks, created, _ = await self._decks()
        if not 1 <= index <= len(decks):
            raise UsageError(f"序号需要在 1~{len(decks)} 之间")
        d = decks[index - 1]
        st = dig(d, "stat.deck", {}) or {}
        tier = dig(d, "stat.opTier", "") or ""
        doc = Doc(title=self._deck_name(d), subtitle=f"版本阵容第 {index} 名", theme="tft", width=820)
        units = d.get("units") or []
        core = next((u for u in units if u.get("isCore")), units[0] if units else None)
        if core:
            doc.icon = self.gd.tft_champion_icon(core["key"])
        if tier:
            doc.badge(f"评级 {tier}", "hx " + OP_TIER_CLS.get(tier, ""))
        doc.badge(f"均名 {num(st.get('avgPlacement'))}", "hx")
        doc.badge(f"前四 {pct(st.get('top4Rate'))}", "hx")
        doc.badge(f"登顶 {pct(st.get('winRate'))}", "hx")
        doc.badge(f"出场 {pct(st.get('pickRate'), 2)}", "hx")
        traits_on = sorted((t for t in d.get("traits") or [] if t.get("key") and (t.get("style") or 0) > 0),
                           key=lambda t: (-(t.get("style") or 0), -(t.get("numUnits") or 0)))
        doc.hero = {
            "art": await self._art(core["key"]) if core else None, "icon": doc.icon, "name": self._deck_name(d),
            "chips": [{"text": f"版本第 {index} 名", "cls": "acc"}]
                     + [{"text": f"{self.gd.tft_trait_name(t['key'])} {t.get('numUnits', '')}", "img": self.gd.tft_trait_icon(t["key"])}
                        for t in traits_on[:3]],
            "side": {"small": "评级", "big": tier, "cls": OP_TIER_CLS.get(tier, "").replace("tp ", "")} if tier else None,
            "stats": [{"v": num(st.get("avgPlacement")), "k": "平均名次"}, {"v": pct(st.get("top4Rate")), "k": "前四率"},
                      {"v": pct(st.get("winRate")), "k": "登顶率"}, {"v": pct(st.get("pickRate"), 2), "k": "出场率"}],
        }
        doc.add_tiles("最终阵容", [self._unit_cell(u["key"], u.get("items"), u.get("isCore"), u.get("tier")) for u in units if u.get("key")],
                      note=UNIT_NOTE)
        doc.add_tiles(
            "羁绊",
            [
                Cell(
                    f"{self.gd.tft_trait_name(t['key'])} {t.get('numUnits', '')}",
                    img=self.gd.tft_trait_icon(t["key"]),
                    cls=f"trait s{min(int(t.get('style') or 1), 6)}",
                    sub=TRAIT_STYLE.get(t.get("style"), ""),
                )
                for t in traits_on
            ],
        )
        stages = []
        for key, label in (("early", "前期"), ("middle", "中期")):
            stage = d.get(key) or {}
            units_s = stage.get("units") or []
            names = [self.gd.tft_champion_name(u.get("characterId") or u.get("key", "")) for u in units_s]
            if names:
                stages.append([
                    Cell(f"{label}", sub=f"{stage.get('level', '?')} 级", cls="muted"),
                    Cell(" ".join(names), imgs=self._unit_icons(units_s, "characterId"), cls="bigstrip"),
                ])
        final_units = [u for u in units if u.get("key")]
        if stages and final_units:
            stages.append([
                Cell("成型", sub=f"{len(final_units)} 人口", cls="muted"),
                Cell(" ".join(self.gd.tft_champion_name(u["key"]) for u in final_units), imgs=self._unit_icons(final_units), cls="bigstrip"),
            ])
        doc.add_table("运营过渡", [], stages)
        if core:
            doc.tip(f"/tft 英雄 {self.gd.tft_champion_name(core['key'])}", "核心英雄的最佳出装")
            core_items = core.get("items") or []
            if core_items:
                doc.tip(f"/tft 装备 {self.gd.tft_item_name(core_items[0])}", "这件装备最适合给谁")
        if index < len(decks):
            doc.tip(f"/tft 阵容 {index + 1}", "下一个阵容")
        doc.footer = freshness(created)
        return View("tft_deck", d, doc, created)

    async def champion(self, query: str) -> View:
        await self._ready()
        api = self.gd.resolve_tft_champion(query, self._enum("tft_get_champion_item_build", "champion_id"))
        raw, created = await self.svc._cached(f"tft:champ:{api}", self.svc.cfg.ttl_tft, "tft_get_champion_item_build", {"champion_id": api})
        builds = raw.get("data", []) if isinstance(raw, dict) else (raw or [])
        if not builds:
            raise NotFoundError(f"暂无 {self.gd.tft_champion_name(api)} 的出装数据")
        cost = self.gd.tft_champion_cost(api)
        champ_meta = (self.gd.tft.get("champions") or {}).get(api, {})
        doc = Doc(
            title=self.gd.tft_champion_name(api),
            subtitle=" · ".join(x for x in [f"{cost} 费" if cost else "", " / ".join(champ_meta.get("traits") or [])] if x),
            icon=self.gd.tft_champion_icon(api),
            theme="tft",
            width=820,
        )
        total = builds[0].get("totalChampionCount")
        if total:
            doc.badge(f"样本 {big(total)} 次登场", "hx")
        best = builds[0]
        doc.hero = {
            "art": await self._art(api), "icon": doc.icon, "name": doc.title,
            "chips": [{"text": t} for t in champ_meta.get("traits") or []],
            "side": {"small": "费用", "big": str(cost), "cls": f"u{min(int(cost), 5)}"} if cost else None,
            "stats": [x for x in [{"v": big(total), "k": "登场次数"} if total else None,
                                  {"v": num(best.get("avgPlacement")), "k": "常用出装 · 均名"},
                                  {"v": pct(best.get("top4Rate")), "k": "前四率"}, {"v": pct(best.get("winRate")), "k": "登顶率"}] if x],
        }
        doc.add_tiles(
            "最常用三件套",
            [Cell(self.gd.tft_item_name(i), img=self.gd.tft_item_icon(i)) for i in best.get("itemNames") or []],
            note=f"均名 {num(best.get('avgPlacement'))} · 前四 {pct(best.get('top4Rate'))} · 登顶 {pct(best.get('winRate'))} · {big(best.get('itemCount'))} 次",
        )
        # Top 3 by win rate, ignoring tiny samples that would dominate the ranking.
        min_count = max(50, int((best.get("itemCount") or 0) * 0.05))
        eligible = [b for b in builds if (b.get("itemCount") or 0) >= min_count] or builds
        top = sorted(eligible, key=lambda b: float(b.get("winRate") or 0), reverse=True)[:3]
        rows = []
        for rank, b in enumerate(top, 1):
            items = b.get("itemNames") or []
            rows.append([
                Cell(str(rank), cls=rank_cls(rank)),
                Cell(" + ".join(self.gd.tft_item_name(i) for i in items), imgs=[u for u in (self.gd.tft_item_icon(i) for i in items) if u], cls="bigstrip"),
                Cell(pct(b.get("winRate")), cls="hi" if rank == 1 else ""),
                num(b.get("avgPlacement")),
                pct(b.get("top4Rate")),
                big(b.get("itemCount")),
            ])
        doc.add_table("登顶率最高的出装", ["#", "装备", "登顶", "均名", "前四", "次数"], rows, note=f"仅统计出现 {min_count} 次以上的组合")
        best_items = best.get("itemNames") or []
        if best_items:
            doc.tip(f"/tft 装备 {self.gd.tft_item_name(best_items[0])}", "这件装备最适合给谁")
        doc.tip("/tft 阵容", "版本强势阵容")
        doc.footer = freshness(created)
        return View("tft_champion", {"champion": api, "builds": builds}, doc, created)

    async def item(self, query: str) -> View:
        await self._ready()
        api = self.gd.resolve_tft_item(query, self._enum("tft_list_champions_for_item", "item_id"))
        raw, created = await self.svc._cached(f"tft:item:{api}", self.svc.cfg.ttl_tft, "tft_list_champions_for_item", {"item_id": api})
        rows_raw = raw if isinstance(raw, list) else (raw.get("data", []) if isinstance(raw, dict) else [])
        if not rows_raw:
            raise NotFoundError(f"暂无 {self.gd.tft_item_name(api)} 的数据")
        it = (self.gd.tft.get("items") or {}).get(api, {})
        recipe = " + ".join(self.gd.tft_item_name(x) for x in it.get("from") or [])
        doc = Doc(title=self.gd.tft_item_name(api), subtitle=f"合成：{recipe}" if recipe else "最适合的英雄", icon=self.gd.tft_item_icon(api),
                  theme="tft", width=820)
        rows = []
        shown = rows_raw[:15]
        top_cid = (shown[0].get("characterId") or "") if shown else ""
        art = None
        for r in shown[:5]:  # set-only units (no League champion) have no splash; use the next carrier's
            art = await self._art(r.get("characterId"), banner=False)
            if art:
                break
        art = art or self.gd.tft_banner()[0]
        doc.hero = {
            "art": art, "icon": doc.icon, "name": doc.title, "sub": "合成" if it.get("from") else "",
            "chips": [{"text": self.gd.tft_item_name(x), "img": self.gd.tft_item_icon(x)} for x in it.get("from") or []],
            "side": {"small": "最佳携带", "big": self.gd.tft_champion_name(top_cid), "sub": f"均名 {num(shown[0].get('avgPlacement'))}"}
            if top_cid else None,
        }
        colours = relative_cls([r.get("avgPlacement") for r in shown])
        for r, colour in zip(shown, colours):
            cid = r.get("characterId", "")
            rows.append([
                Cell(self.gd.tft_champion_name(cid), img=self.gd.tft_champion_icon(cid), cls=self._cost_cls(cid, "c")),
                Cell(num(r.get("avgPlacement")), cls=colour),
                pct(r.get("top4Rate")),
                pct(r.get("winRate")),
                big(r.get("totalCount")),
            ])
        doc.add_table("携带英雄排行", ["英雄", "均名", "前四", "登顶", "次数"], rows)
        if shown:
            doc.tip(f"/tft 英雄 {self.gd.tft_champion_name(shown[0].get('characterId', ''))}", "排名第一的英雄出装")
        doc.tip("/tft 阵容", "版本强势阵容")
        doc.footer = freshness(created)
        return View("tft_item", {"item": api, "champions": rows_raw}, doc, created)

    async def augment(self, query: str, limit: int = 10) -> View:
        if not query.strip():
            raise UsageError("请提供强化符文关键词，例如：/tft 强化 贷款")
        raw, created = await self.svc._cached("tft:augments", self.svc.cfg.ttl_meta, "tft_list_augments", {"lang": self.svc.cfg.lang})
        headers = raw.get("headers") or []
        rows = [dict(zip(headers, r)) for r in raw.get("rows") or []]
        q = query.strip().lower()
        hits = [r for r in rows if q in str(r.get("name", "")).lower() or q in str(r.get("apiName", "")).lower()]
        if not hits:
            hits = [r for r in rows if q in strip_html(r.get("desc")).lower()]
        if not hits:
            raise NotFoundError(f"没有找到包含「{query}」的强化符文")
        try:
            await self._ready()  # the season banner comes with the TFT static data
        except Exception:
            pass
        doc = Doc(title=f"强化符文 · {query}", subtitle=f"共找到 {len(hits)} 个", theme="tft")
        if len(hits) == 1:
            doc.icon = hits[0].get("imageUrl")
            doc.hero = {"name": hits[0].get("name") or query, "sub": f"强化符文 · {AUGMENT_TIERS.get(_aug_tier(hits[0]), '')}".strip(" ·"),
                        "art": self.gd.tft_banner()[0]}
        else:
            doc.hero = {"layout": "center", "eyebrow": "AUGMENTS", "title": f"强化符文 · {query}",
                        "range": f"找到 {len(hits)} 个" + (f"，显示前 {limit} 个" if len(hits) > limit else "")}
            self._season_banner(doc.hero)
        doc.add_table(
            "",
            ["强化符文", "阶级"],
            [
                [
                    Cell(h.get("name") or "?", img=h.get("imageUrl"), sub=strip_html(h.get("desc"))),
                    Cell(AUGMENT_TIERS.get(_aug_tier(h), h.get("tier") or "-"), cls=AUGMENT_PILL.get(_aug_tier(h), "")),
                ]
                for h in hits[:limit]
            ],
        )
        doc.tip("/tft 阵容", "版本强势阵容")
        doc.tip("/tft 阵容 1", "第 1 名阵容的装备、羁绊与运营")
        doc.tip("/tft 装备 无尽之刃", "这件装备最适合给谁")
        doc.footer = freshness(created)
        return View("tft_augment", {"query": query, "augments": hits[:limit]}, doc, created)

    async def overview_for_llm(self, limit: int = 5) -> list[dict[str, Any]]:
        decks, _, _ = await self._decks()
        out = []
        for d in decks[:limit]:
            st = dig(d, "stat.deck", {}) or {}
            out.append({
                "name": self._deck_name(d),
                "avg_placement": st.get("avgPlacement"),
                "top4_rate": st.get("top4Rate"),
                "units": [self.gd.tft_champion_name(u["key"]) for u in d.get("units") or [] if u.get("key")],
            })
        return out

