import asyncio
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from lolhelper.api_server import ApiServer
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


def test_api_auth_formats_and_errors(tmp_path, gd):
    async def run():
        store = StateStore(tmp_path / "s.json")
        s = Settings(api_token="secret")
        f = FakeFacade(gd, store, s)
        api = ApiServer(f)
        from aiohttp import web

        app = web.Application(middlewares=[api._auth])
        app.router.add_get("/health", api.health)
        app.router.add_get("/api/v1/{path:.*}", api.get_view)
        app.router.add_post("/api/v1/push", api.push_view)
        async with TestClient(TestServer(app)) as client:
            assert (await client.get("/health")).status == 200
            assert (await client.get("/api/v1/lol/tier")).status == 401
            h = {"Authorization": "Bearer secret"}
            r = await client.get("/api/v1/lol/tier?position=mid", headers=h)
            body = await r.json()
            assert body["ok"] and body["data"] == {"position": "mid"}
            r = await client.get("/api/v1/lol/tier?format=text&token=secret")
            assert await r.text() == "TEXT:版本梯队"
            r = await client.get("/api/v1/lol/tier?format=html", headers=h)
            assert "版本梯队" in await r.text()
            r = await client.get("/api/v1/lol/tier?position=辅助", headers=h)
            assert r.status == 404 and (await r.json())["error"] == "nothing"
            r = await client.get("/api/v1/lol/tier?position=xx", headers=h)
            assert r.status == 400
            r = await client.post("/api/v1/push", json={"umo": "u", "text": "hi"}, headers=h)
            assert r.status == 403  # disabled by default

    asyncio.run(run())


def test_api_refuses_public_bind_without_token(gd, tmp_path):
    async def run():
        f = FakeFacade(gd, StateStore(tmp_path / "s.json"), Settings(api_host="0.0.0.0", api_token=""))
        assert await ApiServer(f).start() is False

    asyncio.run(run())

