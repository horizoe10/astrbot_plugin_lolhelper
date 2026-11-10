"""Command guide: the help menu, per-category sub-menus, per-command help and usage hints.

One table (CATEGORIES) describes every command once; the main menu, sub-menus, "/lol 帮助 <指令>",
missing-argument hints, the full list and the help card are all generated from it, so they cannot drift
apart. The menu adapts to the asker: features that are switched off, group-only commands in a private chat
and admin-only commands for ordinary members are left out.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

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
                (("", "最近一局"), ("2", "最近第 2 局")), ("/lol 战绩", "/lol 点评")),
            Cmd("资料", "[名字#标签|@群友] [大区]", "段位与常用英雄", (("", ""), ("@群友", "")), ("/lol 战绩",)),
            Cmd("绑定", "名字#标签 [大区]", "绑定外服账号，之后查询不用再写名字",
                (("Hide on bush#KR1 kr", ""),), ("/lol 账号", "/lol 绑定国服"), note="外服默认大区可在插件配置里改"),
            Cmd("绑定国服", "名字#编号 [大区]", "绑定国服账号", (("名字#12345 艾欧尼亚", ""),), ("/lol 国服状态",), needs=_f("cn")),
            Cmd("账号", "", "查看已绑定的账号", (), ("/lol 切换", "/lol 解绑")),
            Cmd("切换", "<序号>", "切换默认账号", (("2", "序号见 /lol 账号"),), ("/lol 账号",)),
            Cmd("解绑", "[序号]", "解除绑定", (("2", ""),), ("/lol 账号",)),
            Cmd("大区", "", "查看大区的写法", (), ("/lol 绑定",)),
        ), button="📜 战绩"),
    Cat("champ", "🛡️", "英雄与版本", ("英雄与版本", "版本", "英雄相关"),
        ("/lol 英雄 亚索", "/lol 梯队 中单", "/lol 今天玩什么"), (
            Cmd("英雄", "<英雄|梯队序号> [位置] [模式] [地区] [段位]", "出装、符文、加点和克制",
                (("亚索 中单", ""), ("3", "看完梯队后查第 3 名"), ("盲僧 打野 韩服 钻石+", "")), ("/lol 梯队", "/lol 海克斯")),
            Cmd("海克斯", "<英雄>", "海克斯大乱斗：出装与强化推荐", (("亚索", ""),), ("/lol 英雄",)),
            Cmd("梯队", "[位置] [地区] [段位]", "版本强势英雄排行", (("", "全部分路"), ("打野 韩服 大师+", "")), ("/lol 英雄 3",)),
            Cmd("今天玩什么", "[位置]", "从当前版本 T1 / T2 英雄里随机抽一个", (("中单", ""),), ("/lol 梯队",)),
        ), button="🛡️ 英雄"),
    Cat("esports", "🏆", "赛事", ("赛事", "电竞", "比赛"),
        ("/lol 赛程", "/lol 赛况 BLG", "/lol 积分 LPL"), (
            Cmd("赛程", "[联赛…]", "近期赛程，可写多个联赛合并显示",
                (("", "按本会话订阅的赛事"), ("S赛 LPL", ""), ("全部", "")), ("/lol 赛果", "/lol 订阅 赛事")),
            Cmd("赛果", "[联赛…]", "近期赛果，带序号可查赛况", (("LPL", ""),), ("/lol 赛况 1",)),
            Cmd("赛况", "[序号|战队] [局号]", "每局阵容、击杀、资源和 MVP；加局号看单局 10 人",
                (("1", "赛果卡片里的第 1 场"), ("BLG", ""), ("BLG 2", "第 2 局")), ("/lol 赛况点评",)),
            Cmd("积分", "<联赛>", "联赛积分榜", (("LPL", ""), ("LCK", "")), ("/lol 赛程",)),
        ), button="🏆 赛事"),
    Cat("ai", "🤖", "AI 点评", ("ai点评", "ai", "点评相关"),
        ("/lol 点评", "/lol 诊断", "/lol 赛况点评 BLG"), (
            Cmd("点评", "[序号] [名字#标签] [专业|幽默|锐评]", "AI 点评单局；/lol 锐评 直接用锐评风格",
                (("", "最近一局"), ("2 幽默", "")), ("/lol 诊断",)),
            Cmd("诊断", "[名字#标签] [局数] [风格]", "近期战绩诊断：整体与最近 5 局对比", (("", ""), ("10", "最近 10 局")), ("/lol 点评",)),
            Cmd("赛况点评", "[战队] [局号] [风格]", "AI 点评比赛单局，不写局号点评整场", (("BLG 3", ""), ("BLG", "整场")), ("/lol 赛况",)),
        ), needs=_f("ai"), button="🤖 AI"),
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
        ), needs=_f("group", "group_chat"), button="🎉 群玩法"),
    Cat("tft", "♟️", "云顶之弈", ("云顶之弈", "云顶", "tft"),
        ("/tft 阵容", "/tft 装备 大天使"), (
            Cmd("阵容", "[序号]", "版本强势阵容，加序号看阵容详情", (("", ""), ("1", "第 1 名阵容")), ("/tft 英雄",), group="tft"),
            Cmd("英雄", "<名称>", "云顶英雄出装", (("凯特琳", ""),), ("/tft 装备",), group="tft"),
            Cmd("装备", "<名称>", "装备适合谁带", (("大天使", ""),), ("/tft 英雄",), group="tft"),
            Cmd("强化", "<关键词>", "强化符文搜索", (("贷款", ""),), ("/tft 阵容",), group="tft"),
        ), button="♟️ 云顶"),
    Cat("settings", "🔔", "订阅与设置", ("订阅与设置", "设置", "订阅相关"),
        ("/lol 订阅 赛事 LPL", "/lol 模式 图片"), (
            Cmd("订阅", "战绩|赛事 [联赛…]|梯队|云顶|列表|取消 <ID>", "订阅推送，也可以查看和取消订阅",
                (("战绩", "有新对局时推送"), ("赛事 S赛 LPL", ""), ("列表", ""), ("取消 3", "")), ("/lol 模式",),
                needs=_f("push"), note="旧写法 /lol 订阅列表、/lol 退订 仍然可用"),
            Cmd("模式", "[文本|md|图片|默认]", "本会话的默认输出方式；单次查询可加 -t / -m / -i", (("图片", ""),)),
            Cmd("国服状态", "", "国服连接和 WeGame 凭证状态", (), ("/lol 绑定国服",), needs=_f("cn")),
            Cmd("WeGame登录", "[QQ|微信]", "管理员扫码登录 WeGame，自动保存国服凭证", (("微信", ""),), needs=_f("cn", "admin")),
            Cmd("状态", "", "插件版本与运行状态"),
            Cmd("刷新", "", "刷新英雄和云顶数据", needs=_f("admin")),
            Cmd("帮助", "[分类|指令|全部]", "帮助菜单", (("赛事", ""), ("竞猜", ""), ("全部", ""))),
        ), button="🔔 设置"),
)

# old or alternative command names -> the entry that documents them
HELP_ALIASES = {"日榜": "榜单", "周榜": "榜单", "竞猜榜": "榜单", "猜英雄榜": "榜单", "订阅列表": "订阅", "退订": "订阅",
                "锐评": "点评", "出装": "英雄", "help": "帮助"}
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


def commands(cat: Cat, ctx: Ctx | None = None) -> list[Cmd]:
    return [c for c in cat.cmds if ctx is None or ctx.ok(c.needs)]


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
def main_menu(ctx: Ctx, bare: bool = False) -> str:
    cats = visible(ctx)
    head = f"联盟助手 v{ctx.version}" if ctx.version else "联盟助手"
    lines = [f"👋 需要帮助吗？这是{head} 的主菜单" if bare else f"📖 {head} · 帮助菜单"]
    if ctx.account:
        lines.append(f"已绑定：{ctx.account}")
    else:
        lines.append("👉 先绑定账号：/lol 绑定 名字#标签 [大区]，之后查战绩不用再写名字")
    lines.append("")
    for i, cat in enumerate(cats, 1):
        lines.append(f"{i}. {cat.label}")
        lines.append("   " + " · ".join(cat.quick))
    lines.append("")
    lines.append("💡 也可以直接发 /lol 亚索、/lol Faker#KR1、/lol BLG、/lol LPL")
    lines.append(f"回复序号查看分类（{REPLY_SECS} 秒内），或发送 /lol 帮助 <序号|分类|指令>，例如 /lol 帮助 竞猜")
    lines.append("全部指令：/lol 帮助 全部")
    return "\n".join(lines)


def category_text(cat: Cat, ctx: Ctx) -> str:
    cats = visible(ctx)
    pos = next((i for i, c in enumerate(cats, 1) if c.key == cat.key), 0)
    lines = [f"{cat.label}" + (f" · {pos}/{len(cats)}" if pos else "")]
    if not ctx.ok(cat.needs):
        lines.append(_unavailable(cat.needs, ctx))
    for c in commands(cat, ctx) or list(cat.cmds):
        lines.append("")
        lines.append(f"▸ {c.line()}")
        lines.append(f"   {c.desc}")
        if c.examples:
            lines.append("   例：" + " · ".join(f"{c.full} {a}".rstrip() for a, _ in c.examples[:2]))
    lines.append("")
    lines.append(f"单个指令的详细用法：/lol 帮助 {commands(cat, ctx)[0].name if commands(cat, ctx) else cat.cmds[0].name}")
    lines.append("返回主菜单：回复 0 或发送 /lol 帮助；回复其他序号切换分类")
    return "\n".join(lines)


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
    lines = [f"联盟助手 v{ctx.version} · 全部指令" if ctx.version else "联盟助手 · 全部指令"]
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
    doc = Doc(title="指令菜单", subtitle=f"联盟助手 v{ctx.version}" if ctx.version else "联盟助手", theme="tool", width=760)
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
def button_rows(ctx: Ctx, current: str | None = None) -> list[list[tuple[str, str]]]:
    """[(button id suffix, label)] rows for the menu buttons; the current category is swapped for "主菜单"."""
    items = [("main", "🏠 主菜单")] if current else []
    items += [(c.key, c.button or c.label) for c in visible(ctx) if c.key != current]
    per = 4
    return [items[i:i + per] for i in range(0, len(items), per)]
