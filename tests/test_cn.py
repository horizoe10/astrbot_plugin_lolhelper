from lolhelper.cn import normalize_lcu_games, normalize_ranked, normalize_sgp_games

ME = "puuid-me"


def sgp_payload():
    def p(puuid, team, kills, win):
        return {
            "puuid": puuid, "teamId": team, "championId": 157, "kills": kills, "deaths": 2, "assists": 3,
            "win": win, "totalMinionsKilled": 150, "neutralMinionsKilled": 10, "teamPosition": "MIDDLE",
            "item0": 3031, "item1": 0, "item2": 6673, "totalDamageDealtToChampions": 20000,
        }

    return {"games": [{"metadata": {}, "json": {
        "gameId": 8721485206, "gameCreation": 1762400000000, "gameDuration": 1800, "queueId": 420,
        "participants": [p(ME, 100, 5, True), p("ally", 100, 5, True), p("enemy", 200, 9, False)],
    }}]}


def test_normalize_sgp(gd):
    [m] = normalize_sgp_games(sgp_payload(), ME, gd)
    assert m["id"] == "8721485206"
    assert m["queue"] == "单双排" and m["result"] == "WIN" and m["position"] == "mid"
    assert m["cs"] == 160 and m["items"] == [3031, 6673]
    assert m["champion_name"] == "疾风剑豪"
    assert abs(m["kill_participation"] - 0.8) < 1e-9


def test_normalize_lcu(gd):
    raw = {"games": {"games": [{
        "gameId": 1, "gameCreation": 1762400000000, "gameDuration": 1500, "queueId": 450,
        "participantIdentities": [{"participantId": 3, "player": {"puuid": ME}}],
        "participants": [{"participantId": 3, "championId": 238, "timeline": {"lane": "MIDDLE"},
                          "stats": {"win": False, "kills": 1, "deaths": 9, "assists": 4, "totalMinionsKilled": 30}}],
    }]}}
    [m] = normalize_lcu_games(raw, ME, gd)
    assert m["queue"] == "极地大乱斗" and m["result"] == "LOSE" and m["champion_name"] == "影流之主"


def test_normalize_ranked_lcu_and_sgp_shapes():
    lcu = {"data": {"queueMap": {"RANKED_SOLO_5x5": {"queueType": "RANKED_SOLO_5x5", "tier": "DIAMOND", "division": "II", "leaguePoints": 50, "wins": 10, "losses": 8},
                                  "RANKED_FLEX_SR": {"queueType": "RANKED_FLEX_SR", "tier": "NONE"}}}}
    sgp = {"data": {"queues": [{"queueType": "RANKED_FLEX_SR", "tier": "GOLD", "rank": "I", "leaguePoints": 0, "wins": 1, "losses": 1}]}}
    assert normalize_ranked(lcu) == [{"queue": "RANKED_SOLO_5x5", "tier": "DIAMOND", "division": "II", "lp": 50, "wins": 10, "losses": 8}]
    assert normalize_ranked(sgp)[0]["division"] == "I"




FAKE_BRIDGE = '''
DEFAULT_CONFIG = {"request_timeout": 15}
ROUTES = {"/v1/status": "status", "/v1/summoner": "summoner"}


class BridgeError(Exception):
    def __init__(self, message, status=502, code="upstream_error"):
        super().__init__(message)
        self.status = status
        self.code = code


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg

    def status(self, q):
        return {"connected": True, "timeout": self.cfg["request_timeout"]}

    def summoner(self, q):
        if q.get("name") == "nobody":
            raise BridgeError("没有这个玩家", 404, "not_found")
        raise BridgeError("未检测到已登录的英雄联盟客户端", 503, "client_not_found")
'''


def test_parse_remote_address():
    from lolhelper.cn import parse_remote

    assert parse_remote("http://192.168.1.5:6198?token=abc") == ("http://192.168.1.5:6198", "abc")
    assert parse_remote("192.168.1.5:6198#abc") == ("http://192.168.1.5:6198", "abc")
    assert parse_remote("http://h:6198/", "old") == ("http://h:6198", "old")
    assert parse_remote("") == ("", "")


def test_builtin_bridge_runs_in_process(tmp_path):
    import asyncio

    import pytest

    from lolhelper.cn import CnBridgeClient
    from lolhelper.errors import BridgeError, NotFoundError

    script = tmp_path / "cn_bridge.py"
    script.write_text(FAKE_BRIDGE, encoding="utf-8")
    c = CnBridgeClient(http=None, url="", timeout=7, bridge_file=script)
    assert c.mode == "builtin" and c.label.startswith("内置")

    async def run():
        assert await c.status() == {"connected": True, "timeout": 7}
        with pytest.raises(NotFoundError):
            await c.lookup("nobody", "1")
        with pytest.raises(BridgeError) as exc:
            await c.lookup("someone", "1")
        assert "远程国服代理" in str(exc.value)  # tells server users what to do

    asyncio.run(run())
