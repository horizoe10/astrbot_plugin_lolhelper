"""Colored team logos from the LoL Esports Global Power Rankings (lolesports.com/en-US/gpr).

The rankings page is server-rendered and carries its GraphQL results in the HTML: every ranked team of the
major leagues with its code, name, logo and home league. The logos are made for the site's dark interface, so
the image cards show them instead of OP.GG's team logos. Teams that are not ranked (smaller leagues) keep
OP.GG's logo. The list is stored on disk and refreshed weekly in the background.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

from .cache import JsonStore
from .errors import UpstreamError
from .log import default_logger
from .net import HttpClient

GPR_URL = "https://lolesports.com/en-US/gpr"  # redirects to the current year's rankings
_APOLLO = '(window[Symbol.for("ApolloSSRDataTransport")] ??= []).push('
_UNDEFINED_RE = re.compile(r"([:,\[]\s*)undefined(?=\s*[,}\]])")
REFRESH = 7 * 86400  # rankings and logos change slowly
RETRY = 3600  # after a failed refresh


def _https(url: str) -> str:
    return "https://" + url[len("http://"):] if url.startswith("http://") else url


def parse_gpr(html: str) -> dict[str, Any]:
    """Teams ({code, name, image, league}) and league logos ({name: image}) found in a rankings page."""
    teams: dict[str, dict] = {}
    leagues: dict[str, str] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            kind, img = node.get("__typename"), node.get("image")
            if kind == "Team" and isinstance(img, str) and img and (node.get("code") or node.get("name")):
                teams[str(node.get("id") or node.get("code"))] = {
                    "code": str(node.get("code") or ""), "name": str(node.get("name") or ""), "image": _https(img),
                    "league": str((node.get("homeLeague") or {}).get("name") or ""),
                }
            elif kind in ("League", "HomeLeague") and isinstance(img, str) and img and node.get("name"):
                leagues[str(node["name"])] = _https(img)
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    pos = 0
    while (i := html.find(_APOLLO, pos)) >= 0:
        pos = i + len(_APOLLO)
        end = html.find("</script>", pos)
        if end < 0:
            break
        body = html[pos:end].strip().rstrip(";").rstrip()
        if body.endswith(")"):
            body = body[:-1]
        try:
            walk(json.loads(_UNDEFINED_RE.sub(r"\1null", body)))
        except ValueError:
            continue
    return {"teams": list(teams.values()), "leagues": leagues}


def _norm(s: Any) -> str:
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


def _inside(a: str, b: str, min_len: int) -> bool:
    short, long_ = sorted((a, b), key=len)
    return len(short) >= min_len and short in long_


def match_team(teams: list[dict], acronym: str | None, name: str | None) -> dict | None:
    """The ranked team for an OP.GG team: the same code when the names agree (or one is unknown), else the
    same full name, where one name may extend the other ("Team Liquid" -> "Team Liquid Alienware")."""
    a, n = _norm(acronym), _norm(name)
    if a:
        for t in teams:
            tn = _norm(t.get("name"))
            if _norm(t.get("code")) == a and (not n or not tn or n == tn or _inside(n, tn, 4) or (a in n and a in tn)):
                return t
    if n:
        named = [(t, _norm(t.get("name"))) for t in teams]
        return next((t for t, tn in named if tn == n), None) or next((t for t, tn in named if tn and _inside(n, tn, 6)), None)
    return None


class LolEsportsLogos:
    def __init__(self, http: HttpClient, path: Path, logger: Any = None, url: str = GPR_URL):
        self.http = http
        self.store = JsonStore(path)
        self.url = url
        self.logger = logger or default_logger
        self.data: dict[str, Any] = {}
        self._task: asyncio.Future | None = None
        self._next = 0.0  # earliest time of the next refresh

    def refresh(self) -> asyncio.Future | None:
        """Load the stored list and refresh it in the background when it is missing or a week old."""
        if self._task is not None and not self._task.done():
            return self._task
        if time.time() < self._next:
            return None
        self._next = time.time() + RETRY
        self._task = asyncio.ensure_future(self._load())
        return self._task

    async def _load(self) -> None:
        if not self.data:
            self.data = await self.store.load({}) or {}
        fetched = float(self.data.get("fetched_at") or 0)
        if self.data.get("teams") and time.time() - fetched < REFRESH:
            self._next = fetched + REFRESH
            return
        try:
            got = parse_gpr(await self._page())
            if not got["teams"]:
                raise UpstreamError("页面里没有找到队伍数据")
            self.data = {**got, "fetched_at": time.time()}
            await self.store.save(self.data)
            self._next = time.time() + REFRESH
            self.logger.info(f"[lolhelper] 已更新 LoL Esports 队标：{len(got['teams'])} 支队伍")
        except Exception as exc:
            self.logger.info(f"[lolhelper] 获取 LoL Esports 队标失败，{'继续用上次的队标' if self.data.get('teams') else '暂用 OP.GG 队标'}：{exc}")

    async def _page(self) -> str:
        import aiohttp

        async with self.http.session.get(self.url, proxy=self.http.proxy, timeout=aiohttp.ClientTimeout(total=90)) as resp:
            if resp.status != 200:
                raise UpstreamError(f"HTTP {resp.status}")
            return await resp.text(errors="ignore")

    async def teams(self, wait: float = 6) -> list[dict]:
        """The ranked teams; waits up to wait seconds for a load in progress."""
        task = self.refresh()
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), wait)
            except Exception:
                pass
        return self.data.get("teams") or []
