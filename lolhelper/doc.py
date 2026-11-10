"""Presentation model shared by the text, Markdown and image renderers.

Every query builds one Doc. Renderers never talk to OP.GG; they only turn a Doc
into a chat message. Keeping the three outputs on one structure means a new
data view automatically supports all push formats.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Cell:
    text: str = ""
    img: str | None = None
    cls: str = ""  # space separated: win lose muted gold accent hi lo r1-r3 | pills: "tp t1".."tp t5", "op"
    sub: str = ""  # optional second line
    sub_cls: str = ""  # colour of the second line: up / down / kda3-5 / muted
    # small icon strip (e.g. items); entries are URLs or {"src": url, "cls": "core"}
    imgs: list = field(default_factory=list)

    @classmethod
    def of(cls, value: Any) -> "Cell":
        if isinstance(value, Cell):
            return value
        return cls(text="" if value is None else str(value))


@dataclass
class Section:
    title: str = ""
    kind: str = "kv"  # kv | table | list | tiles | text | cards | team
    kv: list[list[str]] = field(default_factory=list)
    headers: list[str] = field(default_factory=list)
    rows: list[list[Cell]] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    tiles: list[Cell] = field(default_factory=list)
    text: str = ""
    note: str = ""
    total: int = 0  # original number of rows before truncation
    cards: list[dict] = field(default_factory=list)  # match list cards (image only)
    team: dict = field(default_factory=dict)  # team panel of a match detail (image only)
    widths: list[str] = field(default_factory=list)  # fixed column widths (image), e.g. ["10%", "30%", ...]
    block: dict = field(default_factory=dict)  # structured data for runes / skills / iconrows (image only)
    # image-only presentation hint: hide (shown by the banner instead) | flow (build path) | follow | matchup | medals | ranks
    style: str = ""


@dataclass
class Doc:
    title: str
    subtitle: str = ""
    icon: str | None = None
    theme: str = "lol"  # lol | tft | cn | esports
    badges: list[Cell] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)
    footer: str = ""
    link: str = ""
    width: int = 0  # image width override
    accent: str = ""  # theme color override (e.g. per esports league)
    icon_logo: bool = False  # icon is a transparent logo: show whole, no cropping
    tips: list[dict] = field(default_factory=list)  # command hints shown at the bottom: {"cmd", "desc"}
    # image-only banner, see templates/card.html. layout "profile" (default): art, icon, logo, level, name, tag, sub,
    # chips [{text, cls, img}], side {kind: rank|big, ...}, stats [{v, u, k} | {parts} | {streak} | {icons}];
    # "center": eyebrow, title, range, chips, art, podium, plus wide + slogo (TFT season banner and logo), vivid (brighter
    # art), duo + arts [left, right] (two-sided split banner); "versus": esports scoreboard. Any layout but duel: wm, a logo
    # shown as a large white watermark on a generated poster ("mark" = the plugin emblem), pc (poster tint of center).
    # Profile and center banners without art get the plugin emblem poster. Empty = built from title / badges.
    # Badges whose cls contains "hx" are shown by the banner elsewhere and left out of its chip row.
    hero: dict = field(default_factory=dict)

    # builders -----------------------------------------------------------------
    def badge(self, text: str, cls: str = "", img: str | None = None) -> "Doc":
        if text:
            self.badges.append(Cell(text=text, cls=cls, img=img))
        return self

    def tip(self, cmd: str, desc: str = "") -> "Doc":
        if cmd and len(self.tips) < 3:
            self.tips.append({"cmd": cmd, "desc": desc})
        return self

    def add_kv(self, title: str, pairs: list[tuple[str, Any]], note: str = "") -> "Doc":
        kv = [[str(k), "" if v is None else str(v)] for k, v in pairs if v not in (None, "")]
        if kv:
            self.sections.append(Section(title=title, kind="kv", kv=kv, note=note))
        return self

    def add_table(
        self, title: str, headers: list[str], rows: list[list[Any]], note: str = "", widths: list[str] | None = None,
        style: str = "",
    ) -> "Doc":
        cells = [[Cell.of(v) for v in r] for r in rows]
        if cells:
            self.sections.append(
                Section(title=title, kind="table", headers=headers, rows=cells, note=note, total=len(cells), widths=list(widths or []),
                        style=style)
            )
        return self

    def add_list(self, title: str, items: list[str], note: str = "") -> "Doc":
        items = [i for i in items if i]
        if items:
            self.sections.append(Section(title=title, kind="list", items=items, note=note, total=len(items)))
        return self

    def add_tiles(self, title: str, tiles: list[Cell], note: str = "", style: str = "") -> "Doc":
        if tiles:
            self.sections.append(Section(title=title, kind="tiles", tiles=tiles, note=note, style=style))
        return self

    def add_text(self, title: str, text: str) -> "Doc":
        if text:
            self.sections.append(Section(title=title, kind="text", text=text))
        return self

    def add_cards(self, title: str, cards: list[dict], lines: list[str], note: str = "") -> "Doc":
        """Rich match cards for images; lines are used by the text / Markdown renderers."""
        if cards:
            self.sections.append(Section(title=title, kind="cards", cards=cards, items=lines, note=note, total=len(cards)))
        return self

    def add_team(self, team: dict, lines: list[str]) -> "Doc":
        self.sections.append(Section(title="", kind="team", team=team, items=lines))
        return self

    def add_block(self, title: str, kind: str, block: dict, lines: list[str], note: str = "", style: str = "") -> "Doc":
        """Image-only visual block; lines are what the text / Markdown renderers print."""
        if block and any(lines):
            self.sections.append(Section(title=title, kind=kind, block=block, items=[x for x in lines if x], note=note, style=style))
        return self

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def limited(doc: Doc, max_rows: int) -> Doc:
    """Return a copy with tables/lists truncated to max_rows (0 = unlimited)."""
    if max_rows <= 0:
        return doc
    out = Doc(**{**doc.__dict__, "sections": []})
    for s in doc.sections:
        s2 = Section(**s.__dict__)
        if s.kind == "table" and len(s.rows) > max_rows:
            s2.rows = s.rows[:max_rows]
            s2.total = len(s.rows)
        if s.kind == "list" and len(s.items) > max_rows:
            s2.items = s.items[:max_rows]
            s2.total = len(s.items)
        if s.kind == "cards" and len(s.cards) > max_rows:
            s2.cards = s.cards[:max_rows]
            s2.items = s.items[:max_rows]
            s2.total = len(s.cards)
        out.sections.append(s2)
    return out

