import asyncio
from types import SimpleNamespace

from lolhelper.doc import Doc
from lolhelper.errors import NotFoundError
from lolhelper.push import PushManager
from lolhelper.service import OpggService, View
from lolhelper.settings import Settings
from lolhelper.storage import StateStore


class FakeFacade:
    def __init__(self, gd, store, settings=None):
        self.s = settings or Settings()
        self.gd = gd
        self.store = store
        self.svc = OpggService(mcp=SimpleNamespace(tools={}), gamedata=gd)
        self.mcp = SimpleNamespace(tools={"x": 1})
        self.matches: list[dict] = []

    async def latest_matches(self, account, count=3):
        return list(self.matches)

    async def tier(self, position=None):
        if position == "support":
            raise NotFoundError("nothing")
        doc = Doc(title="版本梯队").add_list("", ["a", "b"])
        return View("lol_tier", {"position": position}, doc)

    def text(self, view):
        return "TEXT:" + view.doc.title

    def markdown(self, view):
        return "MD:" + view.doc.title

    def view_payload(self, view):
        return {"view": view.kind, "data": view.data, "text": self.text(view)}


def m(i, result="WIN"):
    return {"id": str(i), "champion_id": 157, "champion_name": "疾风剑豪", "result": result,
            "kills": 1, "deaths": 1, "assists": 1, "cs": 100, "queue": "单双排", "created_at": 1762400000}


def test_push_new_matches_only_after_first_seen(tmp_path, gd):
    async def run():
        store = StateStore(tmp_path / "s.json")
        await store.load()
        acc = {"kind": "global", "region": "kr", "game_name": "A", "tag_line": "B"}
        await store.add_sub("umo1", "match", {"account": acc})
        await store.add_sub("umo2", "match", {"account": acc})
        f = FakeFacade(gd, store)
        sent = []

        async def send(umo, view):
            sent.append((umo, view.data["match"]["id"]))

        pm = PushManager(f, send)
        f.matches = [m(2), m(1)]
        await pm.check_matches()
        assert sent == []  # first run only records
        f.matches = [m(4, "LOSE"), m(3), m(2), m(1)]
        await pm.check_matches()
        assert sent == [("umo1", "3"), ("umo2", "3"), ("umo1", "4"), ("umo2", "4")]
        sent.clear()
        await pm.check_matches()
        assert sent == []
        view = await pm.match_view(acc, m(5, "LOSE"))
        assert "输掉了一局" in view.doc.title
        assert view.doc.sections[0].kind == "cards"

    asyncio.run(run())




def test_live_games_push_once_per_finished_game(tmp_path, gd):
    from lolhelper.service import View

    async def run():
        store = StateStore(tmp_path / "s.json")
        await store.load()
        await store.add_sub("g1", "esports", {"league": "worlds"})
        f = FakeFacade(gd, store)
        state = {"score": (1, 0), "games": [{"set": 1, "finished": True}, {"set": 2, "finished": False}]}

        class Esp:
            async def match(self, gid):
                h, a = state["score"]
                return {"id": gid, "status": "running", "homeScore": h, "awayScore": a, "numberOfGames": 5,
                        "homeTeam": {"acronym": "BLG"}, "awayTeam": {"acronym": "DK"},
                        "tournament": {"serie": {"league": {"shortName": "Worlds"}}}}

        async def series_games(g):
            return state["games"], 0

        async def esports_match(g, game_no=None, headline="", key=""):
            return View("lol_esports_match", {}, Doc(title=headline))

        f.svc.esp = Esp()
        f.svc.esports_series_games = series_games
        f.svc.esports_match = esports_match
        sent = []

        async def send(umo, view):
            sent.append((umo, view.doc.title))

        pm = PushManager(f, send)
        import time as _t
        from datetime import datetime, timezone
        start = datetime.fromtimestamp(_t.time() - 3600, timezone.utc).isoformat()
        games = [{"id": 1, "league": "Worlds", "scheduledAt": start, "status": "RUNNING"}]
        targets = lambda g: ["g1"]
        assert await pm.check_live_games(games, set(), targets, _t.time()) is True
        assert sent == [("g1", "第1局结束 · BLG 1:0 DK")]
        await pm.check_live_games(games, set(), targets, _t.time())
        assert len(sent) == 1  # same game is not pushed twice
        state.update(score=(1, 1), games=[{"set": 1, "finished": True}, {"set": 2, "finished": True}])
        await pm.check_live_games(games, set(), targets, _t.time())
        assert sent[-1] == ("g1", "第2局结束 · BLG 1:1 DK")
        state.update(score=(3, 1), games=[{"set": i, "finished": True} for i in range(1, 5)])
        await pm.check_live_games(games, set(), targets, _t.time())
        assert len(sent) == 2  # series decided: left to the full result card

    asyncio.run(run())
