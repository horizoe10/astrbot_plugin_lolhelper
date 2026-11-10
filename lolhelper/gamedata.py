"""Static game data: champion/item names and icons for LoL and TFT.

Sources (all cached on disk and refreshed periodically):
- Data Dragon (LoL champions, items, summoner spells, profile icons)
- CommunityDragon (TFT champion and trait names and set splashes for the current set)
- OP.GG MCP tft_list_item_combinations (TFT item names, OP.GG item keys)
- Tencent TFT site (season banner and Chinese season logo, store art of units without a set splash)
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from .log import default_logger
from .cache import JsonStore
from .errors import NotFoundError
from .net import HttpClient
from .nicknames import NICKNAMES

_NORM_RE = re.compile(r"[\s'’·.\-_]+")
SCHEMA = 6  # bump when the cached game data layout changes

RARITY = {"kSilver": "silver", "kGold": "gold", "kPrismatic": "prismatic"}

TFT_SITE = "https://lol.qq.com/tft/"
TFT_THEME = "https://game.gtimg.cn/images/lol/tft/themewebsite/"  # bg-top-s18.png, logo-tft-s18.png
TFT_CHESS = "https://game.gtimg.cn/images/lol/act/img/tft/js/chess.js"  # Tencent unit list: apiName -> store id
TFT_STORE = "https://game.gtimg.cn/images/lol/tftstore/s{set}/624x318/{id}.png"


def norm(s: str) -> str:
    return _NORM_RE.sub("", (s or "").strip().lower())


class GameData:
    def __init__(
        self,
        http: HttpClient,
        data_dir: Path,
        ddragon_base: str = "https://ddragon.leagueoflegends.com",
        cdragon_base: str = "https://raw.communitydragon.org",
        refresh_hours: float = 24,
        custom_aliases: dict[str, str] | None = None,
        logger: Any = None,
    ):
        self.http = http
        self.ddragon = ddragon_base.rstrip("/")
        self.cdragon = cdragon_base.rstrip("/")
        self.refresh_seconds = max(1.0, refresh_hours) * 3600
        self.custom_aliases = custom_aliases or {}
        self.logger = logger or default_logger
        self.lol_store = JsonStore(Path(data_dir) / "gamedata_lol.json")
        self.tft_store = JsonStore(Path(data_dir) / "gamedata_tft.json")
        self.lol: dict[str, Any] = {}
        self.tft: dict[str, Any] = {}
        self._lol_lock = asyncio.Lock()
        self._spells_cache: dict[str, list[dict]] = {}
        self._tft_lock = asyncio.Lock()
        self._champ_index: dict[str, str] = {}
        self._zh_name_index: dict[str, str] = {}
        self._tft_champ_index: dict[str, str] = {}
        self._tft_item_index: dict[str, str] = {}

    # ------------------------------------------------------------------ loading
    async def ensure_lol(self, force: bool = False) -> None:
        if self.lol and not force and not self._stale(self.lol):
            return
        async with self._lol_lock:
            if not self.lol:
                self.lol = await self.lol_store.load({}) or {}
                self._index_lol()
            if self.lol and not force and not self._stale(self.lol):
                return
            try:
                self.lol = await self._fetch_lol()
                self._index_lol()
                await self.lol_store.save(self.lol)
            except Exception as exc:  # keep stale data if refresh fails
                if not self.lol:
                    raise
                self.logger.warning(f"[lolhelper] 刷新 Data Dragon 失败，继续使用旧数据：{exc}")
                self.lol["fetched_at"] = time.time() - self.refresh_seconds + 1800

    async def ensure_tft(self, mcp, force: bool = False) -> None:
        if self.tft and not force and not self._stale(self.tft):
            return
        async with self._tft_lock:
            if not self.tft:
                self.tft = await self.tft_store.load({}) or {}
                self._index_tft()
            if self.tft and not force and not self._stale(self.tft):
                return
            try:
                self.tft = await self._fetch_tft(mcp)
                self._index_tft()
                await self.tft_store.save(self.tft)
            except Exception as exc:
                if not self.tft:
                    raise
                self.logger.warning(f"[lolhelper] 刷新云顶数据失败，继续使用旧数据：{exc}")
                self.tft["fetched_at"] = time.time() - self.refresh_seconds + 1800

    def _stale(self, data: dict) -> bool:
        if data.get("schema", 1) < SCHEMA:  # cache written by an older plugin version
            return True
        return time.time() - float(data.get("fetched_at", 0)) > self.refresh_seconds

    async def _fetch_lol(self) -> dict[str, Any]:
        versions = await self.http.get_json(f"{self.ddragon}/api/versions.json")
        version = versions[0]
        base = f"{self.ddragon}/cdn/{version}/data"
        zh, en, items, spells, runes = await asyncio.gather(
            self.http.get_json(f"{base}/zh_CN/champion.json"),
            self.http.get_json(f"{base}/en_US/champion.json"),
            self.http.get_json(f"{base}/zh_CN/item.json"),
            self.http.get_json(f"{base}/zh_CN/summoner.json"),
            self.http.get_json(f"{base}/zh_CN/runesReforged.json"),
        )
        champs: dict[str, dict] = {}
        for cid, c in zh["data"].items():
            en_c = en["data"].get(cid, {})
            champs[str(c["key"])] = {
                "id": int(c["key"]),
                "key": cid,
                # Data Dragon zh_CN puts the epithet in name and the hero name in title.
                "name_zh": c.get("name", cid),
                "title_zh": c.get("title", ""),
                "name_en": en_c.get("name", cid),
            }
        rune_icons: dict[str, str] = {}
        rune_names: dict[str, str] = {}
        for style in runes or []:
            rune_icons[str(style["id"])] = style.get("icon", "")
            rune_names[str(style["id"])] = style.get("name", "")
            for slot in style.get("slots") or []:
                for perk in slot.get("runes") or []:
                    rune_icons[str(perk["id"])] = perk.get("icon", "")
                    rune_names[str(perk["id"])] = perk.get("name", "")
        augments: dict[str, dict] = {}
        perk_icons: dict[str, str] = {}
        try:
            # CommunityDragon perks include stat shards (5001-5013), which Data Dragon lacks.
            perks = await self.http.get_json(f"{self.cdragon}/latest/plugins/rcp-be-lol-game-data/global/zh_cn/v1/perks.json")
            for p in perks or []:
                icon = p.get("iconPath") or ""
                if icon.startswith("/lol-game-data/assets/"):
                    perk_icons[str(p.get("id"))] = f"{self.cdragon}/latest/plugins/rcp-be-lol-game-data/global/default/" + icon[len("/lol-game-data/assets/"):].lower()
                if p.get("name") and str(p.get("id")) not in rune_names:
                    rune_names[str(p.get("id"))] = p["name"]
        except Exception as exc:
            self.logger.warning(f"[lolhelper] 属性碎片图标获取失败：{exc}")
        try:
            # Arena + ARAM Mayhem augments: names, rarity and icons (OP.GG only gives ids and stats)
            raw = await self.http.get_json(f"{self.cdragon}/latest/plugins/rcp-be-lol-game-data/global/zh_cn/v1/cherry-augments.json")
            for a in raw or []:
                icon = a.get("augmentSmallIconPath") or ""
                if icon.startswith("/lol-game-data/assets/"):
                    icon = f"{self.cdragon}/latest/plugins/rcp-be-lol-game-data/global/default/" + icon[len("/lol-game-data/assets/"):].lower()
                augments[str(a.get("id"))] = {
                    "name": a.get("nameTRA") or a.get("name") or "",
                    "rarity": RARITY.get(a.get("rarity"), ""),
                    "icon": icon or None,
                }
        except Exception as exc:
            self.logger.warning(f"[lolhelper] 海克斯强化数据获取失败，将只显示名称：{exc}")
        return {
            "fetched_at": time.time(),
            "version": version,
            "champions": champs,
            "items": {k: v.get("name", k) for k, v in items["data"].items()},
            "spells": {str(v["key"]): v.get("name", k) for k, v in spells["data"].items()},
            "spell_icons": {str(v["key"]): (v.get("image") or {}).get("full", "") for v in spells["data"].values()},
            "rune_icons": rune_icons,
            "rune_names": rune_names,
            "augments": augments,
            "perk_icons": perk_icons,
            "schema": SCHEMA,
        }

    async def _fetch_tft(self, mcp) -> dict[str, Any]:
        items_raw = await mcp.call("tft_list_item_combinations", {"lang": "zh_CN"})
        set_no = int(items_raw.get("set") or 0)
        items: dict[str, dict] = {}
        for it in items_raw.get("data", []):
            key = it.get("apiName") or it.get("_key")
            if not key:
                continue
            items[key] = {
                "name": it.get("name") or key,
                "icon": it.get("imageUrl") or it.get("icon"),
                "from": it.get("composition") or [],
                "category": it.get("category"),
            }
        champions: dict[str, dict] = {}
        traits: dict[str, str] = {}
        trait_icons: dict[str, str] = {}
        try:
            cd = await self.http.get_json(f"{self.cdragon}/latest/cdragon/tft/zh_cn.json")
            sd = _pick_set(cd.get("setData", []), set_no)
            for c in (sd or {}).get("champions", []):
                api = c.get("apiName")
                if not api:
                    continue
                art = str(c.get("icon") or "")
                champions[api] = {
                    "name": c.get("name") or api,
                    "cost": c.get("cost"),
                    "traits": c.get("traits") or [],
                    "icon": self._cdragon_asset(c.get("tileIcon") or c.get("squareIcon")),
                    # set splash (1024x512); set-only units such as monsters only have an unusable hashed path
                    "splash": self._cdragon_asset(art) if "splash_centered" in art.lower() else None,
                }
            for t in (sd or {}).get("traits", []):
                if t.get("apiName"):
                    traits[t["apiName"]] = t.get("name") or t["apiName"]
                    if t.get("icon"):
                        trait_icons[t["apiName"]] = self._cdragon_asset(t["icon"])
        except Exception as exc:
            self.logger.warning(f"[lolhelper] CommunityDragon 云顶数据获取失败，将用英文 ID 显示：{exc}")
        banner, store = await asyncio.gather(
            self._tft_banner(set_no), self._tft_store_art(set_no, [a for a, c in champions.items() if not c.get("splash")]))
        for api, url in store.items():
            champions[api]["store_art"] = url
        return {
            "fetched_at": time.time(),
            "set": set_no,
            "version": items_raw.get("version"),
            "items": items,
            "champions": champions,
            "traits": traits,
            "trait_icons": trait_icons,
            "banner": banner,
            "schema": SCHEMA,
        }

    async def _image_ok(self, url: str) -> bool:
        import aiohttp

        try:
            async with self.http.session.head(url, proxy=self.http.proxy, allow_redirects=True,
                                              timeout=aiohttp.ClientTimeout(total=8)) as resp:
                return resp.status == 200 and resp.headers.get("Content-Type", "").startswith("image/")
        except Exception:
            return False

    async def _get_text(self, url: str) -> str:
        import aiohttp

        async with self.http.session.get(url, proxy=self.http.proxy, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            return await resp.text(errors="ignore") if resp.status == 200 else ""

    async def _tft_banner(self, set_no: int) -> dict[str, str]:
        """Season banner and Chinese season logo of the Tencent TFT site ({"bg", "logo"}): the usual file names
        for this set first, else the newest season referenced by the site's stylesheet."""
        out: dict[str, str] = {}
        if set_no:
            bg, logo = f"{TFT_THEME}bg-top-s{set_no}.png", f"{TFT_THEME}logo-tft-s{set_no}.png"
            ok = await asyncio.gather(self._image_ok(bg), self._image_ok(logo))
            out = {k: v for (k, v), good in zip((("bg", bg), ("logo", logo)), ok) if good}
        if "bg" in out:
            return out
        try:
            page = await self._get_text(TFT_SITE)
            m = re.search(r"""["']([^"']*themewebsite[^"']*\.css)""", page)
            css = await self._get_text(urljoin(TFT_SITE, m.group(1))) if m else ""
        except Exception as exc:
            self.logger.info(f"[lolhelper] 云顶官网赛季横幅获取失败：{exc}")
            return out

        def newest(kind: str) -> tuple[str | None, int]:
            hits = re.findall(r"((?:https?:)?//[\w./-]+/%s-s(\d+)\.png)" % kind, css)
            if not hits:
                return None, 0
            url, n = max(hits, key=lambda h: int(h[1]))
            return ("https:" + url if url.startswith("//") else url), int(n)

        bg, n = newest("bg-top")
        logo, n_logo = newest("logo-tft")
        if bg:
            out = {"bg": bg, **({"logo": logo} if logo and n_logo == n else {})}
        return out

    async def _tft_store_art(self, set_no: int, apis: list[str]) -> dict[str, str]:
        """Tencent store art (624x318) of units that have no set splash on CommunityDragon."""
        if not set_no or not apis:
            return {}
        try:
            data = await self.http.get_json(TFT_CHESS, retries=1)
        except Exception as exc:
            self.logger.info(f"[lolhelper] 腾讯云顶棋子数据获取失败：{exc}")
            return {}
        ids = {str(x.get("hero_EN_name")): str(x.get("name") or "").split(".")[0]
               for x in (data or {}).get("data") or [] if isinstance(x, dict)}
        cand = {api: TFT_STORE.format(set=set_no, id=ids[api]) for api in apis if ids.get(api, "").isdigit()}
        ok = await asyncio.gather(*(self._image_ok(u) for u in cand.values()))
        return {api: url for (api, url), good in zip(cand.items(), ok) if good}

    def _cdragon_asset(self, path: str | None) -> str | None:
        if not path:
            return None
        p = path.lower().replace(".tex", ".png").replace(".dds", ".png")
        return f"{self.cdragon}/latest/game/{p}"

    # ------------------------------------------------------------------ indexes
    def _index_lol(self) -> None:
        idx: dict[str, str] = {}
        zh_idx: dict[str, str] = {}
        for cid, c in (self.lol.get("champions") or {}).items():
            for alias in (c["key"], c["name_en"], c["name_zh"], c["title_zh"], str(c["id"])):
                if alias:
                    idx[norm(alias)] = cid
            zh_idx[c["name_zh"]] = cid
            zh_idx[c["title_zh"]] = cid
            zh_idx[c["name_en"]] = cid
            zh_idx[c["key"]] = cid
        by_key = {c["key"].lower(): cid for cid, c in (self.lol.get("champions") or {}).items()}
        for nick, key in {**NICKNAMES, **self.custom_aliases}.items():
            cid = by_key.get(str(key).lower()) or idx.get(norm(str(key)))
            if cid:
                idx[norm(nick)] = cid
        self._champ_index = idx
        self._zh_name_index = zh_idx

    def _index_tft(self) -> None:
        cidx: dict[str, str] = {}
        for api, c in (self.tft.get("champions") or {}).items():
            cidx[norm(c.get("name", ""))] = api
            cidx[norm(api)] = api
            cidx[norm(_strip_tft_prefix(api))] = api
        iidx: dict[str, str] = {}
        for api, it in (self.tft.get("items") or {}).items():
            iidx[norm(it.get("name", ""))] = api
            iidx[norm(api)] = api
            iidx[norm(_strip_tft_prefix(api))] = api
        self._tft_champ_index = cidx
        self._tft_item_index = iidx

    # ------------------------------------------------------------------ lookups
    @property
    def version(self) -> str:
        return self.lol.get("version", "")

    def champion(self, champ_id: int | str) -> dict | None:
        return (self.lol.get("champions") or {}).get(str(champ_id))

    def champion_by_name(self, name: str) -> dict | None:
        """Find by OP.GG zh_CN name (e.g. 疾风剑豪) or any alias."""
        if not name:
            return None
        cid = self._zh_name_index.get(name) or self._champ_index.get(norm(name))
        return self.champion(cid) if cid else None

    def resolve_champion(self, query: str) -> dict:
        q = norm(query)
        if not q:
            raise NotFoundError("请提供英雄名")
        cid = self._champ_index.get(q)
        if cid:
            return self.champion(cid)
        hits = sorted({v for k, v in self._champ_index.items() if q in k})
        if len(hits) == 1:
            return self.champion(hits[0])
        if hits:
            names = "、".join(self.display_name(self.champion(h)) for h in hits[:6])
            raise NotFoundError(f"英雄「{query}」不唯一，可能是：{names}")
        raise NotFoundError(f"找不到英雄「{query}」")

    @staticmethod
    def display_name(c: dict | None) -> str:
        if not c:
            return "?"
        if c.get("title_zh"):
            return f"{c['name_zh']} {c['title_zh']}"
        return c.get("name_zh") or c.get("key", "?")

    def obj_icon(self, kind: str, red: bool = False) -> str | None:
        """Match-history objective icon (tower / dragon / baron / herald / gold)."""
        if kind == "gold":
            return f"{self.cdragon}/latest/plugins/rcp-fe-lol-match-history/global/default/icon_gold.png"
        if kind not in ("tower", "dragon", "baron", "herald", "inhibitor"):
            return None
        return f"{self.cdragon}/latest/plugins/rcp-fe-lol-match-history/global/default/{kind}-{200 if red else 100}.png"

    ROLE_ICON_KEYS = {"top": "top", "jungle": "jungle", "mid": "middle", "adc": "bottom", "support": "utility"}

    def role_icon(self, position: str | None) -> str | None:
        """Position icon of the client's lane selector (top / jungle / mid / adc / support)."""
        key = self.ROLE_ICON_KEYS.get(str(position or ""))
        if not key:
            return None
        return (f"{self.cdragon}/latest/plugins/rcp-fe-lol-clash/global/default/assets/images/position-selector/"
                f"positions/icon-position-{key}.png")

    def splash(self, c: dict | None) -> str | None:
        """Default skin splash art (about 150 KB), used as the card banner background."""
        if not c or not c.get("key"):
            return None
        return f"{self.ddragon}/cdn/img/champion/splash/{c['key']}_0.jpg"

    def champ_icon(self, c: dict | None) -> str | None:
        if not c or not self.version:
            return None
        return f"{self.ddragon}/cdn/{self.version}/img/champion/{c['key']}.png"

    def champ_icon_by_name(self, name: str) -> str | None:
        return self.champ_icon(self.champion_by_name(name))

    def item_name(self, item_id: int | str) -> str:
        return (self.lol.get("items") or {}).get(str(item_id), str(item_id))

    def item_icon(self, item_id: int | str) -> str | None:
        if not item_id or str(item_id) == "0" or not self.version:
            return None
        return f"{self.ddragon}/cdn/{self.version}/img/item/{item_id}.png"

    def spell_name(self, spell_id: int | str) -> str:
        return (self.lol.get("spells") or {}).get(str(spell_id), str(spell_id))

    def spell_icon(self, spell_id: int | str) -> str | None:
        img = (self.lol.get("spell_icons") or {}).get(str(spell_id))
        if not img or not self.version:
            return None
        return f"{self.ddragon}/cdn/{self.version}/img/spell/{img}"

    def rune_icon(self, rune_id: int | str | None) -> str | None:
        if not rune_id:
            return None
        path = (self.lol.get("rune_icons") or {}).get(str(rune_id))
        if path:
            return f"{self.ddragon}/cdn/img/{path}"
        return (self.lol.get("perk_icons") or {}).get(str(rune_id))

    async def champion_spells(self, champ: dict) -> list[dict]:
        """Passive + Q/W/E/R names and icons for one champion (Data Dragon, cached in memory)."""
        key = champ["key"]
        if key in self._spells_cache:
            return self._spells_cache[key]
        out: list[dict] = []
        try:
            data = await self.http.get_json(f"{self.ddragon}/cdn/{self.version}/data/zh_CN/champion/{key}.json")
            c = (data.get("data") or {}).get(key) or {}
            for slot, s in zip("QWER", c.get("spells") or []):
                out.append({"key": slot, "name": s.get("name", slot),
                            "icon": f"{self.ddragon}/cdn/{self.version}/img/spell/{(s.get('image') or {}).get('full', '')}"})
        except Exception as exc:
            self.logger.info(f"[lolhelper] 获取 {key} 技能图标失败：{exc}")
        self._spells_cache[key] = out
        return out

    def rune_name(self, rune_id: int | str | None) -> str:
        return (self.lol.get("rune_names") or {}).get(str(rune_id), "") if rune_id else ""

    def augment(self, augment_id: int | str) -> dict:
        return (self.lol.get("augments") or {}).get(str(augment_id)) or {}

    def profile_icon(self, icon_id: int | str | None) -> str | None:
        if icon_id in (None, "") or not self.version:
            return None
        return f"{self.ddragon}/cdn/{self.version}/img/profileicon/{icon_id}.png"

    # TFT
    def tft_champion_name(self, api: str) -> str:
        c = (self.tft.get("champions") or {}).get(api)
        return c["name"] if c else _strip_tft_prefix(api)

    def tft_champion_icon(self, api: str) -> str | None:
        c = (self.tft.get("champions") or {}).get(api)
        return c.get("icon") if c else None

    def tft_champion_cost(self, api: str) -> int | None:
        c = (self.tft.get("champions") or {}).get(api)
        return c.get("cost") if c else None

    def tft_splash(self, api: str | None) -> str | None:
        """Splash art of the League champion behind a TFT unit (banner background); None for set-only units."""
        if not api:
            return None
        c = self.champion_by_name(_strip_tft_prefix(api)) or self.champion_by_name(self.tft_champion_name(api))
        return self.splash(c)

    def tft_art(self, api: str | None) -> str | None:
        """Current-set art of a TFT unit: the set splash, else the Tencent store art; None when neither exists."""
        c = (self.tft.get("champions") or {}).get(api or "") or {}
        return c.get("splash") or c.get("store_art")

    def tft_banner(self) -> tuple[str | None, str | None]:
        """(season banner, Chinese season logo) of the current set, from the Tencent TFT site."""
        b = self.tft.get("banner") or {}
        return b.get("bg"), b.get("logo")

    def tft_localize(self, text: str) -> str:
        """Replace English unit names left in OP.GG's Chinese comp names ("峡谷野怪 Brambleback") with the
        Chinese names; a name that is already in the text is just dropped."""
        def unit(word: str) -> str | None:
            api = self._tft_champ_index.get(norm(word))
            return self.tft_champion_name(api) if api else None

        def sub(m: re.Match) -> str:
            run = m.group(0)
            zh = unit(run)
            if zh is None:
                parts = [(w, unit(w)) for w in run.split()]
                if not any(z for _, z in parts):
                    return run
                return " ".join((z if z not in text else "") if z else w for w, z in parts)
            return "" if zh in text else zh

        out = re.sub(r"[A-Za-z][A-Za-z'’.]*(?:[ -][A-Za-z][A-Za-z'’.]*)*", sub, str(text or ""))
        return re.sub(r"\s{2,}", " ", out).strip() or str(text or "")

    def tft_item_name(self, api: str) -> str:
        it = (self.tft.get("items") or {}).get(api)
        return it["name"] if it else _strip_tft_prefix(api)

    def tft_item_icon(self, api: str) -> str | None:
        it = (self.tft.get("items") or {}).get(api)
        return it.get("icon") if it else None

    def tft_trait_name(self, api: str) -> str:
        return (self.tft.get("traits") or {}).get(api) or _strip_tft_prefix(api)

    def tft_trait_icon(self, api: str) -> str | None:
        return (self.tft.get("trait_icons") or {}).get(api)

    def resolve_tft_champion(self, query: str, allowed: list[str] | None = None) -> str:
        return self._resolve_tft(query, self._tft_champ_index, allowed, "云顶英雄", self.tft_champion_name)

    def resolve_tft_item(self, query: str, allowed: list[str] | None = None) -> str:
        return self._resolve_tft(query, self._tft_item_index, allowed, "云顶装备", self.tft_item_name)

    @staticmethod
    def _resolve_tft(query, index, allowed, label, namer) -> str:
        q = norm(query)
        if not q:
            raise NotFoundError(f"请提供{label}名称")
        allowed_set = set(allowed or [])

        def ok(api: str) -> bool:
            return not allowed_set or api in allowed_set

        api = index.get(q)
        if api and ok(api):
            return api
        hits = sorted({v for k, v in index.items() if q in k and ok(v)})
        if not hits and allowed_set:
            hits = sorted(a for a in allowed_set if q in norm(_strip_tft_prefix(a)))
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            # "大天使" -> prefer 大天使之杖 over 光明版大天使之杖
            ranked = sorted(hits, key=lambda h: (not norm(namer(h)).startswith(q), len(namer(h))))
            a, b = ranked[0], ranked[1]
            if (norm(namer(a)).startswith(q), len(namer(a))) != (norm(namer(b)).startswith(q), len(namer(b))):
                return a
        if hits:
            raise NotFoundError(f"{label}「{query}」不唯一，可能是：" + "、".join(namer(h) for h in hits[:8]))
        raise NotFoundError(f"找不到{label}「{query}」")


def _pick_set(set_data: list[dict], set_no: int) -> dict | None:
    if not set_data:
        return None
    want = f"TFTSet{set_no}" if set_no else None
    if want:
        for sd in set_data:
            if sd.get("mutator") == want:
                return sd
        for sd in set_data:
            if str(sd.get("number")) == str(set_no) and sd.get("champions"):
                return sd
    with_champs = [sd for sd in set_data if sd.get("champions")]
    return max(with_champs, key=lambda s: int(s.get("number") or 0)) if with_champs else None


_TFT_PREFIX_RE = re.compile(r"^(?:DA_|TFT\d*_)?(?:\d+_)?")


def _strip_tft_prefix(api: str) -> str:
    s = _TFT_PREFIX_RE.sub("", api or "")
    return re.sub(r"\d+$", "", s) or api

