"""HTML card renderer (Jinja2) used for image output and the HTML API format.

The template (templates/card.html) is rendered either locally or by AstrBot's
t2i service, which receives the template string plus data and has no loader or
custom filters. Everything the template needs is therefore computed here:
the colour palette, the embedded number font and per-card layout
hints (banner, section captions, name sizes).
"""

from __future__ import annotations

import base64
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..doc import Doc, limited

ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATE_PATH = ROOT / "templates" / "card.html"
FONT_DIR = ROOT / "assets" / "fonts"
MARK_PATH = ROOT / "assets" / "mark.png"  # the plugin's emblem, white on transparent: watermark of generic posters

# the single card theme (fog grey); the hero banner has its own darker backdrop in the template
BG, PANEL, TEXT, MUTED, LINE = "#2a2d32", "#353940", "#f2f3f5", "#a9aeb6", "#43474f"

# accent per card family: rift blue for LoL and the China server, logo cyan for group play and tools,
# TFT gold; esports cards use the league colour (Doc.accent) and fall back to violet
ACCENTS = {"lol": "#4c8dff", "cn": "#4c8dff", "tft": "#e0b252", "esports": "#8b5cf6", "group": "#2ec5d3", "tool": "#2ec5d3"}
TEAM_BLUE, TEAM_RED = "#3d8bff", "#ff4655"

FONT_FILES = {(600, "normal"): "600-normal", (700, "normal"): "700-normal", (800, "normal"): "800-normal",
              (700, "italic"): "700-italic", (800, "italic"): "800-italic"}

# English captions under the centered section titles
SECTION_EN = {
    "段位": "RANKED", "本赛季常用英雄": "SEASON CHAMPIONS", "常用英雄": "CHAMPIONS", "推荐出装": "BUILD",
    "后续装备": "LATE ITEMS", "符文": "RUNES", "技能": "SKILLS", "对线克制": "MATCHUPS", "海克斯强化": "AUGMENTS",
    "海克斯强化推荐": "AUGMENTS", "海克斯强化 · 棱彩": "PRISMATIC AUGMENTS", "海克斯强化 · 黄金": "GOLD AUGMENTS",
    "海克斯强化 · 白银": "SILVER AUGMENTS", "AI 点评": "AI REVIEW", "AI 裁判": "AI JUDGE", "AI 周报": "AI WEEKLY",
    "数据概览": "OVERVIEW", "胜场榜": "PODIUM", "分项榜": "LEADERBOARDS", "称号": "TITLES", "本周称号": "TITLES",
    "段位变动（单双排）": "RANK CHANGES", "可竞猜的比赛": "OPEN MATCHES", "猜中的群友": "WINNERS", "每局数据": "GAMES",
    "最终阵容": "FINAL BOARD", "羁绊": "TRAITS", "运营过渡": "GAME PLAN", "最常用三件套": "MOST USED ITEMS",
    "登顶率最高的出装": "BEST BUILDS", "携带英雄排行": "TOP CARRIERS", "WeGame 凭证": "WEGAME", "选手数据": "PLAYERS",
    "数据对比": "TEAM STATS", "局势走向": "MOMENTUM", "双方数据": "PLAYERS", "上单": "TOP", "打野": "JUNGLE", "中单": "MID", "下路": "BOTTOM",
    "辅助": "SUPPORT", "赛程": "SCHEDULE", "赛果": "RESULTS", "积分榜": "STANDINGS",
}
_EN_PREFIX = (("最近", "RECENT MATCHES"), ("海克斯强化", "AUGMENTS"))

_NUM_RE = re.compile(r"^[-+]?[\d.,:/%×#↑↓ ]+(?:万|K|LP|局|场|次|分)?$")
_EMOJI_RE = re.compile(r"^[^\w\s#(（\[【<]+\s*")
_VALUE_RE = re.compile(r"^([-+]?[\d.,]+%?)\s*(.*)$")


@lru_cache(maxsize=1)
def load_template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=2)
def font_faces(italic: bool = False) -> tuple[dict, ...]:
    """Barlow Condensed (numbers and Latin names) as data URIs; italics only for esports cards."""
    out = []
    for (weight, style), name in FONT_FILES.items():
        if style == "italic" and not italic:
            continue
        path = FONT_DIR / f"barlow-condensed-latin-{name}.woff2"
        if path.exists():
            out.append({"w": weight, "s": style, "b64": base64.b64encode(path.read_bytes()).decode()})
    return tuple(out)


@lru_cache(maxsize=1)
def mark_uri() -> str | None:
    return f"data:image/png;base64,{base64.b64encode(MARK_PATH.read_bytes()).decode()}" if MARK_PATH.exists() else None


# ------------------------------------------------------------------ colours
def _rgb(c: str) -> tuple[int, int, int]:
    h = c.lstrip("#")
    if len(h) == 3:
        h = "".join(x * 2 for x in h)
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def rgba(c: str, a: float) -> str:
    r, g, b = _rgb(c)
    return f"rgba({r},{g},{b},{a})"


def mix(a: str, b: str, t: float) -> str:
    """Blend colour a towards b by t (0..1), as #rrggbb."""
    x, y = _rgb(a), _rgb(b)
    return "#" + "".join(f"{round(p + (q - p) * t):02x}" for p, q in zip(x, y))


def palette(accent: str) -> dict[str, Any]:
    win, lose = "#3fd18a", "#f0606e"
    return {
        "bg": BG, "panel": PANEL, "line": LINE, "text": TEXT, "muted": MUTED,
        "soft": mix(TEXT, MUTED, .45), "dim": mix(MUTED, BG, .4), "panel2": mix(PANEL, BG, .55),
        "fill": "rgba(255,255,255,.07)", "fill2": "rgba(255,255,255,.13)", "slot": "rgba(0,0,0,.28)", "edge": "transparent",
        "kbd": "#15171b", "dt": "#f07c86", "scb": "#e6e9ee", "scf": "#16181c",
        "acc": accent, "acc_t": rgba(accent, .4), "acc_s": rgba(accent, .16), "acc_g": rgba(accent, .3),
        "blue": TEAM_BLUE, "red": TEAM_RED,
        "win": win, "lose": lose, "gold": "#ffcc4d", "wint": "#4fe0a0", "loset": "#ff8a95", "goldt": "#ffcc4d",
        "r1": "#f5c451", "r2": "#d6dde6", "r3": "#e2a476",
        "panel_win": mix(PANEL, win, .12), "panel_lose": mix(PANEL, lose, .12), "panel_me": mix(PANEL, accent, .16),
    }


# ------------------------------------------------------------------ layout hints
def text_em(s: str, latin: float = .48) -> float:
    """Approximate rendered width in em: CJK / full-width glyphs are 1em, condensed Latin about half."""
    w = 0.0
    for ch in str(s or ""):
        o = ord(ch)
        w += 1.0 if o >= 0x2E80 else (.26 if ch == " " else latin)
    return w


def has_cjk(s: str) -> bool:
    return any(ord(ch) >= 0x2E80 for ch in str(s or ""))


def strip_emoji(s: str) -> str:
    return _EMOJI_RE.sub("", str(s or "")).strip() or str(s or "")


def split_value(v: Any) -> tuple[str, str]:
    """"7 胜" -> ("7", "胜"); text without a leading number stays whole."""
    m = _VALUE_RE.match(str(v or "").strip())
    return (m.group(1), m.group(2)) if m else (str(v or ""), "")


def _en(title: str) -> str:
    if title in SECTION_EN:
        return SECTION_EN[title]
    for prefix, en in _EN_PREFIX:
        if title.startswith(prefix):
            return en
    return ""


def _fit(name: str, avail: float, big: int, small: int = 22) -> int:
    em = text_em(name, .5 if has_cjk(name) else .47)
    return int(max(small, min(big, avail / max(em, 1))))


def _hero(d: dict[str, Any], width: int) -> dict[str, Any]:
    hero = dict(d.get("hero") or {})
    hero.setdefault("layout", "profile")
    if hero["layout"] in ("profile", "center") and not (hero.get("art") or hero.get("wm") or any(hero.get("arts") or [])):
        hero["wm"] = "mark"  # no art (no games, no champion, no league): every banner still gets a poster
    if hero.get("wm") == "mark":  # generic poster: the plugin mark as watermark
        hero["wm"] = mark_uri()
    if hero["layout"] == "profile":
        if not hero.get("name"):
            hero["name"] = strip_emoji(d.get("title") or "")
            hero.setdefault("sub", d.get("subtitle") or "")
        if "icon" not in hero:
            hero["icon"], hero["logo"] = d.get("icon"), bool(d.get("icon_logo"))
        name, tag = hero["name"], hero.get("tag") or ""
        zh = has_cjk(name)
        side = hero.get("side") or {}
        avail = width - 56 - (112 if hero.get("icon") else 0) - (300 if side.get("kind") == "rank" else 170 if side else 0)
        avail -= text_em(tag, .5) * 24 + (12 if tag else 0)
        hero["zh"] = zh
        hero["tag_zh"] = has_cjk(tag)
        hero["fs"] = _fit(name, avail, 40 if zh else 46)
        if side.get("big"):
            side["zh"] = has_cjk(side["big"])
    elif hero["layout"] == "center":
        hero.setdefault("title", strip_emoji(d.get("title") or ""))
        hero.setdefault("range", d.get("subtitle") or "")
        hero["range_zh"] = has_cjk(hero["range"])
        hero["fs"] = _fit(hero["title"], width - 120, 38, 24)
    elif hero["layout"] == "duel":
        for k in ("l", "r"):
            p = hero.setdefault(k, {})
            p["zh"] = has_cjk(p.get("name"))
            p["fs"] = _fit(p.get("name") or "", (width - 56 - 190) / 2 - 104, 32 if p["zh"] else 38, 18)
    elif hero["layout"] == "versus":
        for k in ("l", "r"):  # team column less the logo at its smallest (96), the gap and the 1px letter spacing
            t = hero.get(k) or {}
            name = str(t.get("name") or "")
            avail = (width - 56 - 300) / 2 - 96 - 22 - len(name)
            t["fs"] = int(max(30, min(60, avail / max(text_em(name, .5), 1))))
    if hero["layout"] != "versus":
        badges = [b for b in d.get("badges") or [] if "hx" not in str(b.get("cls") or "").split()]
        hero["allchips"] = list(hero.get("chips") or []) + badges
    return hero


def _numeric(text: str) -> bool:
    t = str(text or "").strip()
    return bool(t) and t not in ("-", "#") and bool(_NUM_RE.match(t))


def prepare(d: dict[str, Any], width: int) -> dict[str, Any]:
    """Add presentation hints to a Doc dict (in place) and return it."""
    d["hero"] = _hero(d, width)
    hero = d["hero"]
    secs = d.get("sections") or []
    for i, s in enumerate(secs):
        s.setdefault("style", "")
        s["en"] = _en(s.get("title") or "")
        kind = s.get("kind")
        if kind == "cards" and hero.get("stats"):
            s["note"] = ""  # the record summary is already in the banner's stat strip
        if kind == "table" and s["style"] in ("top3", "cardrow"):
            _tops(s)
        if kind == "table" and s["style"] == "standings":
            for row in s.get("rows") or []:  # win-loss bar under the first record column
                m = re.match(r"(\d+)-(\d+)$", str(row[2].get("text") or "")) if len(row) > 2 else None
                if m and int(m.group(1)) + int(m.group(2)):
                    row[2]["pct"] = round(int(m.group(1)) * 100 / (int(m.group(1)) + int(m.group(2))))
        if kind == "table":
            for row in s.get("rows") or []:
                for cell in row:
                    if _numeric(cell.get("text")) and not cell.get("img") and not cell.get("imgs"):
                        cell["n"] = True
        elif kind == "tiles" and s["style"] == "flow":
            groups: list[dict] = []
            for t in s.get("tiles") or []:
                if t.get("sub") or not groups:
                    groups.append({"label": t.get("sub") or "", "core": t.get("sub") == "核心", "items": []})
                groups[-1]["items"].append(t)
            s["groups"] = groups
            nxt = secs[i + 1] if i + 1 < len(secs) else None
            if nxt and nxt.get("style") == "follow":
                s["follow"] = (nxt.get("block") or {}).get("rows") or []
                nxt["style"] = "hide"
        elif kind == "iconrows" and s["style"] == "matchup":
            for r in (s.get("block") or {}).get("rows") or []:
                for it in r.get("items") or []:
                    wr = it.get("wr")
                    if wr is not None:
                        it["w"] = round(min(50.0, abs(float(wr) - 50) * 4), 1)
                        it["pct"] = f"{float(wr):g}%"
        elif kind == "podium":
            rows = (s.get("block") or {}).get("rows") or []
            for r in rows:
                r["v"], r["u"] = split_value(r.get("value"))
            slots = [rows[1] if len(rows) > 1 else None, rows[0] if rows else None, rows[2] if len(rows) > 2 else None]
            s["block"]["slots"] = slots
            if hero.get("layout") == "center" and not hero.get("podium"):
                hero["podium"] = slots
                s["style"] = "hide"
        elif kind == "boards":
            for bd in (s.get("block") or {}).get("boards") or []:
                bd["icon"], bd["name"] = _emoji_split(bd.get("title") or "")
                for r in bd.get("rows") or []:
                    r["v"], r["u"] = split_value(r.get("value"))
        elif kind == "tiles" and s["style"] == "medals":
            for t in s.get("tiles") or []:
                t["emoji"], t["name"] = _emoji_split(t.get("text") or "")
        elif kind == "split":
            for t in (s.get("block") or {}).get("teams") or []:
                t["name"] = strip_emoji(t.get("label") or "")
    return d


def _emoji_split(s: str) -> tuple[str, str]:
    s = str(s or "").strip()
    m = _EMOJI_RE.match(s)
    return (m.group(0).strip(), s[m.end():].strip()) if m and m.end() < len(s) else ("", s)


def _tops(s: dict[str, Any]) -> None:
    """Lift the leading rows of a ranking table into cards: rank, the ranked thing (icon + name), an optional
    tier pill, then the other columns as labelled numbers. top3 keeps the rest as a table, cardrow takes every row."""
    heads, rows = s.get("headers") or [], s.get("rows") or []
    n = 3 if s["style"] == "top3" else len(rows)
    tops = []
    for row in rows[:n]:
        if len(row) < 2:
            continue
        pill = row[2] if len(row) > 2 and set(str(row[2].get("cls") or "").split()) & {"op", "tp"} else None
        first = 3 if pill else 2
        stats = [{"k": heads[i] if i < len(heads) else "", "v": c.get("text") or "-", "cls": c.get("cls") or ""}
                 for i, c in enumerate(row) if i >= first]
        tops.append({"rank": row[0], "ent": row[1], "pill": pill, "stats": stats[:2] if s["style"] == "cardrow" else stats[:3]})
    s["tops"] = tops
    s["rows"] = rows[n:]
    s["total"] = max(0, int(s.get("total") or 0) - n)


def template_data(doc: Doc, max_rows: int = 0, width: int = 760, min_width: int = 0) -> dict[str, Any]:
    accent = doc.accent or ACCENTS.get(doc.theme, ACCENTS["lol"])
    data = limited(doc, max_rows).to_dict()
    w = max(doc.width or width, min_width)
    prepare(data, w)
    italic = doc.theme == "esports" or (doc.hero or {}).get("layout") == "versus"
    return {"doc": data, "c": palette(accent), "width": w, "fonts": list(font_faces(italic))}


def render_html(doc: Doc, max_rows: int = 0, width: int = 760) -> str:
    """Render locally with Jinja2 (for Playwright and the HTTP API)."""
    import jinja2

    env = jinja2.Environment(autoescape=True)
    return env.from_string(load_template()).render(**template_data(doc, max_rows, width))

