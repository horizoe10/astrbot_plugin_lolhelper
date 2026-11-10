from lolhelper.args import parse_leagues
from lolhelper.constants import league_info, stage_zh
from lolhelper.esports import normalize_esports_game, series_row
from lolhelper.storage import sub_league_list


def test_league_info_and_stage_names():
    assert league_info("Worlds")["short"] == "全球总决赛" and league_info("Worlds")["color"]
    assert league_info("LPL")["color"] != league_info("Worlds")["color"]
    assert league_info("Unknown Cup") == {"key": "unknown cup", "short": "Unknown Cup", "full": "Unknown Cup", "color": "", "logo": None}
    assert stage_zh("Quarterfinal 1") == "八强赛 第1场"
    assert stage_zh("Lower Bracket Final") == "败者组 决赛"
    assert stage_zh("Split 3 Playoffs") == "第3赛段 季后赛"


def test_parse_leagues_multi_and_aliases():
    assert parse_leagues(["S赛", "LPL"]) == (["worlds", "lpl"], [])
    assert parse_leagues(["LTA", "South", "BLG"]) == (["lta south"], ["BLG"])
    assert parse_leagues(["lpl", "LPL"]) == (["lpl"], [])


def test_league_icons_and_banner_backdrop():
    from lolhelper.constants import league_backdrop, set_league_posters

    lpl = league_info("LPL")
    assert lpl["logo"].startswith("https://static.lolesports.com/leagues/")  # colour icon from LoL Esports
    assert league_backdrop(lpl) == {"wm": "https://s-qwer.op.gg/images/symbols/LPL@white.png"}  # one-colour watermark
    assert league_backdrop(league_info("Worlds"))["wm"] == league_info("Worlds")["logo"]
    assert league_backdrop(league_info("Unknown Cup")) == {"wm": "mark"}
    set_league_posters({"lpl": "https://x/lpl.jpg"})
    try:
        assert league_backdrop(lpl) == {"art": "https://x/lpl.jpg"}
    finally:
        set_league_posters({})


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


def test_role_bound_slot(gd):
    """esports lists the role-bound slot among the items; it can hold boots (bot lane) or a control ward (support)."""
    from lolhelper.game import _slots

    g = _game()
    sup, adc, jng = g["players"]
    sup.update(items=[2055, 3364, 3158, 3869, 2055], roleBoundItem=2055)  # the second ward was bought into a normal slot
    adc.update(position="adc", items=[1038, 3363, 3047, 3078, 6676, 3042, 1086, 1037], roleBoundItem=3047)
    jng.update(position="jun", items=[1036, 3364, 6333, 1205, 3071], roleBoundItem=1209)  # another id of the jungle reward
    by = {p["game_name"]: p for t in normalize_esports_game(g, gd)["teams"] for p in t["players"]}
    bot = by["Bin"]
    assert bot["role_item"] == 3047 and 3047 not in bot["items"]
    slots = _slots(bot["items"], gd, bot["role_item"])
    assert len(slots) == 8 and all(slots[:6]) and slots[6].endswith("/3363.png") and slots[7].endswith("/3047.png")
    assert by["ON"]["role_item"] == 2055 and by["ON"]["items"].count(2055) == 1
    assert by["ShowMaker"]["role_item"] == 1209 and 1205 not in by["ShowMaker"]["items"]
