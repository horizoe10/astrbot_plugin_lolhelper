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

