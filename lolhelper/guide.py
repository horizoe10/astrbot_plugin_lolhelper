"""Command guide: the help menu, per-category sub-menus, per-command help and usage hints.

One table (CATEGORIES) describes every command once; the main menu, sub-menus, "/lol 帮助 <指令>",
missing-argument hints, the full list and the help card are all generated from it, so they cannot drift
apart. The menu adapts to the asker: features that are switched off, group-only commands in a private chat
and admin-only commands for ordinary members are left out.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from . import DISPLAY_NAME
from .doc import Cell, Doc


@dataclass(frozen=True)
class Cmd:
    name: str  # sub-command word, e.g. "战绩"
    usage: str  # arguments, e.g. "[名字#标签] [大区]"
    desc: str
    examples: tuple[tuple[str, str], ...] = ()  # (arguments, note)
    related: tuple[str, ...] = ()  # full commands, e.g. "/lol 对局"
    group: str = "lol"
    needs: frozenset = frozenset()  # ai | group | cn | admin | push | group_chat
    note: str = ""

    @property
    def full(self) -> str:
        return f"/{self.group} {self.name}"

    def line(self) -> str:
        return f"{self.full} {self.usage}".rstrip()

    def example(self, i: int = 0) -> str:
        if not self.examples:
            return self.full
        return f"{self.full} {self.examples[i][0]}".rstrip()


@dataclass(frozen=True)
class Cat:
    key: str
    emoji: str
    title: str
    words: tuple[str, ...]  # names accepted by "/lol 帮助 <分类>"
    quick: tuple[str, ...]  # shown on the main menu
    cmds: tuple[Cmd, ...]
    needs: frozenset = frozenset()
    button: str = ""  # short button label

    @property
    def label(self) -> str:
        return f"{self.emoji} {self.title}"


def _f(*xs: str) -> frozenset:
    return frozenset(xs)


CATEGORIES: tuple[Cat, ...] = (
    Cat("account", "📜", "战绩与账号", ("战绩与账号", "账号相关", "账号与战绩", "战绩账号"),
        ("/lol 战绩", "/lol 对局 2", "/lol 绑定 名字#标签"), (
            Cmd("战绩", "[名字#标签|@群友] [大区]", "最近对局：KDA、出装、双方 10 人",
                (("", "查自己（需先绑定）"), ("@群友", "查已绑定的群友"), ("Hide on bush#KR1 kr", "")), ("/lol 对局", "/lol 资料")),
            Cmd("对局", "[序号] [名字#标签]", "单局详情：双方 10 人的伤害、经济、出装",
                (("", "最近一局"), ("2", "最近第 2 局"), ("2 我", "看过别人的战绩后，查自己的第 2 局")), ("/lol 战绩", "/lol 点评"),
                note="不写名字时，序号指你刚查过战绩的那个人（30 分钟内）；加「我」查自己"),
            Cmd("资料", "[名字#标签|@群友] [大区]", "段位与常用英雄", (("", ""), ("@群友", "")), ("/lol 战绩",)),
            Cmd("绑定", "名字#标签 [大区]", "绑定外服账号，之后查询不用再写名字",
                (("Hide on bush#KR1 kr", ""),), ("/lol 账号", "/lol 绑定国服"), note="外服默认大区可在插件配置里改"),
            Cmd("绑定国服", "名字#编号 [大区]", "绑定国服账号", (("名字#12345 艾欧尼亚", ""),), ("/lol 国服状态",), needs=_f("cn")),
            Cmd("账号", "", "查看已绑定的账号", (), ("/lol 切换", "/lol 解绑")),
            Cmd("切换", "<序号>", "切换默认账号", (("2", "序号见 /lol 账号"),), ("/lol 账号",)),
            Cmd("解绑", "[序号]", "解除绑定", (("2", ""),), ("/lol 账号",)),
            Cmd("大区", "", "查看大区的写法", (), ("/lol 绑定",)),
        ), button="📜战绩"),
    Cat("champ", "🛡️", "英雄与版本", ("英雄与版本", "版本", "英雄相关"),
        ("/lol 英雄 亚索", "/lol 梯队 中单", "/lol 今天玩什么"), (
            Cmd("英雄", "<英雄|梯队序号> [位置] [模式] [地区] [段位]", "出装、符文、加点和克制",
                (("亚索 中单", ""), ("3", "看完梯队后查第 3 名"), ("盲僧 打野 韩服 钻石+", "")), ("/lol 梯队", "/lol 海克斯")),
            Cmd("海克斯", "<英雄>", "海克斯大乱斗：出装与强化推荐", (("亚索", ""),), ("/lol 英雄",)),
            Cmd("梯队", "[位置] [地区] [段位]", "版本强势英雄排行", (("", "全部分路"), ("打野 韩服 大师+", "")), ("/lol 英雄 3",)),
            Cmd("今天玩什么", "[位置]", "从当前版本 T1 / T2 英雄里随机抽一个", (("中单", ""),), ("/lol 梯队",)),
        ), button="🛡️英雄"),
    Cat("esports", "🏆", "赛事", ("赛事", "电竞", "比赛"),
        ("/lol 赛程", "/lol 赛况 BLG", "/lol 积分 LPL"), (
            Cmd("赛程", "[联赛…]", "近期赛程，可写多个联赛合并显示",
                (("", "按本会话订阅的赛事"), ("S赛 LPL", ""), ("全部", "")), ("/lol 赛果", "/lol 订阅 赛事")),
            Cmd("赛果", "[联赛…]", "近期赛果，带序号可查赛况", (("LPL", ""),), ("/lol 赛况 1",)),
            Cmd("赛况", "[序号|战队] [局号]", "每局阵容、击杀、资源和 MVP；加局号看单局 10 人",
                (("1", "赛果卡片里的第 1 场"), ("BLG", ""), ("BLG 2", "第 2 局")), ("/lol 赛况点评",)),
            Cmd("积分", "<联赛>", "联赛积分榜", (("LPL", ""), ("LCK", "")), ("/lol 赛程",)),
        ), button="🏆赛事"),
    Cat("ai", "🤖", "AI 点评", ("ai点评", "ai", "点评相关"),
        ("/lol 点评", "/lol 诊断", "/lol 赛况点评 BLG"), (
            Cmd("点评", "[序号] [名字#标签] [专业|幽默|锐评]", "AI 点评单局；/lol 锐评 直接用锐评风格",
                (("", "最近一局"), ("2 幽默", "")), ("/lol 诊断",),
                note="不写名字时，序号指你刚查过战绩的那个人（30 分钟内）；加「我」点评自己"),
            Cmd("诊断", "[名字#标签] [局数] [风格]", "近期战绩诊断：整体与最近 5 局对比", (("", ""), ("10", "最近 10 局")), ("/lol 点评",)),
            Cmd("赛况点评", "[战队] [局号] [风格]", "AI 点评比赛单局，不写局号点评整场", (("BLG 3", ""), ("BLG", "整场")), ("/lol 赛况",)),
        ), needs=_f("ai"), button="🤖AI"),
    Cat("group", "🎉", "群玩法", ("群玩法", "群", "群聊"),
        ("/lol 榜单", "/lol 竞猜", "/lol 猜英雄", "/lol 对比 @群友"), (
            Cmd("榜单", "[日|周|竞猜|猜英雄] [昨天|上周]", "群榜单，不写默认今天的日榜",
                (("", "今天的日榜"), ("周 上周", ""), ("竞猜", ""), ("猜英雄", "")), ("/lol 下榜",),
                note="也可以直接发 /lol 日榜、/lol 周榜、/lol 竞猜榜、/lol 猜英雄榜"),
            Cmd("对比", "@甲 [@乙]", "近期数据 PK，AI 当裁判；只 @ 一个人时和自己比", (("@群友", ""),), ("/lol 诊断",)),
            Cmd("竞猜", "[序号|战队] [比分]", "赛事竞猜，赛后自动结算", (("", "可竞猜的比赛"), ("BLG 3:1", "")), ("/lol 榜单 竞猜",)),
            Cmd("猜英雄", "[技能|原画|技能名]", "猜英雄小游戏，直接发英雄名抢答", (("原画", ""),), ("/lol 不猜了", "/lol 榜单 猜英雄")),
            Cmd("不猜了", "", "结束本题并公布答案"),
            Cmd("分队", "@甲 @乙 …", "4~10 人按段位平衡内战分队", (("@甲 @乙 @丙 @丁", ""),)),
            Cmd("群播报", "[开|关]", "群友五杀、开黑、连胜连败时自动播报", (("关", ""),)),
            Cmd("上榜", "", "加入本群榜单"),
            Cmd("下榜", "", "退出本群榜单"),
            Cmd("群成员", "", "查看本群上榜的群友"),
        ), needs=_f("group", "group_chat"), button="🎉群聊"),
    Cat("tft", "♟️", "云顶之弈", ("云顶之弈", "云顶", "tft"),
        ("/tft 阵容", "/tft 装备 大天使"), (
            Cmd("阵容", "[序号]", "版本强势阵容，加序号看阵容详情", (("", ""), ("1", "第 1 名阵容")), ("/tft 英雄",), group="tft"),
            Cmd("英雄", "<名称>", "云顶英雄出装", (("凯特琳", ""),), ("/tft 装备",), group="tft"),
            Cmd("装备", "<名称>", "装备适合谁带", (("大天使", ""),), ("/tft 英雄",), group="tft"),
            Cmd("强化", "<关键词>", "强化符文搜索", (("贷款", ""),), ("/tft 阵容",), group="tft"),
        ), button="♟️云顶"),
    Cat("settings", "🔔", "订阅与设置", ("订阅与设置", "设置", "订阅相关"),
        ("/lol 订阅 赛事", "/lol 模式 图片"), (
            Cmd("订阅", "战绩|赛事 [联赛…]|梯队|云顶|列表|取消 <ID>", "订阅推送，也可以查看和取消订阅",
                (("战绩", "有新对局时推送"), ("赛事 S赛 LPL", ""), ("列表", ""), ("取消 3", "")), ("/lol 模式",),
                needs=_f("push"), note="旧写法 /lol 订阅列表、/lol 退订 仍然可用"),
            Cmd("模式", "[文本|md|图片|默认]", "本会话的默认输出方式；单次查询可加 -t / -m / -i", (("图片", ""),)),
            Cmd("国服状态", "", "国服连接和 WeGame 凭证状态", (), ("/lol 绑定国服",), needs=_f("cn")),
            Cmd("WeGame登录", "[QQ|微信]", "管理员扫码登录 WeGame，自动保存国服凭证", (("微信", ""),), needs=_f("cn", "admin")),
            Cmd("状态", "", "插件版本与运行状态"),
            Cmd("刷新", "", "刷新英雄和云顶数据", needs=_f("admin")),
            Cmd("名单", "[拉黑|解除|允许|移出 <@群友|用户ID|本群>] [白名单 开|关] [限管理员 <指令>]",
                "访问控制：黑名单、白名单和仅限管理员的指令",
                (("", "查看当前设置"), ("拉黑 @群友", ""), ("拉黑 本群", ""), ("白名单 开", ""), ("允许 群123456", "群号前加「群」"),
                 ("限管理员 订阅", "普通用户使用时提示仅限管理员；再发一次取消"), ("限管理员 全部 开", "整个插件只有管理员能用")),
                needs=_f("admin"), note="AstrBot 管理员不受这些限制；也可以在插件配置「访问控制」里修改"),
            Cmd("帮助", "[分类|指令|全部]", "帮助菜单", (("赛事", ""), ("竞猜", ""), ("全部", ""))),
        ), button="🔔设置"),
)

# old or alternative command names -> the entry that documents them
HELP_ALIASES = {"日榜": "榜单", "周榜": "榜单", "竞猜榜": "榜单", "猜英雄榜": "榜单", "订阅列表": "订阅", "退订": "订阅",
                "锐评": "点评", "出装": "英雄", "help": "帮助",
                "黑名单": "名单", "白名单": "名单", "权限": "名单", "访问控制": "名单"}
ALL_WORDS = {"全部", "all", "完整", "全部指令"}
BACK_WORDS = {"0", "返回", "主菜单", "菜单"}
REPLY_SECS = 60


@dataclass
class Ctx:
    version: str = ""
    account: str = ""  # label of the default bound account, "" when none
    in_group: bool = True
    ai: bool = True
    group_play: bool = True
    cn: bool = False
    admin: bool = False
    push: bool = True
    buttons: bool = False
    hidden: set[str] = field(default_factory=set)

    def has(self, need: str) -> bool:
        return {"ai": self.ai, "group": self.group_play, "cn": self.cn, "admin": self.admin, "push": self.push,
                "group_chat": self.in_group}.get(need, True)

    def ok(self, needs: frozenset) -> bool:
        return all(self.has(n) for n in needs)


def visible(ctx: Ctx) -> list[Cat]:
    return [c for c in CATEGORIES if ctx.ok(c.needs)]


def hidden(ctx: Ctx | None, group: str, name: str) -> bool:
    """Admin-only commands (ctx.hidden, written "订阅" or "tft 英雄") are left out of menus for other users."""
    return bool(ctx and ctx.hidden) and (name in ctx.hidden or f"{group} {name}" in ctx.hidden)


def commands(cat: Cat, ctx: Ctx | None = None) -> list[Cmd]:
    return [c for c in cat.cmds if ctx is None or (ctx.ok(c.needs) and not hidden(ctx, c.group, c.name))]


def all_commands() -> list[tuple[Cat, Cmd]]:
    return [(cat, c) for cat in CATEGORIES for c in cat.cmds]


def find_command(name: str, group: str = "lol") -> tuple[Cat, Cmd] | None:
    name = HELP_ALIASES.get(name, name).lower()
    hits = [(cat, c) for cat, c in all_commands() if c.name.lower() == name]
    return next((x for x in hits if x[1].group == group), hits[0] if hits else None)


def resolve(word: str, ctx: Ctx, group: str = "lol"):
    """What "/lol 帮助 <word>" asks for: ("main",) | ("all",) | ("cat", Cat) | ("cmd", Cat, Cmd) | None."""
    w = (word or "").strip().lstrip("/").strip()
    for g in ("lol ", "tft ", "云顶 "):
        if w.lower().startswith(g):
            group, w = ("tft" if g != "lol " else "lol"), w[len(g):].strip()
    low = w.lower()
    if not w or low in BACK_WORDS:
        return ("main",)
    if low in ALL_WORDS:
        return ("all",)
    cats = visible(ctx)
    if w.isdigit():
        n = int(w)
        return ("cat", cats[n - 1]) if 1 <= n <= len(cats) else None
    for cat in CATEGORIES:
        if low in (x.lower() for x in cat.words) or low == cat.title.lower() or low == cat.key:
            return ("cat", cat)
    hit = find_command(w, group)
    if hit:
        return ("cmd", hit[0], hit[1])
    return None


def suggest(word: str, names: dict[str, str] | list[str], n: int = 3) -> list[str]:
    """Commands close to a mistyped word. names maps every spelling (names and aliases) to its command."""
    if not isinstance(names, dict):
        names = {x: x for x in names}
    w = (word or "").lower()
    if not w:
        return []
    scored = []
    for k, v in names.items():
        kl = str(k).lower()
        if not kl:
            continue
        r = difflib.SequenceMatcher(None, w, kl).ratio()
        if kl[0] == w[0]:
            r += 0.25
        if len(w) >= 2 and len(kl) >= 2 and (w in kl or kl in w):
            r += 0.3
        if r >= 0.6:
            scored.append((r, v))
    out: list[str] = []
    for _, v in sorted(scored, key=lambda x: -x[0]):
        if v not in out:
            out.append(v)
    return out[:n]


# ---------------------------------------------------------------- text
# Menus are laid out for a narrow chat bubble (QQ shows about 16 Chinese characters per line): one short line per
# command plus an example line. The Markdown versions use the same layout: no bold (QQ swallows the space after it),
# no blank lines and no block syntax such as "1." lists.
LINE_WIDTH = 30  # display width, a Chinese character counts 2; QQ wraps at about 31

# (group name) -> (short description, short example)
SHORT: dict[str, tuple[str, str]] = {
    "lol 战绩": ("最近对局与出装", "/lol 战绩 @群友"), "lol 对局": ("单局 10 人详情", "/lol 对局 2"),
    "lol 资料": ("段位与常用英雄", "/lol 资料 @群友"), "lol 绑定": ("绑定外服账号", "/lol 绑定 名字#标签"),
    "lol 绑定国服": ("绑定国服账号", "/lol 绑定国服 名字#编号"), "lol 账号": ("已绑定的账号", ""),
    "lol 切换": ("切换默认账号", "/lol 切换 2"), "lol 解绑": ("解除绑定", "/lol 解绑 2"), "lol 大区": ("大区写法", ""),
    "lol 英雄": ("出装、符文、克制", "/lol 英雄 亚索 中单"), "lol 海克斯": ("大乱斗出装强化", "/lol 海克斯 亚索"),
    "lol 梯队": ("版本强势排行", "/lol 梯队 打野"), "lol 今天玩什么": ("随机强势英雄", "/lol 今天玩什么 中单"),
    "lol 赛程": ("近期赛程", "/lol 赛程 LPL"), "lol 赛果": ("近期赛果", "/lol 赛果 LPL"),
    "lol 赛况": ("每局数据与 MVP", "/lol 赛况 BLG 2"), "lol 积分": ("联赛积分榜", "/lol 积分 LPL"),
    "lol 点评": ("AI 点评单局", "/lol 点评 2 幽默"), "lol 诊断": ("近期战绩诊断", "/lol 诊断 10"),
    "lol 赛况点评": ("AI 点评比赛", "/lol 赛况点评 BLG"),
    "lol 榜单": ("群日榜、周榜", "/lol 榜单 周"), "lol 对比": ("数据 PK", "/lol 对比 @群友"),
    "lol 竞猜": ("赛事竞猜", "/lol 竞猜 BLG 3:1"), "lol 猜英雄": ("猜英雄小游戏", "/lol 猜英雄 原画"),
    "lol 不猜了": ("公布答案", ""), "lol 分队": ("内战平衡分队", "/lol 分队 @甲 @乙…"),
    "lol 群播报": ("五杀、连胜播报", "/lol 群播报 关"), "lol 上榜": ("加入本群榜单", ""),
    "lol 下榜": ("退出本群榜单", ""), "lol 群成员": ("上榜的群友", ""),
    "tft 阵容": ("版本强势阵容", "/tft 阵容 1"), "tft 英雄": ("云顶英雄出装", "/tft 英雄 凯特琳"),
    "tft 装备": ("装备适合谁带", "/tft 装备 大天使"), "tft 强化": ("强化符文搜索", "/tft 强化 贷款"),
    "lol 订阅": ("推送订阅", "/lol 订阅 赛事 LPL"), "lol 模式": ("本会话输出方式", "/lol 模式 图片"),
    "lol 国服状态": ("国服连接状态", ""), "lol WeGame登录": ("扫码登录", ""),
    "lol 状态": ("插件运行状态", ""), "lol 名单": ("黑白名单与权限", "/lol 名单 拉黑 @群友"), "lol 刷新": ("刷新游戏数据", ""), "lol 帮助": ("帮助菜单", "/lol 帮助 竞猜"),
}


def width(s: str) -> int:
    """Display width: ASCII 1, Chinese characters and emoji 2, variation selectors and joiners 0."""
    w = 0
    for ch in s:
        o = ord(ch)
        if ch in "\ufe0f\u200d" or 0x300 <= o < 0x370:
            continue
        w += 1 if o < 0x1100 else 2
    return w


def brief(c: Cmd) -> tuple[str, str]:
    desc, ex = SHORT.get(f"{c.group} {c.name}", (c.desc, c.example()))
    return desc, ("" if ex == c.full else ex)


RULE = "─" * 14
NUMS = "①②③④⑤⑥⑦⑧⑨⑩"


def _main(ctx: Ctx, bare: bool, md: bool = False) -> str:
    """The main menu. Plain text and Markdown share one layout without bold: QQ swallows the space after bold text."""
    cats = visible(ctx)
    ver = f" v{ctx.version}" if ctx.version else ""
    lines = ["👋 需要帮助吗？这是主菜单" if bare else f"📖 帮助菜单 · {DISPLAY_NAME}"]
    lines.append(f"已绑定：{ctx.account}" if ctx.account else "👉 先绑定：/lol 绑定 名字#标签")
    lines.append(RULE)
    for i, cat in enumerate(cats, 1):
        lines.append(f"{NUMS[i - 1] if i <= len(NUMS) else i} {cat.emoji} {cat.title}")
        if not ctx.buttons:
            quick = ""
            for q in cat.quick:
                cand = f"{quick} · {q}" if quick else q
                if width("　" + cand) <= LINE_WIDTH:
                    quick = cand
            lines.append("　" + quick)
    lines.append(RULE)
    lines.append("💡 也可直接发 /lol 亚索")
    lines.append("👇 点按钮或回复序号进入分类" if ctx.buttons else f"↩ 回复序号进入分类（{REPLY_SECS} 秒内）")
    lines.append("📖 全部指令 /lol 帮助 全部")
    return "\n".join(lines)


def main_menu(ctx: Ctx, bare: bool = False) -> str:
    return _main(ctx, bare)


def main_menu_md(ctx: Ctx, bare: bool = False) -> str:
    """Markdown version of the main menu (same layout; nothing in it is Markdown syntax)."""
    return _main(ctx, bare, True)


def _cat(cat: Cat, ctx: Ctx, md: bool = False) -> str:
    """A category sub-menu: one line per command (name and what it does) and an indented example below."""
    cats = visible(ctx)
    pos = next((i for i, c in enumerate(cats, 1) if c.key == cat.key), 0)
    lines = [f"{cat.emoji} {cat.title}" + (f" · {pos}/{len(cats)}" if pos else "")]
    if not ctx.ok(cat.needs):
        lines.append(_unavailable(cat.needs, ctx))
    lines.append(RULE)
    for c in commands(cat, ctx) or list(cat.cmds):
        desc, ex = brief(c)
        lines.append(f"▸ {c.name}　{desc}")
        lines.append(f"　{ex or c.full}")
    lines.append(RULE)
    if ctx.buttons and acts(cat, ctx):
        lines.append("👇 点按钮直接查看")
        asks = [a.label for a in acts(cat, ctx) if a.ask]
        if asks:
            tip = f"✏️ {'、'.join(asks)} 点了再输入"
            lines.append(tip if width(tip) <= LINE_WIDTH else f"✏️ {asks[0]}等 点了再输入")
    lines.append("📖 详解 /lol 帮助 <指令>")
    lines.append("↩ 回复 0 返回 · 序号切换分类")
    return "\n".join(lines)


def category_text(cat: Cat, ctx: Ctx) -> str:
    return _cat(cat, ctx)


def category_md(cat: Cat, ctx: Ctx) -> str:
    """Markdown version of a category sub-menu (same layout)."""
    return _cat(cat, ctx, True)


def command_text(cat: Cat, cmd: Cmd, ctx: Ctx, aliases: list[str] | None = None) -> str:
    lines = [f"📖 {cmd.full} · {cmd.desc}", f"用法：{cmd.line()}"]
    if not ctx.ok(cmd.needs | cat.needs):
        lines.append(_unavailable(cmd.needs | cat.needs, ctx))
    if cmd.examples:
        lines.append("示例：")
        for a, note in cmd.examples:
            ex = f"{cmd.full} {a}".rstrip()
            lines.append(f"  {ex}" + (f"    {note}" if note else ""))
    if cmd.note:
        lines.append(cmd.note)
    al = [a for a in (aliases or []) if a != cmd.name]
    if al:
        lines.append("别名：" + "、".join(f"/{cmd.group} {a}" for a in al))
    if cmd.related:
        lines.append("相关：" + " · ".join(cmd.related))
    if cmd.name not in ("帮助", "模式", "绑定", "绑定国服", "切换", "解绑", "上榜", "下榜", "群播报", "不猜了", "刷新"):
        lines.append("任意查询可加 -t / -m / -i，以文本 / Markdown / 图片发送")
    cats = visible(ctx)
    pos = next((i for i, c in enumerate(cats, 1) if c.key == cat.key), 0)
    lines.append(f"所属分类：{cat.label}" + (f"（/lol 帮助 {pos}）" if pos else ""))
    return "\n".join(lines)


def usage_text(name: str, group: str = "lol", extra: str = "") -> str:
    """Hint for a command used without the arguments it needs: usage, examples and where to read more."""
    hit = find_command(name, group)
    if not hit:
        return f"用法有误，发送 /lol 帮助 查看指令{extra}"
    _, c = hit
    lines = [f"用法：{c.line()}"]
    if c.examples:
        lines.append("示例：" + " · ".join(c.example(i) for i in range(min(2, len(c.examples)))))
    if extra:
        lines.append(extra)
    lines.append(f"详细说明：/lol 帮助 {c.name}")
    return "\n".join(lines)


def full_text(ctx: Ctx) -> str:
    lines = [f"{DISPLAY_NAME} v{ctx.version} · 全部指令" if ctx.version else f"{DISPLAY_NAME} · 全部指令"]
    for cat in visible(ctx):
        lines.append("")
        lines.append(cat.label)
        lines += [f"  {c.line()}  {c.desc}" for c in commands(cat, ctx)]
    lines.append("")
    lines.append("旧写法 /lol 日榜、周榜、竞猜榜、猜英雄榜、订阅列表、退订 仍然可用")
    lines.append("任意查询可加 -t / -m / -i，以文本 / Markdown / 图片发送")
    return "\n".join(lines)


def suggestion_text(word: str, hits: list[str], group: str = "lol") -> str:
    if hits:
        return f"没有「{word}」这个指令，你是不是想找：" + " · ".join(f"/{group} {h}" for h in hits) + "\n发送 /lol 帮助 查看主菜单"
    return f"没有「{word}」这个指令，也没认出是英雄、玩家、战队或联赛。发送 /lol 帮助 查看主菜单"


def _unavailable(needs: frozenset, ctx: Ctx) -> str:
    if not ctx.has("group_chat") and "group_chat" in needs:
        return "（需要在群聊里使用）"
    if not ctx.has("ai") and "ai" in needs:
        return "（AI 点评未开启，可在插件配置「AI 点评」里开启）"
    if not ctx.has("group") and "group" in needs:
        return "（群玩法未开启，可在插件配置「群玩法」里开启）"
    if not ctx.has("cn") and "cn" in needs:
        return "（国服战绩查询未开启，可在插件配置「国服」里开启）"
    if not ctx.has("admin") and "admin" in needs:
        return "（仅管理员可用）"
    if not ctx.has("push") and "push" in needs:
        return "（推送功能未开启）"
    return ""


# ---------------------------------------------------------------- card
def menu_doc(ctx: Ctx) -> Doc:
    """The full command list as an image card (/lol 帮助 -i)."""
    doc = Doc(title="指令菜单", subtitle=f"{DISPLAY_NAME} v{ctx.version}" if ctx.version else DISPLAY_NAME, theme="tool", width=760)
    doc.hero = {"layout": "center", "eyebrow": "COMMAND MENU", "range": doc.subtitle,
                "chips": [{"text": f"已绑定 {ctx.account}" if ctx.account else "未绑定账号"}]}
    for i, cat in enumerate(visible(ctx), 1):
        rows = [[Cell(c.line(), cls="hi"), c.desc] for c in commands(cat, ctx)]
        doc.add_table(f"{i} · {cat.title}", ["指令", "说明"], rows, widths=["50%", "50%"])
    doc.footer = "任意查询可加 -t / -m / -i 以文本 / Markdown / 图片发送"
    doc.tip("/lol 帮助 竞猜", "单个指令的详细用法")
    doc.tip("/lol 亚索", "直接写英雄、玩家、战队或联赛")
    doc.tip("/lol 帮助 全部", "文字版全部指令")
    return doc


# ---------------------------------------------------------------- buttons
BUTTONS_PER_ROW = 3
MAX_ROWS = 5  # QQ keyboards hold at most 5 rows


@dataclass(frozen=True)
class Act:
    """A card button in a category sub-menu: sends cmd as is, asks for the rest (ask), or opens an option list."""
    label: str
    cmd: str = ""
    ask: str = ""  # what the user still has to type, e.g. "英雄名"
    ex: str = ""  # an example of it
    opts: str = ""  # key in OPTIONS
    needs: frozenset = frozenset()


@dataclass(frozen=True)
class Opts:
    title: str
    items: tuple[tuple[str, str], ...]  # (label, command)
    cat: str  # category the "返回" button goes back to


@dataclass(frozen=True)
class Btn:
    id: str
    label: str
    data: dict
    primary: bool = False


ACTS: dict[str, tuple[Act, ...]] = {
    "account": (Act("战绩", "/lol 战绩"), Act("上一局", "/lol 对局"), Act("资料", "/lol 资料"),
                Act("查别人", "/lol 战绩 ", "名字#标签 [大区]", "Hide on bush#KR1 kr"),
                Act("绑定", "/lol 绑定 ", "名字#标签 [大区]", "Hide on bush#KR1 kr"), Act("账号", "/lol 账号")),
    "champ": (Act("查英雄", "/lol 英雄 ", "英雄名，可加位置", "亚索 中单"), Act("梯队", "/lol 梯队"),
              Act("分路", opts="tier"), Act("随机", "/lol 今天玩什么"), Act("大乱斗", "/lol 海克斯 ", "英雄名", "亚索")),
    "esports": (Act("赛程", "/lol 赛程"), Act("赛果", "/lol 赛果"), Act("赛况", "/lol 赛况"),
                Act("积分榜", opts="standings"), Act("查战队", "/lol 赛况 ", "战队名", "BLG")),
    "ai": (Act("点评", "/lol 点评"), Act("锐评", "/lol 锐评"), Act("诊断", "/lol 诊断"), Act("赛况点评", "/lol 赛况点评")),
    "group": (Act("今日榜", "/lol 榜单"), Act("周榜", "/lol 榜单 周"), Act("竞猜", "/lol 竞猜"),
              Act("竞猜榜", "/lol 榜单 竞猜"), Act("猜英雄", "/lol 猜英雄"), Act("对比", "/lol 对比 ", "@ 要比的群友"),
              Act("上榜", "/lol 上榜")),
    "tft": (Act("阵容", "/tft 阵容"), Act("查英雄", "/tft 英雄 ", "英雄名", "凯特琳"),
            Act("查装备", "/tft 装备 ", "装备名", "大天使"), Act("搜强化", "/tft 强化 ", "关键词", "贷款")),
    "settings": (Act("订阅", "/lol 订阅 列表", needs=_f("push")), Act("模式", opts="mode"),
                 Act("国服", "/lol 国服状态", needs=_f("cn")), Act("状态", "/lol 状态"), Act("全部", "/lol 帮助 全部"),
                 Act("名单", "/lol 名单", needs=_f("admin"))),
}
OPTIONS: dict[str, Opts] = {
    "tier": Opts("🛡️ 分路梯队 · 选位置", tuple((p, f"/lol 梯队 {p}") for p in ("上单", "打野", "中单", "下路", "辅助")), "champ"),
    "standings": Opts("🏆 积分榜 · 选联赛",
                      tuple((x, f"/lol 积分 {x}") for x in ("LPL", "LCK", "LEC", "LCP", "LTA", "S赛")), "esports"),
    "mode": Opts("🔔 本会话输出方式", (("图片", "/lol 模式 图片"), ("Markdown", "/lol 模式 md"), ("文本", "/lol 模式 文本"),
                                ("默认", "/lol 模式 默认")), "settings"),
}


def cmd_data(cmd: str, ask: str = "", ex: str = "") -> dict:
    """Callback data of a command button: c = command, e = send at once, a / x = what to ask for otherwise."""
    return {"c": cmd, "e": not ask, "a": ask, "x": ex}


def _spread(items: list[Btn]) -> list[list[Btn]]:
    """QQ splits a row evenly between its buttons and cuts long labels to "…": three buttons fit labels up to three
    Chinese characters wide, longer labels get two per row. Items are spread evenly over the rows (7 -> 3/2/2)."""
    if not items:
        return []
    per = BUTTONS_PER_ROW if all(width(b.label) <= 6 for b in items) else 2
    n_rows = -(-len(items) // per)
    base, extra = divmod(len(items), n_rows)
    rows, i = [], 0
    for r in range(n_rows):
        size = base + (1 if r < extra else 0)
        rows.append(items[i:i + size])
        i += size
    return rows


def _home() -> Btn:
    return Btn("lolhelper:menu:main", "🏠 主页", {"k": "main"}, True)


def acts(cat: Cat, ctx: Ctx) -> list[Act]:
    out = []
    for a in ACTS.get(cat.key, ()):
        words = a.cmd.lstrip("/").split()
        if ctx.ok(a.needs) and not (len(words) > 1 and hidden(ctx, words[0], words[1])):
            out.append(a)
    return out


def main_buttons(ctx: Ctx) -> list[list[Btn]]:
    return _spread([Btn(f"lolhelper:menu:{c.key}", c.button or c.label, {"k": c.key}) for c in visible(ctx)])


def category_buttons(cat: Cat, ctx: Ctx) -> list[list[Btn]]:
    """The cards of a category, one button each, and a row back to the main menu."""
    items = []
    for i, a in enumerate(acts(cat, ctx)):
        if a.opts:
            items.append(Btn(f"lolhelper:opt:{a.opts}", a.label, {"o": a.opts}))
        else:
            items.append(Btn(f"lolhelper:cmd:{cat.key}:{i}", a.label, cmd_data(a.cmd, a.ask, a.ex)))
    return _spread(items)[:MAX_ROWS - 1] + [[_home()]]


def option_buttons(key: str) -> list[list[Btn]]:
    o = OPTIONS[key]
    items = [Btn(f"lolhelper:cmd:{key}:{i}", label, cmd_data(cmd)) for i, (label, cmd) in enumerate(o.items)]
    return _spread(items)[:MAX_ROWS - 1] + [[Btn(f"lolhelper:menu:{o.cat}", "↩ 返回", {"k": o.cat}, True), _home()]]


def button_rows(ctx: Ctx, current: str | None = None) -> list[list[Btn]]:
    """Buttons under the main menu (current=None) or under a category sub-menu."""
    cat = next((c for c in CATEGORIES if c.key == current), None) if current else None
    return category_buttons(cat, ctx) if cat else main_buttons(ctx)


def option_text(key: str, buttons: bool = True, md: bool = False) -> str:
    o = OPTIONS[key]
    lines = [o.title, RULE]
    if buttons:
        lines.append("👇 点下方按钮选择")
    else:
        lines += [f"▸ {label}　{cmd}" for label, cmd in o.items]
    return "\n".join(lines)


def option_md(key: str, buttons: bool = True) -> str:
    return option_text(key, buttons, True)


def ask_text(cmd: str, ask: str, ex: str = "") -> str:
    lines = [f"✏️ 请发送{ask}"]
    if ex:
        lines.append(f"例如：{ex}")
    lines.append(f"直接发内容，不用写 {cmd.strip()}")
    lines.append(f"{REPLY_SECS} 秒内有效，发其他指令取消")
    return "\n".join(lines)
