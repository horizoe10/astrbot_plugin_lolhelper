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



WG_DETAIL = {
    "game_id": "600930755727", "game_start_time": "1786366988048", "game_time_played": 1800, "map_id": 11,
    "game_queue_id": 420, "was_early_surrender": 0,
    "team_details": [
        {"teamId": "100", "win": "Win", "totalKills": 34, "totalGoldEarned": 67279, "totalTurretsKilled": 10,
         "totalDragonKills": 1, "totalBaronKills": 0, "banInfoList": [{"championId": 21}, {"championId": -1}]},
        {"teamId": "200", "win": "Fail", "totalKills": 39, "totalGoldEarned": 62976, "totalTurretsKilled": 6,
         "totalDragonKills": 3, "totalBaronKills": 1, "banInfoList": [{"championId": 72}]},
    ],
    "player_details": [
        {"name": "%E5%BC%84%E6%AD%BB%E6%B1%9F%E7%88%BD%2343924", "teamId": "100", "position": "MIDDLE", "win": "Win",
         "championId": 157, "PERK0": 8112, "perkSubStyle": 8200, "summonSpell1Id": 14, "summonSpell2Id": 4,
         "item0": 4646, "item1": 0, "item6": 3340, "championsKilled": 10, "numDeaths": 2, "assists": 5, "level": 18,
         "minionsKilled": 200, "neutralMinionsKilled": 12, "goldEarned": 15000, "totalDamageToChampions": 25846,
         "totalDamageTaken": 20000, "gameScore": 64391, "openid": "me"},
        {"name": "Uzi%235555", "teamId": "200", "position": "BOTTOM", "win": "Fail", "championId": 81,
         "championsKilled": 3, "numDeaths": 5, "assists": 4, "openid": "other"},
    ],
}


def test_wegame_normalizers(gd):
    from lolhelper.game import target_entry
    from lolhelper.wegame import normalize_wg_game, parse_cookie, wg_ranked, wg_server

    assert parse_cookie("a=1; tgp_id=7; x=2;tgp_ticket=ABC") == "tgp_id=7; tgp_ticket=ABC"
    assert parse_cookie("p_skey=1") == ""
    assert wg_server(4) == "TENCENT_GZ100" and wg_server(1) == "TENCENT_HN1" and wg_server(14) == "TENCENT_HN10"
    ranked = wg_ranked({"season": {"tier": 2, "queue": 2, "win_point": 60, "wins": 20, "losses": 26,
                                   "team_tier": 255, "team_queue": 0}})
    assert ranked == [{"queue": "RANKED_SOLO_5x5", "tier": "PLATINUM", "division": "III", "lp": 60, "wins": 20, "losses": 26}]
    assert wg_ranked({"season": {"tier": 0, "queue": 0, "win_point": 900}})[0]["division"] is None  # 最强王者
    g = normalize_wg_game(WG_DETAIL, "me", gd)
    assert g["queue"] == "单双排" and g["source"] == "wegame" and g["duration"] == 1800
    blue, red = g["teams"]
    assert blue["win"] and blue["towers"] == 10 and blue["bans"] == [21] and not red["win"]
    me = blue["players"][0]
    assert (me["game_name"], me["tag_line"], me["position"]) == ("弄死江爽", "43924", "mid")
    assert me["keystone"] == 8112 and me["spells"] == [14, 4] and me["items"] == [4646, 3340] and me["cs"] == 212
    assert me["score"] == 6.4 and me["is_target"]
    e = target_entry(g)
    assert e["result"] == "WIN" and e["kills"] == 10


def test_cn_falls_back_to_wegame(gd):
    import asyncio

    from lolhelper.cn import CnService
    from lolhelper.errors import BridgeError
    from lolhelper.service import OpggService

    class DeadBridge:
        mode, label = "remote", "http://x"

        async def lookup(self, *a):
            raise BridgeError("连接远程国服代理失败")

        async def matches(self, *a):
            raise BridgeError("连接远程国服代理失败")

    class FakeWg:
        configured, expired = True, False

        async def search(self, name, tag=""):
            return [{"openid": "me", "area": 4, "level": 300, "tag_num": 43924, "icon_id": 1}]

        async def battles(self, openid, area, offset=0, count=10):
            return [{"game_id": "600930755727", "game_queue_id": 420, "win": "Win", "champion_id": 157}]

        async def detail(self, openid, area, game_id):
            return WG_DETAIL

    svc = OpggService(mcp=None, gamedata=gd)
    cn = CnService(DeadBridge(), svc, wegame=FakeWg())

    async def run():
        acc = await cn.resolve_account("弄死江爽", "43924", None)
        assert acc["wg_openid"] == "me" and acc["server"] == "TENCENT_GZ100" and acc["puuid"] == ""
        [m] = await cn.recent_matches(acc, 1)
        assert m["_game"]["source"] == "wegame" and len(m["_game"]["teams"]) == 2

    asyncio.run(run())
