"""Image preparation for the HTML cards, run before rendering.

- Team logos: OP.GG's logo is replaced by the team's LoL Esports logo when the team is ranked there (see
  lolesports.py), else by OP.GG's dark-mode variant. Team logos and the LoL Esports region icons are cropped to
  their visible area and scaled down, so each logo fills its box and the HTML sent to the renderer stays small.
- Banner art (PNG season banners and set splashes, official posters) is flattened onto the banner colour
  and re-encoded as JPEG.

Cropping and re-encoding need Pillow and follow the "inline icons" setting; without them only the team logo
URLs are swapped. Results are cached on disk and in memory.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..log import default_logger

FetchFn = Callable[[str], Awaitable[tuple[bytes, str] | None]]
LogoMapFn = Callable[[], Awaitable[dict[str, str]]]

TEAM_LOGO_RE = re.compile(r"^https?://[^/]+/images/lol/teams/", re.I)  # OP.GG team logos
LOGO_RE = re.compile(r"^https?://(?:[^/]+/images/lol/teams/|static\.lolesports\.com/(?:teams|leagues)/)", re.I)
SKIP_KEYS = ("link", "text", "sub", "title", "subtitle")  # text fields, as in IconInliner
ART_KEYS = ("art", "arts")
BANNER_BG = (20, 22, 26)  # the banner background (#14161a) under transparent art
LOGO_BIG, LOGO_SMALL = 320, 128  # logos in the banner (team logos fill 160x120) / in rows, tables and bars
KEEP_JPEG = 300_000  # JPEG art up to this size is embedded as it is
VERSION = 2  # bump when the processing changes, so cached files are rebuilt
FAIL_TTL = 600  # seconds before a failed image is tried again


def has_pillow() -> bool:
    try:
        import PIL.Image  # noqa: F401

        return True
    except ImportError:
        return False


# ------------------------------------------------------------------ pure image functions
def trim_logo(raw: bytes, size: int = LOGO_BIG) -> bytes | None:
    """A logo cropped to its visible area and scaled down to at most size px, as PNG with its colours."""
    from PIL import Image

    im = Image.open(io.BytesIO(raw))
    im.load()
    im = im.convert("RGBA")
    box = im.getchannel("A").point(lambda v: 255 if v > 8 else 0).getbbox()
    if not box:
        return None
    im = im.crop(box)
    im.thumbnail((size, size), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def flatten_jpeg(raw: bytes, max_width: int = 1600, quality: int = 82) -> bytes:
    """Banner art as JPEG: transparent margins cropped, the rest laid on the banner colour, at most max_width wide."""
    from PIL import Image

    im = Image.open(io.BytesIO(raw))
    im.load()
    if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
        im = im.convert("RGBA")
        alpha = im.getchannel("A")
        box = alpha.point(lambda v: 255 if v > 8 else 0).getbbox()
        if box:
            im, alpha = im.crop(box), alpha.crop(box)
        base = Image.new("RGB", im.size, BANNER_BG)
        base.paste(im, mask=alpha)
        im = base
    else:
        im = im.convert("RGB")
    if im.width > max_width:
        im = im.resize((max_width, max(1, round(im.height * max_width / im.width))), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def _uri(data: bytes, ctype: str) -> str:
    return f"data:{ctype};base64,{base64.b64encode(data).decode()}"


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes() if path.exists() else None
    except OSError:
        return None


def _build(fn: Callable[..., bytes | None], raw: bytes, path: Path, *args: Any) -> bytes | None:
    """Run an image function in a worker thread and cache its result on disk."""
    data = fn(raw, *args)
    if data:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        except OSError:
            pass
    return data


def _swap(node: Any, pick: Callable[[str, str, bool], str | None], key: str = "", hero: bool = False) -> Any:
    """Copy of a doc dict with image strings replaced where pick(value, key, inside_banner) returns a value."""
    if isinstance(node, dict):
        return {k: (v if k == "link" else _swap(v, pick, k, hero or k == "hero")) for k, v in node.items()}
    if isinstance(node, list):
        return [_swap(v, pick, key, hero) for v in node]
    if isinstance(node, str) and key not in SKIP_KEYS:
        got = pick(node, key, hero)
        return node if got is None else got
    return node


# ------------------------------------------------------------------ processor
class ArtProcessor:
    def __init__(self, cache_dir: Path, fetch: FetchFn, team_logos: LogoMapFn | None = None,
                 inline: bool = True, logger: Any = None, keep_days: float = 30):
        self.dir = Path(cache_dir)
        self.fetch = fetch
        self.team_logos = team_logos  # OP.GG team logo URL -> the logo to show instead
        self.logger = logger or default_logger
        self.ok = has_pillow() and inline  # crop logos and compress art into data URIs
        self.sem = asyncio.Semaphore(6)
        self.logo_mem: OrderedDict[str, tuple[str | None, float]] = OrderedDict()
        self.art_mem: OrderedDict[str, tuple[str | None, float]] = OrderedDict()
        if self.ok:
            self._prune(keep_days)

    def _prune(self, keep_days: float) -> None:
        cutoff = time.time() - keep_days * 86400
        try:
            for f in self.dir.glob("*"):
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
        except OSError:
            pass

    @staticmethod
    def _recall(mem: OrderedDict, key: str) -> str | None:
        """Cached data URI; "" for a recent failure; None when unknown (or the failure is old)."""
        hit = mem.get(key)
        if hit is None:
            return None
        uri, ts = hit
        if uri is None and time.time() - ts > FAIL_TTL:
            mem.pop(key, None)
            return None
        mem.move_to_end(key)
        return uri or ""

    @staticmethod
    def _remember(mem: OrderedDict, key: str, uri: str | None, limit: int) -> None:
        mem[key] = (uri, time.time())
        mem.move_to_end(key)
        while len(mem) > limit:
            mem.popitem(last=False)

    async def _get(self, url: str) -> tuple[bytes, str] | None:
        try:
            got = await self.fetch(url)
        except Exception:
            return None
        return got if got and got[0] else None

    async def _logo(self, url: str, src: str, size: int) -> str | None:
        key = f"{size}:{url}|{src}"
        hit = self._recall(self.logo_mem, key)
        if hit is not None:
            return hit or None
        uri = None
        async with self.sem:
            for source in dict.fromkeys((src, url)):  # the preferred logo first, the original if it cannot be loaded
                path = self.dir / f"logo{VERSION}_{_md5(f'{size}:{source}')}.png"
                data = await asyncio.to_thread(_read, path)
                if data is None:
                    got = await self._get(source)
                    if not got:
                        continue
                    try:
                        data = await asyncio.to_thread(_build, trim_logo, got[0], path, size)
                    except Exception as exc:
                        self.logger.info(f"[lolhelper] 队标处理失败 {source}：{exc}")
                        continue
                if data:
                    uri = _uri(data, "image/png")
                    break
        self._remember(self.logo_mem, key, uri, 400)
        return uri

    async def _art(self, url: str) -> str | None:
        hit = self._recall(self.art_mem, url)
        if hit is not None:
            return hit or None
        uri = None
        async with self.sem:
            path = self.dir / f"art{VERSION}_{_md5(url)}.jpg"
            data = await asyncio.to_thread(_read, path)
            if data is not None:
                uri = _uri(data, "image/jpeg")
            else:
                got = await self._get(url)
                if got and got[1] == "image/jpeg" and len(got[0]) <= KEEP_JPEG:
                    uri = _uri(got[0], "image/jpeg")
                elif got:
                    try:
                        data = await asyncio.to_thread(_build, flatten_jpeg, got[0], path)
                        uri = _uri(data, "image/jpeg") if data else None
                    except Exception as exc:
                        self.logger.info(f"[lolhelper] 横幅图处理失败 {url}：{exc}")
        self._remember(self.art_mem, url, uri, 40)
        return uri

    async def prepare(self, doc: dict[str, Any]) -> dict[str, Any]:
        """Return the doc with team logos pointed at the logo to show and, when processing is on, logos cropped
        and banner art compressed into data URIs (banner logos at the large size). Anything that cannot be
        processed keeps its URL."""
        logos: dict[tuple[str, int], None] = {}
        arts: dict[str, None] = {}

        def collect(node: Any, key: str = "", hero: bool = False) -> None:
            if isinstance(node, dict):
                for k, v in node.items():
                    if k != "link":
                        collect(v, k, hero or k == "hero")
            elif isinstance(node, list):
                for v in node:
                    collect(v, key, hero)
            elif isinstance(node, str) and key not in SKIP_KEYS:
                if LOGO_RE.match(node):
                    logos[(node, LOGO_BIG if hero else LOGO_SMALL)] = None
                elif self.ok and key in ART_KEYS and node.startswith("http"):
                    arts[node] = None

        collect(doc)
        if not logos and not arts:
            return doc
        src: dict[str, str] = {}
        if self.team_logos and any(TEAM_LOGO_RE.match(u) for u, _ in logos):
            try:
                src = await asyncio.wait_for(self.team_logos(), 8) or {}
            except Exception:
                src = {}
        if not self.ok:
            return _swap(doc, lambda v, k, h: src.get(v)) if src else doc
        logo_keys, art_keys = list(logos), list(arts)
        results = await asyncio.gather(*[self._logo(u, src.get(u) or u, size) for u, size in logo_keys],
                                       *[self._art(u) for u in art_keys], return_exceptions=True)
        maps: dict[int, dict[str, str]] = {LOGO_BIG: {}, LOGO_SMALL: {}}
        for (u, size), r in zip(logo_keys, results):
            if isinstance(r, str):
                maps[size][u] = r
            elif src.get(u):
                maps[size][u] = src[u]  # not processed: still point at the preferred logo
        art_map = {u: r for u, r in zip(art_keys, results[len(logo_keys):]) if isinstance(r, str)}
        if not art_map and not maps[LOGO_BIG] and not maps[LOGO_SMALL]:
            return doc

        def pick(value: str, key: str, hero: bool) -> str | None:
            if key in ART_KEYS and value in art_map:
                return art_map[value]
            return maps[LOGO_BIG if hero else LOGO_SMALL].get(value)

        return _swap(doc, pick)

