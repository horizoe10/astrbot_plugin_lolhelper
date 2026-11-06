from types import SimpleNamespace

from lolhelper.args import pick_game_mode
from lolhelper.doc import Doc
from lolhelper.render import render_text
from lolhelper.service import OpggService


def test_mayhem_mode_words():
    assert pick_game_mode(["亚索", "海克斯大乱斗"]) == ("mayhem", ["亚索"])
    assert pick_game_mode(["海克斯", "亚索"])[0] == "mayhem"
    assert pick_game_mode(["大乱斗"])[0] == "aram"


def test_augments_grouped_by_rarity_and_ranked(gd):
    svc = OpggService(mcp=SimpleNamespace(tools={}), gamedata=gd)
    augs = [
        {"id": 1, "name": "全能龙魂", "rarity": "prismatic", "icon": None, "performance": 91.7, "popular": 0.18},
        {"id": 2, "name": "回归基本功", "rarity": "prismatic", "icon": None, "performance": 92.1, "popular": 0.17},
        {"id": 3, "name": "下雪天", "rarity": "silver", "icon": None, "performance": 86.4, "popular": 0.21},
        {"id": 4, "name": "冷门强化", "rarity": "gold", "icon": None, "performance": 105.0, "popular": 0.0},
        {"id": 5, "name": "鞭挞", "rarity": "gold", "icon": None, "performance": 85.2, "popular": 0.11},
    ]
    doc = Doc(title="t")
    svc._add_augments(doc, augs)
    titles = [s.title for s in doc.sections]
    assert titles == ["海克斯强化 · 棱彩", "海克斯强化 · 黄金", "海克斯强化 · 白银"]
    prismatic = [t.text for t in doc.sections[0].tiles]
    assert prismatic == ["回归基本功", "全能龙魂"]  # sorted by performance
    gold = [t.text for t in doc.sections[1].tiles]
    assert gold == ["鞭挞"]  # 0% pick rate sample excluded
    assert doc.sections[1].tiles[0].cls == "aug aug-gold"
    text = render_text(doc)
    assert "强度 92 · 17%" in text and "冷门强化" not in text

