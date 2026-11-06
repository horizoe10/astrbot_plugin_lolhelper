from lolhelper.args import parse_leagues
from lolhelper.esports import normalize_esports_game, series_row
from lolhelper.storage import sub_league_list


def test_parse_leagues_multi_and_aliases():
    assert parse_leagues(["S赛", "LPL"]) == (["worlds", "lpl"], [])
    assert parse_leagues(["LTA", "South", "BLG"]) == (["lta south"], ["BLG"])
    assert parse_leagues(["lpl", "LPL"]) == (["lpl"], [])


def test_sub_league_list_old_and_new_forms():
    assert sub_league_list({"target": {"league": "lpl"}}) == ["lpl"]
    assert sub_league_list({"target": {"leagues": ["worlds", "lpl"]}}) == ["worlds", "lpl"]
    assert sub_league_list({"target": {}}) == []


def _game():
    def p(side, pos, champ, nick, score):
        return {"side": side, "position": pos, "championId": champ, "kills": 3, "deaths": 1, "assists": 4,
                "items": [3006, 0], "spells": [4, 12], "opScore": score, "creepScore": 200, "goldEarned": 10000,
                "totalDamageDealtToChampions": 15000, "totalDamageTaken": 9000,
                "perk": {"primary": {"style": 8000, "selections": [8005]}, "sub": {"style": 8100}},
                "player": {"nickName": nick}}
    return {
        "id": "1", "set": 2, "length": 1900, "finished": True, "beginAt": "2026-11-05T22:08:37.000Z",
        "winner": {"id": "632"},
        "teams": [
            {"side": "red", "kills": 9, "towerKills": 3, "dragonKills": 0, "baronKills": 0, "goldEarned": 60000, "bans": [1], "team": {"id": "1681", "acronym": "DK"}},
            {"side": "blue", "kills": 24, "towerKills": 11, "dragonKills": 5, "baronKills": 2, "goldEarned": 72000, "bans": [2], "team": {"id": "632", "acronym": "BLG"}},
        ],
        "players": [p("blue", "sup", 1, "ON", 5), p("blue", "top", 2, "Bin", 9), p("red", "mid", 3, "ShowMaker", 4)],
    }


def test_normalize_and_series_row(gd):
    n = normalize_esports_game(_game(), gd)
    assert [t["label"] for t in n["teams"]] == ["BLG", "DK"]  # blue side first
    blg = n["teams"][0]
    assert blg["win"] and not n["teams"][1]["win"]
    assert [p["game_name"] for p in blg["players"]] == ["Bin", "ON"]  # sorted top -> support
    assert blg["players"][0]["keystone"] == 8005 and blg["players"][0]["sub_style"] == 8100
    # the match lists DK as home team even though DK played red side
    row, line = series_row(n, {"homeTeam": {"id": "1681", "acronym": "DK"}}, gd)
    assert row["home"]["label"] == "DK" and row["home"]["lose"] and row["away"]["win"]
    assert row["mvp"]["name"] == "Bin" and "BLG 胜" in line
