import pytest

from lolhelper.args import parse_args, parse_target, pick_game_mode, pick_position, strip_command
from lolhelper.errors import UsageError


def test_riot_id_with_spaces_and_region():
    t = parse_target("Hide on bush#KR1 kr".split())
    assert (t.game_name, t.tag_line, t.region, t.is_cn) == ("Hide on bush", "KR1", "kr", False)


def test_region_first_and_chinese_alias():
    t = parse_target("日服 なまえ#JP1".split())
    assert t.region == "jp" and t.game_name == "なまえ"


def test_cn_server_names_and_legacy_names():
    t = parse_target("测试玩家#12345 艾欧尼亚".split())
    assert t.is_cn and t.cn_server == "TENCENT_HN1"
    t = parse_target("祖安 测试玩家#12345".split())
    assert t.cn_server == "TENCENT_NJ100"
    t = parse_target("国服 测试玩家＃12345".split())
    assert t.is_cn and t.cn_server is None and t.tag_line == "12345"


def test_missing_tag_is_rejected():
    with pytest.raises(UsageError):
        parse_target(["Faker"])


def test_flags_and_count():
    a = parse_args("亚索 中单 -i -n 5")
    assert a.words == ["亚索", "中单"] and a.mode == "image" and a.count == 5
    assert parse_args("x --md").mode == "markdown"


def test_position_and_mode():
    pos, rest = pick_position(["李青", "打野"])
    assert pos == "jungle" and rest == ["李青"]
    mode, rest = pick_game_mode(["大乱斗", "亚索"])
    assert mode == "aram" and rest == ["亚索"]


def test_strip_command():
    assert strip_command("/lol 战绩 Hide on bush#KR1", [["lol", "战绩"]]) == "Hide on bush#KR1"
    assert strip_command("lol 战绩", [["lol", "战绩"]]) == ""

