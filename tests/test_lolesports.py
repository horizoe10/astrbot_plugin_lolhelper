"""LoL Esports power-ranking logos: page parsing and matching OP.GG teams to ranked teams."""
import json

from lolhelper.lolesports import match_team, parse_gpr
from lolhelper.net import image_type

PUSH = '<script>(window[Symbol.for("ApolloSSRDataTransport")] ??= []).push(%s)</script>'


def _team(tid, code, name, img, league="LPL"):
    return {"__typename": "Team", "id": tid, "code": code, "name": name, "image": img,
            "homeLeague": {"__typename": "HomeLeague", "id": "9", "name": league, "slug": league.lower(),
                           "image": f"http://static.lolesports.com/leagues/{league}.png"}}


def test_parse_gpr_reads_teams_and_leagues_from_the_page_data():
    first = {"rehydrate": {"a": {"data": {"__typename": "Query", "teamGPR": [
        {"__typename": "TeamGPR", "rank": "UNDEF", "team": _team("1", "BLG", "BILIBILI GAMING", "http://static.lolesports.com/teams/blg.png")},
        {"__typename": "TeamGPR", "team": _team("2", "T1", "T1", "http://static.lolesports.com/teams/t1.png", "LCK")},
    ]}}}}
    second = {"rehydrate": {"b": {"data": {"leagueELO": [
        {"__typename": "LeagueELO", "league": {"__typename": "League", "id": "7", "name": "LEC", "slug": "lec",
                                                "image": "http://static.lolesports.com/leagues/lec.png"}}]}}}}
    # the page writes JavaScript, where a missing value can be the bare word undefined
    html = (PUSH % json.dumps(first).replace('"UNDEF"', "undefined")) + "<div>x</div>" + PUSH % json.dumps(second)
    got = parse_gpr(html)
    teams = {t["code"]: t for t in got["teams"]}
    assert set(teams) == {"BLG", "T1"}
    assert teams["BLG"] == {"code": "BLG", "name": "BILIBILI GAMING", "image": "https://static.lolesports.com/teams/blg.png", "league": "LPL"}
    assert got["leagues"]["LEC"] == "https://static.lolesports.com/leagues/lec.png"
    assert got["leagues"]["LCK"].endswith("/LCK.png")
    assert parse_gpr("<html>no data</html>") == {"teams": [], "leagues": {}}


RANKED = [
    {"code": "BLG", "name": "BILIBILI GAMING", "image": "blg"},
    {"code": "JDG", "name": "Beijing JDG Esports", "image": "jdg"},
    {"code": "TLAW", "name": "Team Liquid Alienware", "image": "tl"},
    {"code": "WBG", "name": "WeiboGaming", "image": "wbg"},
    {"code": "SK", "name": "SK Gaming", "image": "sk"},
    {"code": "NAVI", "name": "Natus Vincere", "image": "navi"},
    {"code": "LEV", "name": "LEVIATÁN", "image": "lev"},
]


def _img(acronym, name):
    hit = match_team(RANKED, acronym, name)
    return hit["image"] if hit else None


def test_match_team_by_code_or_full_name():
    assert _img("BLG", "Bilibili Gaming") == "blg"
    assert _img("JDG", "JD Gaming") == "jdg"  # names differ, but both contain the code
    assert _img("Navi", "Natus Vincere") == "navi"
    assert _img("LEV", "Leviatan Esports") == "lev"  # accents ignored, one name extends the other
    assert _img("WB", "Weibo Gaming") == "wbg"  # different code, same name
    assert _img("TL", "Team Liquid") == "tl"
    assert _img("BLG", None) == "blg"


def test_match_team_rejects_unrelated_teams():
    assert _img("SK", "Supermassive") is None  # same code, nothing else in common
    assert _img("T1A", "T1 Academy") is None
    assert _img("", "") is None


def test_image_type_from_signature():
    assert image_type(b"\x89PNG\r\n\x1a\n" + b"0" * 8) == "image/png"
    assert image_type(b"\xff\xd8\xff\xe0" + b"0" * 8) == "image/jpeg"
    assert image_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert image_type(b"<html>") is None

