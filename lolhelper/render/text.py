"""Plain text renderer. Works on every platform, so it is the final fallback."""

from __future__ import annotations

from ..doc import Cell, Doc, limited


def _cell(c: Cell) -> str:
    return f"{c.text}({c.sub})" if c.sub else c.text


def render_text(doc: Doc, max_rows: int = 10, show_footer: bool = True) -> str:
    doc = limited(doc, max_rows)
    lines: list[str] = [f"【{doc.title}】" + (f" {doc.subtitle}" if doc.subtitle else "")]
    if doc.badges:
        lines.append(" | ".join(b.text for b in doc.badges))
    for s in doc.sections:
        lines.append("")
        if s.title:
            lines.append(f"▌{s.title}")
        if s.kind == "kv":
            lines.extend(f"{k}：{v}" for k, v in s.kv)
        elif s.kind == "table":
            if s.headers and any(s.headers):
                lines.append(" / ".join(h for h in s.headers if h))
            for row in s.rows:
                lines.append("  ".join(_cell(c) for c in row if c.text or c.sub))
            if s.total > len(s.rows):
                lines.append(f"…共 {s.total} 条，仅显示前 {len(s.rows)} 条")
        elif s.kind == "list":
            lines.extend(f"• {i}" for i in s.items)
            if s.total > len(s.items):
                lines.append(f"…共 {s.total} 条")
        elif s.kind in ("cards", "team"):
            lines.extend(s.items)
        elif s.kind == "tiles":
            lines.append(" / ".join(_cell(t) for t in s.tiles))
        elif s.kind == "text":
            lines.append(s.text)
        if s.note:
            lines.append(f"（{s.note}）")
    if show_footer and doc.footer:
        lines.append("")
        lines.append(doc.footer)
    return "\n".join(lines).strip()

