from lolhelper.dsl import dig, parse_opgg_text

SAMPLE = """class LolGetSummonerProfile: data
class Data: summoner
class Summoner: game_name,tagline,level,league_stats
class LeagueStat: game_type,tier_info,win,lose
class TierInfo: tier,division,lp

LolGetSummonerProfile(Data(Summoner("Hide on bush","KR1",946,[LeagueStat("SOLORANKED",TierInfo("GRANDMASTER",1,1806),448,371),LeagueStat("FLEXRANKED",TierInfo(null,null,null),null,null)])))"""


def test_parse_compact_format():
    data = parse_opgg_text(SAMPLE)
    summ = dig(data, "data.summoner")
    assert summ["game_name"] == "Hide on bush"
    assert summ["level"] == 946
    assert summ["league_stats"][0]["tier_info"] == {"tier": "GRANDMASTER", "division": 1, "lp": 1806}
    assert summ["league_stats"][1]["win"] is None


def test_parse_strings_numbers_and_escapes():
    text = 'class A: data\nclass B: s,n,f,neg,arr\n\nA(B("a\\"b\\u4e2d",3,0.25,-1.5e2,[]))'
    b = parse_opgg_text(text)["data"]
    assert b == {"s": 'a"b中', "n": 3, "f": 0.25, "neg": -150.0, "arr": []}


def test_extra_args_and_unknown_class_are_kept():
    text = "class A: x\n\nA(1,2,Z(3))"
    a = parse_opgg_text(text)
    assert a["x"] == 1
    assert a["_extra"] == [2, {"_type": "Z", "_args": [3]}]


def test_json_passthrough():
    assert parse_opgg_text('{"data": [1, 2]}') == {"data": [1, 2]}
    assert parse_opgg_text("[]") == []


def test_dig_defaults():
    assert dig({"a": {"b": None}}, "a.b", 5) == 5
    assert dig({"a": [{"b": 1}]}, "a.0.b") == 1

