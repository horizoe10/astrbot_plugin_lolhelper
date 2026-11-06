import re

from lolhelper.dsl import dig, parse_opgg_text
from lolhelper.fmt import freshness
from lolhelper.game import build_game_view, normalize_opgg_game, normalize_sgp_game
from lolhelper.render import render_text
from lolhelper.render.html import render_html

OPGG = """class LolGetSummonerGameDetail: data
class Data: game_detail
class GameDetail: id,created_at,game_type,game_length_second,average_tier_info,teams
class AverageTierInfo: tier,division
class Team: key,game_stat,banned_champions,participants
class GameStat: is_win,champion_kill,dragon_kill,baron_kill,tower_kill,horde_kill,gold_earned
class Participant: is_target,summoner,champion_id,champion_name,team_key,position,items,spells,stats
class Summoner: game_name,tagline
class Stats: champion_level,total_damage_taken,total_damage_dealt_to_champions,ward_place,kill,death,assist,minion_kill,neutral_minion_kill,gold_earned,result,op_score,op_score_rank

LolGetSummonerGameDetail(Data(GameDetail("m_1","2026-10-17T01:53:14+09:00","SOLORANKED",1011,AverageTierInfo("GRANDMASTER",1),[Team("BLUE",GameStat(false,14,1,0,0,2,34679),[238],[Participant(false,Summoner("enemy","KR2"),238,"影流之主","BLUE","MID",[3031,0],[4,14],Stats(11,18453,14251,6,7,7,1,110,0,9192,"LOSE",4.38,7)),Participant(false,Summoner("enemy2","KR3"),62,"齐天大圣","BLUE","JUNGLE",[],[11,4],Stats(10,1,9000,6,2,5,3,10,120,7000,"LOSE",5.1,5))]),Team("RED",GameStat(true,29,1,0,3,1,41010),[157],[Participant(true,Summoner("Hide on bush","KR1"),157,"疾风剑豪","RED","MID",[3031,6673],[4,14],Stats(12,15000,13660,5,7,4,6,123,0,8205,"WIN",6.93,4)),Participant(false,Summoner("ally","KR1"),777,"封魔剑魂","RED","TOP",[],[4,12],Stats(13,17127,22529,6,10,2,5,146,0,6632,"WIN",8.4,1))])])))"""


def test_opgg_game_detail_card(gd):
    g = dig(parse_opgg_text(OPGG), "data.game_detail")
    detail = normalize_opgg_game(g, gd)
    blue, red = detail["teams"]
    assert [p["position"] for p in blue["players"]] == ["jungle", "mid"]  # sorted by lane
    assert red["win"] and red["players"][1]["is_target"] and red["players"][1]["cs"] == 123
    view = build_game_view(detail, gd, created=1762400000)
    doc = view.doc
    assert doc.title == "胜利 · 单双排 · 16:51"
    assert "Hide on bush#KR1" in doc.subtitle and "傲世宗师" in doc.subtitle
    text = render_text(doc)
    assert "失败 蓝队 14/" in text and "胜利 红队 29/" in text
    assert "MVP" in text and "ACE" in text  # best of winners / losers
    assert "禁用 疾风剑豪" in text and "▶ 疾风剑豪 Hide on bush 7/4/6" in text
    html = render_html(doc)
    assert 'class="trow me"' in html and "SummonerFlash.png" in html


def test_sgp_game_detail_without_scores(gd):
    def p(puuid, team, champ, k, win):
        return {"puuid": puuid, "teamId": team, "championId": champ, "kills": k, "deaths": 1, "assists": 2, "win": win,
                "riotIdGameName": puuid, "riotIdTagline": "1", "champLevel": 15, "goldEarned": 9000,
                "totalDamageDealtToChampions": 1000 * k, "teamPosition": "MIDDLE", "summoner1Id": 4, "summoner2Id": 14}

    raw = {"json": {"gameId": 9, "gameCreation": 1762400000000, "gameDuration": 1500, "queueId": 420,
                    "teams": [{"teamId": 100, "win": True, "objectives": {"tower": {"kills": 9}, "champion": {"kills": 10}},
                               "bans": [{"championId": 238}, {"championId": -1}]},
                              {"teamId": 200, "win": False, "objectives": {"tower": {"kills": 2}}}],
                    "participants": [p("me", 100, 157, 8, True), p("x", 200, 62, 3, False)]}}
    detail = normalize_sgp_game(raw, "me", gd)
    assert detail["teams"][0]["bans"] == [238] and detail["teams"][0]["towers"] == 9
    text = render_text(build_game_view(detail, gd, "cn").doc)
    assert "参团" in text and "100%" in text  # (8+2)/10 kill participation
    assert "数据来自国服客户端" in text


def test_freshness_is_absolute_time():
    assert re.fullmatch(r"数据更新于 (\d{4}-)?\d{2}-\d{2} \d{2}:\d{2}", freshness(1762400000))

