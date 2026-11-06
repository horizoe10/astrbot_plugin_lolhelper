import pytest

from lolhelper.errors import NotFoundError


def test_resolve_champion_by_names_and_nicknames(gd):
    assert gd.resolve_champion("亚索")["key"] == "Yasuo"
    assert gd.resolve_champion("疾风剑豪")["key"] == "Yasuo"
    assert gd.resolve_champion("快乐风男")["key"] == "Yasuo"
    assert gd.resolve_champion("wukong")["key"] == "MonkeyKing"
    assert gd.resolve_champion("猴子")["key"] == "MonkeyKing"
    assert gd.champion_by_name("影流之主")["key"] == "Zed"


def test_resolve_champion_errors(gd):
    with pytest.raises(NotFoundError):
        gd.resolve_champion("不存在的英雄")


def test_tft_item_prefers_shortest_match(gd):
    assert gd.resolve_tft_item("大天使") == "DA_ArchangelsStaff"
    assert gd.resolve_tft_item("光明版大天使") == "DA_ArchangelsStaffRadiant"
    assert gd.resolve_tft_champion("凯特琳") == "DA_18_Caitlyn"
    assert gd.tft_trait_name("DA_18_Solar") == "日蚀骑士"


def test_icons(gd):
    assert gd.champ_icon(gd.champion(157)).endswith("/16.22.1/img/champion/Yasuo.png")
    assert gd.item_icon(0) is None

