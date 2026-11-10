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
    assert text.startswith("👋 需要帮助吗") and "已绑定：Faker#KR1" in text and "⑤ 🎉 群玩法" in text
    assert "　/lol 战绩 · /lol 对局 2" in text and "/lol 战绩 ·" not in guide.main_menu(guide.Ctx(buttons=True))
    p = guide.main_menu(private)
    assert "群玩法" not in p and "AI 点评" not in p and "先绑定" in p
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
    assert [b.data["k"] for r in rows for b in r] == [c.key for c in guide.visible(ctx)]
    assert all(len(r) <= guide.BUTTONS_PER_ROW for r in rows)  # QQ cuts labels when a row is crowded
    assert max(map(len, rows)) - min(map(len, rows)) <= 1
    assert all(len(b.label) <= 5 for r in rows for b in r)
    full = guide.Ctx(cn=True, admin=True)
    for cat in guide.CATEGORIES:  # every category: cards + home row, within QQ's 5 rows, unique ids, short labels
        sub = guide.category_buttons(cat, full)
        ids = [b.id for r in sub for b in r]
        assert len(sub) <= guide.MAX_ROWS and len(ids) == len(set(ids)) and sub[-1][0].id == "lolhelper:menu:main"
        assert all(len(b.label) <= 5 for r in sub for b in r)
    sub = guide.button_rows(ctx, "esports")
    btns = {b.label: b for r in sub for b in r}
    assert btns["赛程"].data == {"c": "/lol 赛程", "e": True, "a": "", "x": ""}
    assert btns["查战队"].data["e"] is False and btns["积分榜"].id == "lolhelper:opt:standings"
    opts = guide.option_buttons("standings")
    assert opts[-1][0].data == {"k": "esports"} and "/lol 积分 LPL" in [b.data.get("c") for r in opts for b in r]
    assert "我的订阅" not in [b.label for r in guide.button_rows(guide.Ctx(push=False), "settings") for b in r]
    assert "按钮" in guide.category_text(guide.CATEGORIES[1], guide.Ctx(buttons=True))
    assert "按钮" not in guide.category_text(guide.CATEGORIES[1], guide.Ctx())
    md = guide.main_menu_md(guide.Ctx(version="2.1.0"), bare=True)
    assert md.startswith("👋 需要帮助吗") and "① 📜 战绩与账号" in md and "**" not in md and "\n\n" not in md
    assert "▸ 赛程　近期赛程\n　/lol 赛程 LPL" in guide.category_md(guide.CATEGORIES[2], guide.Ctx())
    assert "60 秒内" in guide.ask_text("/lol 英雄 ", "英雄名", "亚索")


def test_menus_fit_a_narrow_bubble():
    """QQ shows about 16 Chinese characters per line; menu lines (but the account name) stay within that."""
    texts = []
    for ctx in (guide.Ctx(version="2.1.0", cn=True, admin=True), guide.Ctx(version="2.1.0", buttons=True, cn=True, admin=True)):
        for md in (False, True):
            texts += [guide._main(ctx, bare, md) for bare in (False, True)]
            texts += [guide._cat(c, ctx, md) for c in guide.CATEGORIES]
            texts += [guide.option_text(k, ctx.buttons, md) for k in guide.OPTIONS]
    texts += [guide.ask_text(a.cmd, a.ask, a.ex) for acts in guide.ACTS.values() for a in acts if a.ask]
    long = [ln for t in texts for ln in t.replace("**", "").splitlines() if guide.width(ln) > guide.LINE_WIDTH]
    assert not long, long
    # Markdown menus stay a single paragraph: no blank lines, lists, quotes or headings
    mds = [guide._main(guide.Ctx(buttons=b), bare, True) for b in (False, True) for bare in (False, True)]
    mds += [guide._cat(c, guide.Ctx(buttons=b, cn=True, admin=True), True) for c in guide.CATEGORIES for b in (False, True)]
    mds += [guide.option_md(k, b) for k in guide.OPTIONS for b in (False, True)]
    blocks = [ln for t in mds for ln in t.split("\n") if not ln.strip() or re.match(r"\s*(\d+[.)]\s|[-+*]\s|#|>)", ln)]
    assert not blocks, blocks
    labels = [b for c in guide.CATEGORIES for r in guide.category_buttons(c, guide.Ctx(cn=True)) for b in r]
    for c in guide.CATEGORIES:
        for r in guide.category_buttons(c, guide.Ctx(cn=True))[:-1]:
            assert len(r) <= (3 if all(guide.width(b.label) <= 6 for b in r) else 2), [b.label for b in r]
    assert labels


def test_qq_command_buttons():
    from types import SimpleNamespace as NS

    from lolhelper import qqkb

    rows = guide.category_buttons(guide.CATEGORIES[2], guide.Ctx())
    chain = NS(chain=[NS(buttons=[NS(id=b.id, action=NS(data=b.data)) for b in r]) for r in rows])
    cmds = qqkb.command_map(chain)
    assert cmds["lolhelper:cmd:esports:0"] == ("/lol 赛程", True) and "lolhelper:menu:main" not in cmds
    kb = {"content": {"rows": [{"buttons": [{"id": i, "action": {"type": 1, "data": "x"}}] } for i in
                                ("lolhelper:cmd:esports:0", "lolhelper:cmd:esports:4", "lolhelper:menu:main")]}}
    out = qqkb.rewrite(kb, cmds)["content"]["rows"]
    assert out[0]["buttons"][0]["action"] == {"type": 2, "permission": {"type": 2}, "data": "/lol 赛程", "enter": True,
                                              "reply": False, "unsupport_tips": "请发送 /lol 赛程"}
    assert out[1]["buttons"][0]["action"]["data"] == "/lol 赛况 " and out[1]["buttons"][0]["action"]["enter"] is False
    assert out[2]["buttons"][0]["action"]["type"] == 1

    class Ev:
        @staticmethod
        def _parse_keyboard(chain):
            return {"content": {"rows": [{"buttons": [{"id": "lolhelper:cmd:esports:0", "action": {"type": 1}}]}]}}

    assert qqkb.install(Ev) and qqkb.install(Ev) and qqkb.installed(Ev)  # a reload does not wrap twice
    assert Ev._parse_keyboard(chain)["content"]["rows"][0]["buttons"][0]["action"]["type"] == 2
    qqkb.uninstall(Ev)
    assert not qqkb.installed(Ev) and Ev._parse_keyboard(chain)["content"]["rows"][0]["buttons"][0]["action"]["type"] == 1


    doc = guide.menu_doc(guide.Ctx(version="2.0.0"))
    assert len(doc.sections) == len(guide.CATEGORIES) and doc.theme == "tool"


def test_admin_only_commands_leave_the_menu():
    ctx = guide.Ctx(hidden={"状态", "tft 英雄"})
    settings = next(c for c in guide.CATEGORIES if c.key == "settings")
    tft = next(c for c in guide.CATEGORIES if c.key == "tft")
    assert "▸ 状态" not in guide.category_text(settings, ctx) and "状态" not in [a.label for a in guide.acts(settings, ctx)]
    assert "▸ 英雄" not in guide.category_text(tft, ctx) and "▸ 英雄" in guide.category_text(next(
        c for c in guide.CATEGORIES if c.key == "champ"), ctx)
    assert "名单" not in guide.category_text(settings, guide.Ctx()) and "▸ 名单" in guide.category_text(settings, guide.Ctx(admin=True))
