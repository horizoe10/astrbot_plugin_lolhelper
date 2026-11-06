"""HTML card renderer (Jinja2) used for image output and the HTML API format."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from ..doc import Doc, limited

TEMPLATE_PATH = Path(__file__).resolve().parent.parent.parent / "templates" / "card.html"

THEMES: dict[str, dict[str, str]] = {
    "dark": {
        "bg": "#0c0c0e", "card": "#161618", "card2": "#202024", "text": "#ececef",
        "muted": "#8f8f98", "line": "#2c2c31",
    },
    "light": {
        "bg": "#eef1f6", "card": "#ffffff", "card2": "#f5f7fb", "text": "#1f2633",
        "muted": "#6b7587", "line": "#e1e6ef",
    },
}

ACCENTS = {"lol": "#5383e8", "tft": "#d9a441", "cn": "#d0353f", "esports": "#8b5cf6"}


@lru_cache(maxsize=1)
def load_template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def template_data(doc: Doc, theme: str = "dark", max_rows: int = 0, width: int = 760) -> dict[str, Any]:
    palette = dict(THEMES.get(theme, THEMES["dark"]))
    palette["accent"] = ACCENTS.get(doc.theme, ACCENTS["lol"])
    return {"doc": limited(doc, max_rows).to_dict(), "c": palette, "width": doc.width or width}


def render_html(doc: Doc, theme: str = "dark", max_rows: int = 0, width: int = 760) -> str:
    """Render locally with Jinja2 (for Playwright and the HTTP API)."""
    import jinja2

    env = jinja2.Environment(autoescape=True)
    return env.from_string(load_template()).render(**template_data(doc, theme, max_rows, width))

