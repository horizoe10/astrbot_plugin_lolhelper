"""Group play logic: ranks, team split, bets, highlights, boards and titles."""
import random
from datetime import timezone

from lolhelper.group import (bet_result, board_rows, counts, current_streak, highlights, ledger_row, member_stats,
                             parse_bet, rank_score, split_teams, titles)


def row(i, win=True, k=5, d=3, a=7, score=6.0, rank=3, multi=1, ts=None, secs=1800, queue="单双排", cs=200, pos="mid"):
    return {"id": str(i), "ts": ts if ts is not None else 1_800_000_000 + i * 3600, "res": "WIN" if win else "LOSE", "win": win,
            "k": k, "d": d, "a": a, "cid": 1, "champ": "x", "pos": pos, "score": score, "rank": rank, "multi": multi,
            "queue": queue, "secs": secs, "cs": cs}


def test_rank_score_order():
    s = [rank_score({"tier": t, "division": dv, "lp": lp}) for t, dv, lp in
         [("SILVER", "I", 90), ("GOLD", "IV", 0), ("EMERALD", "II", 50), ("MASTER", 1, 120), ("CHALLENGER", 1, 1500)]]
    assert s == sorted(s) and s[3] == 2920
    assert rank_score({"tier": "GOLD", "division": 4, "lp": 10}) == rank_score({"tier": "GOLD", "division": "IV", "lp": 10})
    assert rank_score(None) is None and rank_score({"tier": "NONE"}) is None


def test_split_teams_is_balanced():
    scores = [2900, 2400, 2100, 1900, 1700, 1650, 1500, 1300, 1200, 800]
    for seed in range(5):
        a, b, diff = split_teams(scores, random.Random(seed))
        assert sorted(a + b) == list(range(10)) and len(a) == len(b) == 5
        assert abs(sum(scores[i] for i in a) - sum(scores[i] for i in b)) <= 120
    a, b, _ = split_teams([1000, 1000, 1000, 1000, 1000])
    assert sorted(len(x) for x in (a, b)) == [2, 3]


def test_bets_parse_and_settle():
    assert parse_bet(["2", "BLG", "3:1"]) == (2, "BLG", (3, 1))
    assert parse_bet(["T1", "3比0"]) == (None, "T1", (3, 0))
    bet = {"team": "T1", "score": [3, 1]}
    assert bet_result(bet, "BLG", "T1", 1, 3) == (True, True)  # score is the picked team's : the opponent's
    assert bet_result(bet, "BLG", "T1", 2, 3) == (True, False)
    assert bet_result(bet, "BLG", "T1", 3, 2) == (False, False)


def test_highlights_and_counts():
    assert ("win", "五杀！") in highlights(row(1, multi=5))
    assert any("零死亡" in t for _, t in highlights(row(1, k=9, d=0, a=10)))
    assert any(c == "lose" for c, _ in highlights(row(1, k=0, d=11, a=2)))
    assert highlights(row(1)) == []
    assert not counts(row(1, secs=200)) and not counts(row(1, queue="斗魂竞技场")) and counts(row(1))


def test_ledger_row_and_streak():
    r = ledger_row({"id": "a", "created_at": "2026-11-08T20:00:00+08:00", "duration": 1800, "result": "WIN",
                    "kills": 3, "deaths": 1, "assists": 4, "multikill": 2, "queue": "单双排"})
    assert r["win"] and r["multi"] == 2 and r["secs"] == 1800
    rows = [row(1, False), row(2), row(3), row(4)]
    assert current_streak(rows) == (3, True)


def test_member_stats_boards_titles():
    tz = timezone.utc
    a = {"label": "A", "stats": member_stats([row(i, win=i % 4 != 0, k=10, d=2) for i in range(1, 9)], tz)}
    b = {"label": "B", "stats": member_stats([row(i, win=False, k=1, d=9, a=2, score=2.0) for i in range(1, 5)], tz)}
    c = {"label": "C", "stats": member_stats([row(1, k=30, d=0, a=5, multi=5)], tz)}
    assert a["stats"]["games"] == 8 and a["stats"]["wins"] == 6 and a["stats"]["win_streak"] == 3
    assert b["stats"]["lose_streak"] == 4
    wr = board_rows([a, b, c], "winrate", weekly=False)
    assert [x["member"]["label"] for x in wr] == ["A", "B"]  # C has 1 game: below the 3-game minimum
    ts = {t["title"]: t["member"]["label"] for t in titles([a, b, c], weekly=False)}
    assert ts["🌟 五杀达人"] == "C" and ts["🧊 连败熔断"] == "B" and ts["🔥 连胜王"] == "A"

