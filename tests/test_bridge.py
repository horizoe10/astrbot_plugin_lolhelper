import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bridge"))

import cn_bridge as br  # noqa: E402

CMDLINE = (
    '"D:/WeGameApps/英雄联盟/LeagueClient/LeagueClientUx.exe" "--riotclient-auth-token=abc" '
    '"--riotclient-app-port=50001" "--app-port=54321" "--remoting-auth-token=Tok-en_1" '
    '"--app-pid=1234" "--region=TENCENT" "--rso_platform_id=HN1"'
)


def test_parse_command_line():
    auth = br._from_cmdline(CMDLINE, "cmdline")
    assert (auth.port, auth.token, auth.pid, auth.platform, auth.region) == (54321, "Tok-en_1", 1234, "HN1", "TENCENT")
    assert br._from_cmdline("no args here", "x") is None


def test_lockfile_discovery(tmp_path):
    lf = tmp_path / "LeagueClient" / "lockfile"
    lf.parent.mkdir()
    lf.write_text("LeagueClient:1234:54321:secretpw:https", encoding="utf-8")
    auth = br.discover_lcu({"install_dir": str(tmp_path), "lockfile": ""})
    assert auth.port == 54321 and auth.token == "secretpw" and auth.source.startswith("lockfile")


class FakeBridge:
    def status(self, q):
        return {"connected": True, "server": "TENCENT_HN1"}

    def summoner(self, q):
        if q.get("name") == "none":
            raise br.BridgeError("找不到", 404, "not_found")
        return {"puuid": "p", "gameName": q.get("name"), "tagLine": q.get("tag")}


def test_http_handler_auth_and_errors():
    server = ThreadingHTTPServer(("127.0.0.1", 0), br.make_handler(FakeBridge(), "tkn"))
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    def get(path, token=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    try:
        assert get("/health")[0] == 200
        assert get("/v1/status")[0] == 401
        assert get("/v1/status", "bad")[0] == 401
        code, body = get("/v1/status", "tkn")
        assert code == 200 and body["data"]["server"] == "TENCENT_HN1"
        code, body = get("/v1/summoner?name=%E7%8E%A9%E5%AE%B6&tag=12345", "tkn")
        assert body["data"]["gameName"] == "玩家"
        code, body = get("/v1/summoner?name=none", "tkn")
        assert code == 404 and body["code"] == "not_found"
        assert get("/v1/nope", "tkn")[0] == 404
    finally:
        server.shutdown()


def test_load_config_generates_token(tmp_path):
    p = tmp_path / "bridge.json"
    cfg = br.load_config(p)
    assert cfg["token"] and json.loads(p.read_text(encoding="utf-8"))["token"] == cfg["token"]
    assert br.load_config(p)["token"] == cfg["token"]

