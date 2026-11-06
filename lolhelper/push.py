"""Background push: new matches of bound accounts, esports reminders/results, daily digests."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Awaitable, Callable

from .doc import Doc
from .errors import LolHelperError
from .fmt import get_tz, local_time, parse_time
from .constants import stage_zh
from .service import OpggService, View, league_theme, versus_row
from .storage import account_key, account_label, sub_league_list

SendFn = Callable[[str, View], Awaitable[None]]

SUB_TYPES = {
    "match": "新对局",
    "esports": "赛事提醒",
    "lol_daily": "LOL 梯队日报",
    "tft_daily": "云顶阵容日报",
}


class PushManager:
    def __init__(self, facade, send: SendFn, logger: logging.Logger | None = None):
        self.f = facade
        self.send = send
        self.logger = logger or logging.getLogger("lolhelper")
        self._task: asyncio.Task | None = None
        self._last: dict[str, float] = {}

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.exception("[lolhelper] 推送任务出错")
            await asyncio.sleep(60)

    def _due(self, name: str, interval: float) -> bool:
        now = time.time()
        if now - self._last.get(name, 0) >= interval:
            self._last[name] = now
            return True
        return False

    async def tick(self) -> None:
        s = self.f.s
        if not s.push_enabled:
            return
        store = self.f.store
        if store.subs(kind="match") and self._due("match", s.push_match_interval):
            await self.check_matches()
        if store.subs(kind="esports") and self._due("esports", s.push_esports_interval):
            await self.check_esports()
        if store.subs(kind="lol_daily") or store.subs(kind="tft_daily"):
            await self.check_daily()

    async def _broadcast(self, umos: list[str], view: View) -> None:
        for umo in dict.fromkeys(umos):
            try:
                await self.send(umo, view)
            except Exception as exc:
                self.logger.warning(f"[lolhelper] 推送到 {umo} 失败：{exc}")

    # ------------------------------------------------------------------ matches
    async def check_matches(self) -> None:
        groups: dict[str, dict] = {}
        for sub in self.f.store.subs(kind="match"):
            acc = (sub.get("target") or {}).get("account")
            if not acc:
                continue
            g = groups.setdefault(account_key(acc), {"account": acc, "umos": []})
            g["umos"].append(sub["umo"])
        for key, g in groups.items():
            try:
                matches = await self.f.latest_matches(g["account"], 5)
            except LolHelperError as exc:
                self.logger.info(f"[lolhelper] 检查 {account_label(g['account'])} 新对局失败：{exc}")
                continue
            ids = [m["id"] for m in matches if m.get("id")]
            state_key = f"seen:{key}"
            seen = self.f.store.state(state_key)
            if seen is None:  # first run: remember without pushing
                self.f.store.set_state(state_key, ids[:30])
                continue
            new = [m for m in matches if m.get("id") and m["id"] not in seen]
            if not new:
                continue
            self.f.store.set_state(state_key, (ids + [i for i in seen if i not in ids])[:30])
            for m in reversed(new[: self.f.s.push_max_new_matches]):
                await self._broadcast(g["umos"], await self.match_view(g["account"], m))
            await asyncio.sleep(1)

    async def match_view(self, account: dict, m: dict) -> View:
        from .game import add_match_cards, minimal_game

        game = m.get("_game")
        if not game and account.get("kind") != "cn":
            try:
                game = (await self.f.svc.games_for([m], account["game_name"], account["tag_line"], account["region"]))[0]
            except Exception:
                game = None
        game = game or minimal_game(m)
        res = str(m.get("result") or "").upper()
        word = {"WIN": "赢下了一局", "LOSE": "输掉了一局"}.get(res, "结束了一局")
        doc = Doc(
            title=f"{account.get('game_name')}#{account.get('tag_line')} {word}",
            subtitle=account_label(account),
            icon=self.f.gd.champ_icon(self.f.gd.champion(m.get("champion_id"))) if m.get("champion_id") else None,
            theme="cn" if account.get("kind") == "cn" else "lol",
            width=880,
        )
        add_match_cards(doc, "", [game], self.f.gd)
        doc.footer = f"对局时间 {local_time(m.get('created_at'), self.f.s.timezone)} · 发送「/lol 对局」查看详情"
        match = {k: v for k, v in m.items() if k != "_game"}
        return View("push_match", {"account": account, "match": match}, doc, time.time())

    # ------------------------------------------------------------------ esports
    async def check_esports(self) -> None:
        subs = self.f.store.subs(kind="esports")
        svc: OpggService = self.f.svc
        wanted = [sub_league_list(s) for s in subs]
        fetch = ([None] if any(not w for w in wanted) else []) + sorted({x for w in wanted for x in w})
        try:
            up_games, _ = await svc.esports_games("schedule", fetch)
            res_games, _ = await svc.esports_games("result", fetch)
        except LolHelperError as exc:
            self.logger.info(f"[lolhelper] 获取赛程失败：{exc}")
            return
        store = self.f.store
        reminded = set(store.state("esports_reminded", []))
        resulted = store.state("esports_resulted")
        now = time.time()
        if resulted is None:  # first run: do not flood with old results
            store.set_state("esports_resulted", [g.get("id") for g in res_games][-200:])
            resulted = []
        resulted = set(resulted)

        def targets(game: dict) -> list[str]:
            name = str(game.get("league") or "").lower()
            out = []
            for s in subs:
                lgs = sub_league_list(s)
                if not lgs or any(lg == name or lg in name or name in lg for lg in lgs):
                    out.append(s["umo"])
            return out

        for g in up_games:
            start = parse_time(g.get("scheduledAt"))
            if not start or g.get("id") in reminded:
                continue
            delta = start.timestamp() - now
            if 0 < delta <= self.f.s.push_esports_remind:
                umos = targets(g)
                if umos:
                    await self._broadcast(umos, self.esports_view(g, f"{int(delta // 60)} 分钟后开赛"))
                reminded.add(g.get("id"))
        pending = dict(store.state("esports_pending", {}) or {})
        for g in res_games:
            if g.get("status") != "FINISHED" or g.get("id") in resulted:
                continue
            start = parse_time(g.get("scheduledAt"))
            if start and now - start.timestamp() < 2 * 86400:
                umos = targets(g)
                if umos:
                    view = await self.result_view(g)
                    if view is None:  # per-game stats are not published yet; retry next round
                        pending.setdefault(str(g.get("id")), now)
                        if now - pending[str(g.get("id"))] < 40 * 60:
                            continue
                        view = self.esports_view(g, "比赛结束")
                    await self._broadcast(umos, view)
            resulted.add(g.get("id"))
            pending.pop(str(g.get("id")), None)
        store.set_state("esports_reminded", list(reminded)[-300:])
        store.set_state("esports_resulted", list(resulted)[-300:])
        store.set_state("esports_pending", pending)

    async def result_view(self, g: dict) -> View | None:
        """Series card with per-game stats, or None while OP.GG has not published every game yet."""
        try:
            games, _ = await self.f.svc.esports_series_games(g)
        except LolHelperError as exc:
            self.logger.info(f"[lolhelper] 获取比赛数据失败：{exc}")
            return None
        played = int(g.get("homeScore") or 0) + int(g.get("awayScore") or 0)
        if len(games) < played:
            self.f.svc.cache.clear(f"esgames:{g.get('id')}:")
            return None
        return await self.f.svc.esports_match(g, headline="比赛结束", key=str(g.get("homeTeam", {}).get("acronym") or ""))

    def esports_view(self, g: dict, headline: str) -> View:
        home, away = g.get("homeTeam") or {}, g.get("awayTeam") or {}
        h, a = home.get("acronym") or home.get("name"), away.get("acronym") or away.get("name")
        stage = stage_zh(str(g.get("name") or "").split(":")[0].strip())
        doc = Doc(title=f"{h} vs {a}", theme="esports")
        info = league_theme(doc, g.get("league"))
        doc.subtitle = " · ".join(x for x in [info["full"], stage] if x)
        if not doc.icon:
            doc.icon, doc.icon_logo = home.get("image_url"), True
        doc.badge(headline, "accent")
        row, line = versus_row(g, self.f.s.timezone)
        doc.add_block("", "versus", {"rows": [row]}, [line])
        doc.link = g.get("details") or ""
        return View("push_esports", g, doc, time.time())

    # -------------------------------------------------------------------- daily
    async def check_daily(self) -> None:
        now = datetime.now(get_tz(self.f.s.timezone))
        if now.hour != self.f.s.push_daily_hour:
            return
        today = now.strftime("%Y-%m-%d")
        for kind in ("lol_daily", "tft_daily"):
            subs = self.f.store.subs(kind=kind)
            if not subs or self.f.store.state(f"daily:{kind}") == today:
                continue
            try:
                view = await (self.f.tier(None) if kind == "lol_daily" else self.f.tft.meta(10))
            except LolHelperError as exc:
                self.logger.info(f"[lolhelper] 日报生成失败：{exc}")
                continue
            self.f.store.set_state(f"daily:{kind}", today)
            await self._broadcast([s["umo"] for s in subs], view)

