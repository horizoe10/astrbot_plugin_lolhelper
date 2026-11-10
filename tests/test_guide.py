"""Help menu, sub-menus, per-command help and typo suggestions (lolhelper.guide)."""

import re
from pathlib import Path

from lolhelper import guide

MAIN = (Path(__file__).resolve().parent.parent / "main.py").read_text("utf-8")


def registered() -> list[tuple[str, str]]:
    return re.findall(r'@(lol|tft)\.command\("([^"]+)"', MAIN)


def test_every_command_is_documented():
    missing = [f"{g} {n}" for g, n in registered() if not guide.find_command(n, g)]
    assert not missing, missing
    # and nothing in the guide points to a command that does not exist
    names = {(g, n) for g, n in registered()}
    ghosts = [c.full for _, c in guide.all_commands() if (c.group, c.name) not in names]
    assert not ghosts, ghosts


def test_menu_adapts_to_the_asker():
    group = guide.Ctx(version="2.0.0", account="Faker#KR1（韩服）")
    private = guide.Ctx(version="2.0.0", in_group=False, ai=False)
    text = guide.main_menu(group, bare=True)
    assert text.startswith("👋 需要帮助吗") and "已绑定：Faker#KR1" in text and "5. 🎉 群玩法" in text
    p = guide.main_menu(private)
    assert "群玩法" not in p and "AI 点评" not in p and "先绑定账号" in p
    # numbers follow what was shown: in this private chat 4 is TFT, not AI
    assert guide.resolve("4", private)[1].key == "tft"
    assert guide.resolve("4", group)[1].key == "ai"
    assert guide.resolve("9", group) is None


def test_admin_and_cn_commands_hidden_for_others():
    member = guide.Ctx(cn=True)
    settings = next(c for c in guide.CATEGORIES if c.key == "settings")
    assert "WeGame登录" not in guide.category_text(settings, member)
    assert "WeGame登录" in guide.category_text(settings, guide.Ctx(cn=True, admin=True))
    assert "国服状态" not in guide.category_text(settings, guide.Ctx(cn=False))


def test_resolve_words():
    ctx = guide.Ctx()
    assert guide.resolve("", ctx) == ("main",) and guide.resolve("0", ctx) == ("main",)
    assert guide.resolve("全部", ctx) == ("all",)
    assert guide.resolve("赛事", ctx)[1].key == "esports"
    kind, _, cmd = guide.resolve("竞猜", ctx)
    assert kind == "cmd" and cmd.name == "竞猜"
    assert guide.resolve("日榜", ctx)[2].name == "榜单"  # old command names point to the merged entry
    assert guide.resolve("/tft 英雄", ctx)[2].group == "tft"
    assert guide.resolve("英雄", ctx)[2].group == "lol"


def test_command_help_and_usage():
    ctx = guide.Ctx()
    _, cat, cmd = guide.resolve("赛况", ctx)
    text = guide.command_text(cat, cmd, ctx, ["赛况", "比赛", "match"])
    assert "用法：/lol 赛况 [序号|战队] [局号]" in text and "/lol 赛况 BLG 2" in text and "/lol 比赛" in text and "/lol match" in text
    usage = guide.usage_text("积分")
    assert usage.splitlines() == ["用法：/lol 积分 <联赛>", "示例：/lol 积分 LPL · /lol 积分 LCK", "详细说明：/lol 帮助 积分"]


def test_suggest_prefers_the_same_start():
    words = {"战绩": "战绩", "最近": "战绩", "积分": "积分", "积分榜": "积分", "对局": "对局", "猜英雄榜": "猜英雄榜"}
    assert guide.suggest("战积", words)[0] == "战绩"
    assert guide.suggest("猜英雄榜单", words)[0] == "猜英雄榜"
    assert guide.suggest("哈哈哈", words) == []


def test_buttons_and_card():
    ctx = guide.Ctx(in_group=False)
    rows = guide.button_rows(ctx)
    assert [k for r in rows for k, _ in r] == [c.key for c in guide.visible(ctx)]
    assert all(len(r) <= 5 for r in rows)  # QQ allows 5 buttons per row
    sub = guide.button_rows(ctx, "esports")
    assert sub[0][0][0] == "main" and "esports" not in [k for r in sub for k, _ in r]
    doc = guide.menu_doc(guide.Ctx(version="2.0.0"))
    assert len(doc.sections) == len(guide.CATEGORIES) and doc.theme == "tool"
