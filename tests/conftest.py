import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lolhelper.gamedata import GameData  # noqa: E402
from lolhelper.net import HttpClient  # noqa: E402


@pytest.fixture
def gd(tmp_path):
    g = GameData(HttpClient(), tmp_path)
    g.lol = {
        "fetched_at": 9e12,
        "version": "16.22.1",
        "champions": {
            "157": {"id": 157, "key": "Yasuo", "name_zh": "疾风剑豪", "title_zh": "亚索", "name_en": "Yasuo"},
            "238": {"id": 238, "key": "Zed", "name_zh": "影流之主", "title_zh": "劫", "name_en": "Zed"},
            "62": {"id": 62, "key": "MonkeyKing", "name_zh": "齐天大圣", "title_zh": "孙悟空", "name_en": "Wukong"},
            "777": {"id": 777, "key": "Yone", "name_zh": "封魔剑魂", "title_zh": "永恩", "name_en": "Yone"},
        },
        "items": {"3031": "无尽之刃", "6673": "不朽盾弓"},
        "spells": {"4": "闪现", "14": "引燃"},
        "spell_icons": {"4": "SummonerFlash.png", "14": "SummonerDot.png"},
        "rune_icons": {"8005": "perk-images/Styles/Precision/PressTheAttack/PressTheAttack.png", "8100": "perk-images/Styles/7200_Domination.png"},
        "rune_names": {"8005": "强攻", "8100": "主宰"},
        "schema": 3,
    }
    g._index_lol()
    g.tft = {
        "fetched_at": 9e12,
        "set": 18,
        "items": {
            "DA_ArchangelsStaff": {"name": "大天使之杖", "icon": None, "from": ["DA_NeedlesslyLargeRod", "DA_TearOfTheGoddess"]},
            "DA_ArchangelsStaffRadiant": {"name": "光明版大天使之杖", "icon": None, "from": []},
            "DA_NeedlesslyLargeRod": {"name": "无用大棒", "icon": None, "from": []},
            "DA_TearOfTheGoddess": {"name": "女神之泪", "icon": None, "from": []},
        },
        "champions": {"DA_18_Caitlyn": {"name": "凯特琳", "cost": 1, "traits": ["日蚀骑士"], "icon": None}},
        "traits": {"DA_18_Solar": "日蚀骑士"},
        "trait_icons": {"DA_18_Solar": "https://raw.communitydragon.org/latest/game/assets/ux/traiticons/trait_icon_18_solar.png"},
        "schema": 3,
    }
    g._index_tft()
    return g

