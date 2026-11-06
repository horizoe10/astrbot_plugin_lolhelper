from lolhelper.opgg_web import analysis, lane_rows, pick_scope, scope_label


def test_pick_scope():
    assert pick_scope(["中单", "韩服", "钻石+"]) == ("kr", "diamond_plus", ["中单"])
    assert pick_scope(["全球", "大师"]) == ("global", "master", [])
    assert pick_scope(["亚索"]) == (None, None, ["亚索"])
    assert pick_scope(["翡翠＋"])[1] == "emerald_plus"  # full-width plus
    assert scope_label("global", "emerald_plus") == "全球 · 翡翠+"


def test_lane_rows_and_analysis(gd):
    data = [
        {"id": 157, "positions": [{"name": "MID", "stats": {"win_rate": 0.49, "pick_rate": 0.08, "ban_rate": 0.25,
                                                          "role_rate": 0.6, "tier_data": {"tier": 3, "rank": 2, "rank_prev": 4}}}]},
        {"id": 103, "positions": [{"name": "MID", "stats": {"win_rate": 0.51, "tier_data": {"tier": 1, "rank": 1}}}]},
    ]
    rows = lane_rows(data, gd)
    assert [r["champion_id"] for r in rows["mid"]] == [103, 157] and rows["top"] == []
    assert rows["mid"][1]["rank_prev"] == 4 and rows["mid"][1]["champion"] == "疾风剑豪"

    detail = {
        "summary": {"average_stats": {"play": 100, "win_rate": 0.5},
                    "positions": [{"name": "MID", "stats": {"win_rate": 0.48, "tier_data": {"tier": 3, "rank": 15}}}]},
        "runes": [{"primary_page_id": 8000, "secondary_page_id": 8400, "primary_rune_ids": [8008]}],
        "core_items": [{"ids": [6673, 3031, 6333], "play": 10, "win": 6}],
        "boots": [{"ids": [3006]}], "starter_items": [{"ids": [1055, 2003]}],
        "last_items": [{"ids": [6673]}, {"ids": [3072], "pick_rate": 0.27}],
        "counters": [{"champion_id": 1, "play": 100, "win": 40}, {"champion_id": 2, "play": 100, "win": 60},
                     {"champion_id": 3, "play": 2, "win": 2}],
    }
    d = analysis(detail, "mid", gd)
    st = d["summary"]["average_stats"]
    assert st["win_rate"] == 0.48 and st["tier"] == 3 and st["rank"] == 15  # position stats win over overall
    assert "primary_page_name" in d["runes"] and "secondary_page_name" in d["runes"]
    assert [x["ids"][0] for x in d["later_items"]] == [3072]  # core items are not repeated
    assert d["weak_counters"][0]["champion_id"] == 1 and d["strong_counters"][0]["champion_id"] == 2
    assert all(c["champion_id"] != 3 for c in d["weak_counters"])  # tiny samples dropped
