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

