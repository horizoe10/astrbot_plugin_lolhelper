"""Markdown renderer for platforms that display Markdown (Telegram, Discord...)."""

from __future__ import annotations

from ..doc import Cell, Doc, limited
from .text import IMAGE_ONLY


def _esc(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", " ")


def _cell(c: Cell) -> str:
    text = _esc(c.text)
    if set(c.cls.split()) & {"win", "t1", "accent", "op", "hi", "r1"} and text:
        text = f"**{text}**"
    if c.sub:
        text += f" ({_esc(c.sub)})"
    return text


def render_markdown(doc: Doc, max_rows: int = 10, show_footer: bool = True) -> str:
    doc = limited(doc, max_rows)
    out: list[str] = [f"### {doc.title}"]
    if doc.subtitle:
        out.append(f"> {doc.subtitle}")
    if doc.badges:
        out.append("")
        out.append(" · ".join(f"**{b.text}**" for b in doc.badges))
    for s in doc.sections:
        if s.kind in IMAGE_ONLY:
            continue
        out.append("")
        if s.title:
            out.append(f"**{s.title}**")
            out.append("")
        if s.kind == "kv":
            out.extend(f"- {k}：{v}" for k, v in s.kv)
        elif s.kind == "table":
            width = max([len(s.headers)] + [len(r) for r in s.rows])
            headers = (s.headers + [""] * width)[:width]
            out.append("| " + " | ".join(_esc(h) for h in headers) + " |")
            out.append("|" + "---|" * width)
            for row in s.rows:
                cells = ([_cell(c) for c in row] + [""] * width)[:width]
                out.append("| " + " | ".join(cells) + " |")
            if s.total > len(s.rows):
                out.append("")
                out.append(f"_共 {s.total} 条，仅显示前 {len(s.rows)} 条_")
        elif s.kind == "list":
            out.extend(f"- {i}" for i in s.items)
        elif s.kind in ("cards", "runes", "skills", "iconrows", "versus", "series", "ai", "form", "podium", "boards", "pk", "split"):
            out.extend(f"- {i}" for i in s.items)
        elif s.kind == "team" and s.items:
            out.append(f"**{s.items[0]}**")
            out.append("")
            out.extend(f"- {i.strip().replace('▶', '**▶**')}" for i in s.items[1:])
        elif s.kind == "tiles":
            out.append(" · ".join(_cell(t) for t in s.tiles))
        elif s.kind == "text":
            out.append(s.text)
        if s.note:
            out.append("")
            out.append(f"_{s.note}_")
    if doc.tips:
        out.append("")
        out.append("> 💡 **试试** " + " · ".join(f"\u0060{t['cmd']}\u0060 {t['desc']}".rstrip() for t in doc.tips))
    if doc.link:
        out.append("")
        out.append(f"[在 OP.GG 查看]({doc.link})")
    if show_footer and doc.footer:
        out.append("")
        out.append(f"_{doc.footer}_")
    return "\n".join(out).strip()

