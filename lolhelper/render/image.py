"""Image output with a fallback chain.

1. AstrBot t2i service (Star.html_render) renders templates/card.html.
2. Local Playwright, if installed, renders the same HTML.
3. Caller falls back to AstrBot text_to_image(markdown) or plain text.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..log import default_logger
from ..doc import Doc
from .html import load_template, template_data

HtmlRenderFn = Callable[..., Awaitable[str]]
FetchFn = Callable[[str], Awaitable[tuple[bytes, str] | None]]

MAX_INLINE_BYTES = 400_000
# The AstrBot text-to-image service renders on a fixed 800px-wide page; narrower cards leave a dark strip on the right.
T2I_VIEWPORT_WIDTH = 800


class ImageRenderError(Exception):
    pass


class IconInliner:
    """Download icons once and embed them as data URIs.

    Remote t2i services often cannot reach CommunityDragon / Data Dragon in
    time, which leaves broken images. Inlining makes the HTML self-contained.
    """

    def __init__(self, fetch: FetchFn, max_items: int = 600, concurrency: int = 8):
        self.fetch = fetch
        self.cache: OrderedDict[str, str | None] = OrderedDict()
        self.max_items = max_items
        self.sem = asyncio.Semaphore(concurrency)

    async def _one(self, url: str) -> str | None:
        if url in self.cache:
            self.cache.move_to_end(url)
            return self.cache[url]
        async with self.sem:
            try:
                got = await self.fetch(url)
            except Exception:
                got = None
        uri = None
        if got and 0 < len(got[0]) <= MAX_INLINE_BYTES:
            uri = f"data:{got[1] or 'image/png'};base64,{base64.b64encode(got[0]).decode()}"
        self.cache[url] = uri
        while len(self.cache) > self.max_items:
            self.cache.popitem(last=False)
        return uri

    async def inline(self, doc_dict: dict[str, Any]) -> dict[str, Any]:
        """Replace every http(s) image reference in the doc (except links) with a data URI."""
        urls: set[str] = set()

        def collect(node: Any, key: str = "") -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    if k not in ("link",):
                        collect(v, k)
            elif isinstance(node, list):
                for v in node:
                    collect(v, key)
            elif isinstance(node, str) and node.startswith("http") and key not in ("link", "text", "sub", "title", "subtitle"):
                urls.add(node)

        collect(doc_dict)
        if not urls:
            return doc_dict
        ordered = list(urls)
        results = await asyncio.gather(*(self._one(u) for u in ordered))
        mapping = {u: r for u, r in zip(ordered, results) if r}

        def swap(node: Any, key: str = "") -> Any:
            if isinstance(node, dict):
                return {k: (v if k == "link" else swap(v, k)) for k, v in node.items()}
            if isinstance(node, list):
                return [swap(v, key) for v in node]
            if isinstance(node, str) and node in mapping and key not in ("link", "text", "sub", "title", "subtitle"):
                return mapping[node]
            return node

        return swap(doc_dict)


class ImageRenderer:
    def __init__(
        self,
        out_dir: Path,
        html_render: HtmlRenderFn | None = None,
        strategy: str = "auto",
        theme: str = "dark",
        quality: int = 85,
        width: int = 760,
        max_rows: int = 15,
        logger: Any = None,
        fetch: FetchFn | None = None,
    ):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.html_render = html_render
        self.strategy = strategy
        self.theme = theme
        self.quality = max(10, min(100, int(quality)))
        self.width = width
        self.max_rows = max_rows
        self.logger = logger or default_logger
        self.inliner = IconInliner(fetch) if fetch else None
        try:
            import playwright  # noqa: F401

            self._playwright_ok = True
        except ImportError:
            self._playwright_ok = False

    async def _data(self, doc: Doc, min_width: int = 0) -> dict[str, Any]:
        data = template_data(doc, self.theme, self.max_rows, self.width)
        if min_width and data["width"] < min_width:
            data["width"] = min_width  # fill the render service's fixed viewport: no empty strip on the right
        if self.inliner:
            try:
                data["doc"] = await asyncio.wait_for(self.inliner.inline(data["doc"]), timeout=15)
            except asyncio.TimeoutError:
                self.logger.info("[lolhelper] 图标下载超时，部分图标将由渲染端加载")
        return data

    async def render(self, doc: Doc, umo: str | None = None, want_file: bool = False) -> str:
        """Return an image URL or local file path."""
        order = {
            "t2i": ["t2i"],
            "playwright": ["playwright"],
            "auto": ["playwright", "t2i"] if self._playwright_ok else ["t2i"],
        }.get(self.strategy, ["t2i"])
        errors: list[str] = []
        for name in order:
            try:
                if name == "t2i" and self.html_render is not None:
                    return await self._t2i(doc, umo, want_file)
                if name == "playwright":
                    return await self._playwright(doc)
            except Exception as exc:  # try next strategy
                if name == "playwright" and self.strategy == "auto":
                    self._playwright_ok = False  # do not retry a broken local browser every time
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
                self.logger.warning(f"[lolhelper] 图片渲染 {name} 失败：{exc}")
        raise ImageRenderError("; ".join(errors) or "没有可用的图片渲染方式")

    async def _t2i(self, doc: Doc, umo: str | None, want_file: bool) -> str:
        data = await self._data(doc, T2I_VIEWPORT_WIDTH)
        options = {"full_page": True, "type": "jpeg", "quality": self.quality}
        # ask for a local file so the empty area below short cards (the service page is >= 720px tall) can be cut off
        try:
            ref = await self.html_render(load_template(), data, return_url=False, options=options, umo=umo)
        except TypeError:  # AstrBot versions without the umo parameter
            ref = await self.html_render(load_template(), data, return_url=False, options=options)
        if ref and not str(ref).startswith("http"):
            trimmed = await asyncio.to_thread(trim_bottom, str(ref), self.out_dir, self.quality)
            return trimmed or ref
        return ref

    async def _playwright(self, doc: Doc) -> str:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise ImageRenderError("未安装 playwright") from exc
        import jinja2

        data = await self._data(doc)
        html = jinja2.Environment(autoescape=True).from_string(load_template()).render(**data)
        name = hashlib.md5(html.encode("utf-8")).hexdigest()[:16]
        path = self.out_dir / f"{name}.jpg"
        if path.exists():
            return str(path)
        async with async_playwright() as p:
            browser = await _launch(p)
            try:
                page = await browser.new_page(
                    viewport={"width": int(data["width"]), "height": 400}, device_scale_factor=2
                )
                await page.set_content(html, wait_until="networkidle", timeout=20000)
                await page.screenshot(path=str(path), full_page=True, type="jpeg", quality=self.quality)
            finally:
                await browser.close()
        self._cleanup()
        return str(path)

    def _cleanup(self, keep_seconds: int = 3 * 3600) -> None:
        now = time.time()
        for f in self.out_dir.glob("*.jpg"):
            try:
                if now - f.stat().st_mtime > keep_seconds:
                    f.unlink()
            except OSError:
                pass


async def _launch(p):
    """Use Playwright's Chromium, or fall back to an installed Edge / Chrome."""
    errors = []
    for channel in (None, "msedge", "chrome"):
        try:
            return await (p.chromium.launch(channel=channel) if channel else p.chromium.launch())
        except Exception as exc:
            errors.append(f"{channel or 'chromium'}: {str(exc).splitlines()[0]}")
    raise ImageRenderError("没有可用的浏览器（可运行 playwright install chromium）；" + " | ".join(errors))



def trim_bottom(path: str, out_dir: Path, quality: int = 85, pad: int = 18) -> str | None:
    """Cut the uniform background below the card's last line, keeping `pad` px. Returns the new file or None."""
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as im:
            rgb = im.convert("RGB")
        w, h = rgb.size
        bg = rgb.getpixel((w - 2, h - 2))
        px = rgb.load()
        step = max(1, w // 200)

        def blank(y: int) -> bool:
            return all(sum(abs(a - b) for a, b in zip(px[x, y], bg)) <= 24 for x in range(0, w, step))

        y = h - 1
        while y > 0 and blank(y):
            y -= 1
        cut = min(h, y + 1 + pad)
        if cut >= h - 4:
            return None  # nothing worth trimming
        out = Path(out_dir) / f"t2i_{hashlib.md5(path.encode()).hexdigest()[:12]}_{int(time.time())}.jpg"
        rgb.crop((0, 0, w, cut)).save(out, "JPEG", quality=quality)
        return str(out)
    except Exception:
        return None
