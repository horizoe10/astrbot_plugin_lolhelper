"""AstrBot 联盟助手 (lolhelper) — LoL / TFT data with text, Markdown and image output."""

from __future__ import annotations

import re

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .lolhelper import PLUGIN_NAME, VERSION
from .lolhelper.args import MODE_WORDS, parse_args, parse_leagues, parse_target, pick_game_mode, pick_position
from .lolhelper.opgg_web import pick_scope
from .lolhelper.constants import (
    CN_LEGACY_SERVERS,
    CN_SERVERS,
    OPGG_REGIONS,
    POSITIONS,
    resolve_league,
)
from .lolhelper.errors import LolHelperError, UsageError
from .lolhelper.facade import Facade
from .lolhelper.push import SUB_TYPES, PushManager
from .lolhelper.service import View
from .lolhelper.settings import Settings
from .lolhelper.service import league_label
from .lolhelper.storage import account_label, sub_league_list

# first words that start a plugin command (group names and aliases)
ENTRY_WORDS = {"lol", "tft", "云顶"}

MODE_ZH = {"text": "纯文本", "markdown": "Markdown", "image": "图片"}

HELP = """联盟助手 v{version}

英雄联盟
/lol 英雄 <英雄|梯队序号> [位置] [模式]   出装/符文/克制
/lol 海克斯 <英雄>              海克斯大乱斗：出装 + 强化推荐
/lol 梯队 [位置] [全球/韩服] [翡翠+…]   版本强势英雄
/lol 战绩 [名字#标签] [大区]     最近对局（可@已绑定的人）
/lol 资料 [名字#标签] [大区]     段位与常用英雄
/lol 对局 [序号] [名字#标签]     单局详情（双方 10 人）
/lol 赛程 [联赛…] | 赛果 [联赛…]  可写多个，如 S赛 LPL；不写时按本会话订阅
/lol 赛况 [序号|战队] [局号]     每局阵容与数据（序号对应赛果卡片）
/lol 积分 <联赛>

云顶之弈
/tft 阵容 [序号]   版本阵容 / 单个阵容详情
/tft 英雄 <名称>   英雄出装
/tft 装备 <名称>   适合携带的英雄
/tft 强化 <关键词> 强化符文

账号与设置
/lol 绑定 名字#标签 [大区]        外服默认 {region}
/lol 绑定国服 名字#编号 [大区]    需登录国服客户端（本机或远程代理）
/lol 账号 | 切换 <序号> | 解绑 [序号]
/lol 模式 [文本|md|图片|默认]
/lol 订阅 战绩|赛事 [联赛…]|梯队|云顶
/lol 订阅列表 | 退订 <ID|all>
/lol 大区 | 国服状态

任意查询可加 -t / -m / -i 指定本次输出为文本 / Markdown / 图片"""


def _as_markdown(result):
    """Mark a result as Markdown on AstrBot versions that support it (e.g. QQ official)."""
    fn = getattr(result, "use_markdown", None)
    return fn(True) if callable(fn) else result


class LolHelperPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.settings = Settings.from_config(dict(self.config))
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.f = Facade(self.settings, data_dir, html_render=self.html_render, logger=logger)
        self.push = PushManager(self.f, self._push_send, logger)

    async def initialize(self) -> None:
        await self.f.start()
        self.push.start()
        logger.info(f"[lolhelper] 联盟助手 v{VERSION} 已加载")

    async def terminate(self) -> None:
        await self.push.stop()
        await self.f.close()

    # =============================================================== helpers
    @staticmethod
    def _rest(event: AstrMessageEvent, names: set[str]) -> str:
        toks = event.get_message_str().strip().split()
        for i, t in enumerate(toks[:3]):
            if t.lstrip("/") in names:
                return " ".join(toks[i + 1:])
        return " ".join(toks[2:])

    @staticmethod
    def _user_key(event: AstrMessageEvent, user_id: str | None = None) -> str:
        return f"{event.get_platform_id()}:{user_id or event.get_sender_id()}"

    @staticmethod
    def _mentioned(event: AstrMessageEvent) -> str | None:
        self_id = str(getattr(event.message_obj, "self_id", "") or "")
        for comp in event.message_obj.message or []:
            if isinstance(comp, Comp.At) and str(comp.qq) not in (self_id, "all"):
                return str(comp.qq)
        return None

    def _platform_name(self, umo: str) -> str | None:
        try:
            inst = self.context.get_platform_inst(umo.split(":", 1)[0])
            return inst.meta().name if inst else None
        except Exception:
            return None

    async def _reply(self, event: AstrMessageEvent, view: View, mode: str | None = None):
        umo = event.unified_msg_origin
        mode = self.f.resolve_mode(mode, umo, event.get_platform_name())
        if mode == "image":
            try:
                ref = await self.f.image(view, umo)
                return event.image_result(ref)
            except Exception as exc:
                logger.warning(f"[lolhelper] HTML 图片渲染失败，尝试降级：{exc}")
            if self.settings.image_fallback_markdown:
                try:
                    return event.image_result(await self._md_image(self.f.markdown(view), umo))
                except Exception as exc:
                    logger.warning(f"[lolhelper] Markdown 转图片失败，改发文本：{exc}")
            mode = "text"
        if mode == "markdown":
            return _as_markdown(event.plain_result(self.f.markdown(view)).use_t2i(False))
        return event.plain_result(self.f.text(view)).use_t2i(False)

    async def _md_image(self, markdown: str, umo: str) -> str:
        try:
            return await self.text_to_image(markdown, umo=umo)
        except TypeError:  # older AstrBot
            return await self.text_to_image(markdown)

    async def _answer(self, event: AstrMessageEvent, make, mode: str | None):
        try:
            view = await make()
        except LolHelperError as exc:
            return event.plain_result(f"⚠️ {exc}").use_t2i(False)
        except Exception as exc:
            logger.exception("[lolhelper] 查询出错")
            return event.plain_result(f"⚠️ 查询失败：{type(exc).__name__}").use_t2i(False)
        return await self._reply(event, view, mode)

    def _plain(self, event: AstrMessageEvent, text: str):
        return event.plain_result(text).use_t2i(False)

    async def _push_send(self, umo: str, view: View, mode: str | None = None) -> None:
        mode = mode or self.f.resolve_mode(None, umo, self._platform_name(umo))
        chain = MessageChain()
        if mode == "image":
            try:
                ref = await self.f.image(view, umo)
                chain = chain.url_image(ref) if ref.startswith("http") else chain.file_image(ref)
            except Exception as exc:
                logger.warning(f"[lolhelper] 推送图片渲染失败，改发文本：{exc}")
                chain = MessageChain().message(self.f.text(view))
        elif mode == "markdown":
            chain = _as_markdown(chain.message(self.f.markdown(view)))
        else:
            chain = chain.message(self.f.text(view))
        chain.use_t2i(False)
        await self.context.send_message(umo, chain)

    async def _account_for(self, event: AstrMessageEvent, words: list[str]) -> dict:
        if any("#" in w or "＃" in w for w in words):
            return await self.f.account_from_target(parse_target(words, self.settings.cn_servers_extra))
        mention = self._mentioned(event)
        user = self._user_key(event, mention)
        acc = self.f.store.default_account(user)
        if not acc:
            who = "对方" if mention else "你"
            raise UsageError(f"{who}还没有绑定账号。用法：/lol 绑定 名字#标签 [大区]，或直接 /lol 战绩 名字#标签 大区")
        return acc

    # ================================================================== /lol
    @filter.command_group("lol")
    def lol(self):
        """英雄联盟数据"""

    @lol.command("英雄", alias={"出装", "build", "champ", "champion"})
    async def lol_champion(self, event: AstrMessageEvent):
        """英雄出装、符文与克制：/lol 英雄 亚索 中单 [韩服] [钻石+]，或看完梯队后 /lol 英雄 3"""
        a = parse_args(self._rest(event, {"英雄", "出装", "build", "champ", "champion"}))
        pos, words = pick_position(a.words)
        mode, words = pick_game_mode(words)
        region, tier, words = pick_scope(words)
        if not words:
            yield self._plain(event, "用法：/lol 英雄 <英雄名|梯队序号> [位置] [排位/大乱斗] [全球/韩服] [翡翠+/钻石+…]")
            return
        if len(words) == 1 and words[0].isdigit() and int(words[0]) <= 50:
            last = self.f.store.state(f"last_tier:{event.unified_msg_origin}") or {}
            order = last.get("order") or {}
            if not order:
                yield self._plain(event, "请先发送 /lol 梯队 [位置] 查看排行榜，再用序号查询英雄，例如 /lol 英雄 3")
                return
            if pos is None and len(order) == 1:
                pos = next(iter(order))
            if pos not in order:
                yield self._plain(event, "上一张是全分路梯队，请带上位置，例如 /lol 英雄 中单 3")
                return
            n = int(words[0])
            if not 1 <= n <= len(order[pos]):
                yield self._plain(event, f"序号需要在 1~{len(order[pos])} 之间")
                return
            item = order[pos][n - 1]
            c = self.f.gd.champion(item) if isinstance(item, int) or str(item).isdigit() else None
            words = [self.f.gd.display_name(c).split(" ")[0] if c else str(item)]
            region, tier = region or last.get("region"), tier or last.get("tier")
        yield await self._answer(event, lambda: self.f.champion(" ".join(words), pos, mode, region, tier), a.mode)

    @lol.command("海克斯", alias={"海克斯大乱斗", "mayhem"})
    async def lol_mayhem(self, event: AstrMessageEvent):
        """海克斯大乱斗：/lol 海克斯 亚索"""
        a = parse_args(self._rest(event, {"海克斯", "海克斯大乱斗", "mayhem"}))
        _, words = pick_position(a.words)
        _, words = pick_game_mode(words)
        if not words:
            yield self._plain(event, "用法：/lol 海克斯 <英雄名>")
            return
        yield await self._answer(event, lambda: self.f.champion(" ".join(words), None, "mayhem"), a.mode)

    @lol.command("梯队", alias={"tier", "强势", "版本"})
    async def lol_tier(self, event: AstrMessageEvent):
        """版本梯队：/lol 梯队 [位置] [全球/韩服] [翡翠+/钻石+…]"""
        a = parse_args(self._rest(event, {"梯队", "tier", "强势", "版本"}))
        pos, words = pick_position(a.words)
        region, tier, _ = pick_scope(words)
        umo = event.unified_msg_origin

        async def make():
            view = await self.f.tier(pos, region, tier)
            scope = view.data.get("scope") or {}
            self.f.store.set_state(f"last_tier:{umo}", {
                "order": view.data.get("order") or {}, "region": scope.get("region"), "tier": scope.get("tier")})
            return view

        yield await self._answer(event, make, a.mode)

    @lol.command("战绩", alias={"match", "matches", "最近", "战绩查询"})
    async def lol_matches(self, event: AstrMessageEvent):
        """最近对局：/lol 战绩 [名字#标签] [大区]"""
        a = parse_args(self._rest(event, {"战绩", "match", "matches", "最近", "战绩查询"}))

        async def make():
            acc = await self._account_for(event, a.words)
            return await self.f.summoner(acc, a.count)

        yield await self._answer(event, make, a.mode)

    @lol.command("资料", alias={"profile", "查询", "段位", "rank"})
    async def lol_profile(self, event: AstrMessageEvent):
        """段位与常用英雄：/lol 资料 [名字#标签] [大区]"""
        a = parse_args(self._rest(event, {"资料", "profile", "查询", "段位", "rank"}))

        async def make():
            acc = await self._account_for(event, a.words)
            return await self.f.summoner(acc, a.count, detail=True)

        yield await self._answer(event, make, a.mode)

    @lol.command("对局", alias={"详情", "对局详情", "detail", "game"})
    async def lol_game(self, event: AstrMessageEvent):
        """单局详情：/lol 对局 [序号] [名字#标签] [大区]，序号为最近第几场，默认 1"""
        a = parse_args(self._rest(event, {"对局", "详情", "对局详情", "detail", "game"}))
        words = list(a.words)
        index = 1
        if words and words[0].isdigit():
            index = int(words.pop(0))
        elif words and words[-1].isdigit() and "#" not in words[-1]:
            index = int(words.pop())

        async def make():
            acc = await self._account_for(event, words)
            return await self.f.game_detail(acc, index)

        yield await self._answer(event, make, a.mode)

    @lol.command("赛程", alias={"schedule"})
    async def lol_schedule(self, event: AstrMessageEvent):
        """电竞赛程：/lol 赛程 [联赛…]"""
        a = parse_args(self._rest(event, {"赛程", "schedule"}))
        leagues, bad = self._leagues(event, a.words)
        if bad:
            yield self._plain(event, f"未知联赛「{' '.join(bad)}」，例如 LPL / LCK / S赛 / MSI")
            return
        yield await self._answer(event, lambda: self.f.esports("schedule", leagues), a.mode)

    @lol.command("赛果", alias={"result", "results"})
    async def lol_results(self, event: AstrMessageEvent):
        """电竞赛果：/lol 赛果 [联赛…]"""
        a = parse_args(self._rest(event, {"赛果", "result", "results"}))
        leagues, bad = self._leagues(event, a.words)
        if bad:
            yield self._plain(event, f"未知联赛「{' '.join(bad)}」，例如 LPL / LCK / S赛 / MSI")
            return
        yield await self._answer(event, lambda: self.f.esports("result", leagues), a.mode)

    @lol.command("赛况", alias={"比赛", "match"})
    async def lol_match(self, event: AstrMessageEvent):
        """比赛数据：/lol 赛况 [联赛…] [序号|战队] [局号]"""
        a = parse_args(self._rest(event, {"赛况", "比赛", "match"}))
        words = [w for w in a.words if w.upper() != "AL"]  # AL is both a league and a team; treat it as the team
        leagues, rest = parse_leagues(words)
        rest += [w for w in a.words if w.upper() == "AL"]
        index = match_id = game_no = None
        team = None
        for w in rest:
            m = re.fullmatch(r"(?:第|g|game)?(\d+)(?:局)?", w, re.I)
            if m and (w[0] in "第gG" or w.endswith("局")):
                game_no = int(m.group(1))
            elif w.isdigit():
                n = int(w)
                if n >= 1000:
                    match_id = w
                elif index is None and team is None:
                    index = n
                else:
                    game_no = n
            else:
                team = w
        if team and index is not None and game_no is None:
            index, game_no = None, index
        leagues = leagues or (None if (match_id or team) else self.f.sub_leagues(event.unified_msg_origin))
        yield await self._answer(event, lambda: self.f.esports_match(leagues, index, team, match_id, game_no), a.mode)

    def _leagues(self, event: AstrMessageEvent, words: list[str]) -> tuple[list[str] | None, list[str]]:
        if not words:
            return self.f.sub_leagues(event.unified_msg_origin), []
        if len(words) == 1 and words[0] in ("全部", "所有", "all"):
            return None, []
        leagues, bad = parse_leagues(words)
        return leagues, bad

    @lol.command("积分", alias={"standings", "积分榜"})
    async def lol_standings(self, event: AstrMessageEvent):
        """联赛积分榜：/lol 积分 LPL"""
        a = parse_args(self._rest(event, {"积分", "standings", "积分榜"}))
        league = resolve_league(a.rest) or a.rest.lower()
        if not league:
            yield self._plain(event, "用法：/lol 积分 <联赛>，例如 /lol 积分 LPL")
            return
        yield await self._answer(event, lambda: self.f.standings(league), a.mode)

    @lol.command("帮助", alias={"help"})
    async def lol_help(self, event: AstrMessageEvent):
        """查看帮助"""
        yield self._plain(event, HELP.format(version=VERSION, region=OPGG_REGIONS.get(self.settings.default_region, self.settings.default_region)))

    # ================================================================== /tft
    @filter.command_group("tft", alias={"云顶"})
    def tft(self):
        """云顶之弈数据"""

    @tft.command("阵容", alias={"meta", "comp", "comps"})
    async def tft_meta(self, event: AstrMessageEvent):
        """版本阵容：/tft 阵容 [序号]"""
        a = parse_args(self._rest(event, {"阵容", "meta", "comp", "comps"}))
        if a.words and a.words[0].isdigit():
            idx = int(a.words[0])
            yield await self._answer(event, lambda: self.f.tft.deck(idx), a.mode)
            return
        yield await self._answer(event, lambda: self.f.tft.meta(a.count or 10), a.mode)

    @tft.command("英雄", alias={"champ", "champion", "出装"})
    async def tft_champion(self, event: AstrMessageEvent):
        """云顶英雄出装：/tft 英雄 凯特琳"""
        a = parse_args(self._rest(event, {"英雄", "champ", "champion", "出装"}))
        if not a.rest:
            yield self._plain(event, "用法：/tft 英雄 <英雄名>")
            return
        yield await self._answer(event, lambda: self.f.tft.champion(a.rest), a.mode)

    @tft.command("装备", alias={"item", "items"})
    async def tft_item(self, event: AstrMessageEvent):
        """装备适合谁：/tft 装备 大天使"""
        a = parse_args(self._rest(event, {"装备", "item", "items"}))
        if not a.rest:
            yield self._plain(event, "用法：/tft 装备 <装备名>")
            return
        yield await self._answer(event, lambda: self.f.tft.item(a.rest), a.mode)

    @tft.command("强化", alias={"augment", "海克斯", "符文"})
    async def tft_augment(self, event: AstrMessageEvent):
        """强化符文搜索：/tft 强化 贷款"""
        a = parse_args(self._rest(event, {"强化", "augment", "海克斯", "符文"}))
        yield await self._answer(event, lambda: self.f.tft.augment(a.rest), a.mode)

    @tft.command("帮助", alias={"help"})
    async def tft_help(self, event: AstrMessageEvent):
        """查看帮助"""
        yield self._plain(event, HELP.format(version=VERSION, region=OPGG_REGIONS.get(self.settings.default_region, self.settings.default_region)))

    # ======================================================= /lol 账号与设置
    async def _bind(self, event: AstrMessageEvent, words: list[str], force_cn: bool):
        tgt = parse_target(words, self.settings.cn_servers_extra)
        if force_cn:
            tgt.is_cn = True
        try:
            acc = await self.f.account_from_target(tgt)
            if acc["kind"] == "global":
                summ, _ = await self.f.svc._profile(acc["game_name"], acc["tag_line"], acc["region"])
                acc["game_name"] = summ.get("game_name") or acc["game_name"]
                acc["tag_line"] = summ.get("tagline") or acc["tag_line"]
            elif not acc.get("server"):
                raise UsageError("无法确定国服大区，请在命令末尾写上大区，例如 艾欧尼亚")
        except LolHelperError as exc:
            return self._plain(event, f"⚠️ 绑定失败：{exc}")
        idx = await self.f.store.bind(self._user_key(event), acc)
        return self._plain(event, f"✅ 已绑定 {account_label(acc)}（序号 {idx + 1}，已设为默认）\n发送 /lol 战绩 查看最近对局")

    @lol.command("绑定", alias={"bind"})
    async def lol_bind(self, event: AstrMessageEvent):
        """绑定账号：/lol 绑定 名字#标签 [大区]"""
        a = parse_args(self._rest(event, {"绑定", "bind"}))
        if not a.words:
            yield self._plain(event, "用法：/lol 绑定 名字#标签 [大区]\n国服请用：/lol 绑定国服 名字#编号 [大区]")
            return
        yield await self._bind(event, a.words, force_cn=False)

    @lol.command("绑定国服", alias={"bindcn", "国服绑定"})
    async def lol_bind_cn(self, event: AstrMessageEvent):
        """绑定国服账号：/lol 绑定国服 名字#编号 [大区]"""
        a = parse_args(self._rest(event, {"绑定国服", "bindcn", "国服绑定"}))
        if not a.words:
            names = "、".join(v[0] for v in CN_SERVERS.values())
            yield self._plain(event, f"用法：/lol 绑定国服 名字#编号 [大区]\n可用大区：{names}\n（旧大区名如 祖安、德玛西亚 会自动映射到合并后的大区）")
            return
        if not self.settings.cn_enabled:
            yield self._plain(event, "国服功能未启用（插件配置 cn.enabled）")
            return
        yield await self._bind(event, a.words, force_cn=True)

    @lol.command("账号", alias={"我的", "accounts", "me"})
    async def lol_accounts(self, event: AstrMessageEvent):
        """查看已绑定账号"""
        user = self._user_key(event)
        accs = self.f.store.accounts(user)
        if not accs:
            yield self._plain(event, "还没有绑定账号。/lol 绑定 名字#标签 [大区]")
            return
        default = self.f.store.default_account(user)
        lines = ["已绑定账号："]
        for i, acc in enumerate(accs, 1):
            mark = "★" if acc == default else "  "
            lines.append(f"{mark}{i}. {account_label(acc)}")
        lines.append("切换默认：/lol 切换 序号　解绑：/lol 解绑 序号")
        yield self._plain(event, "\n".join(lines))

    @lol.command("切换", alias={"switch", "默认"})
    async def lol_switch(self, event: AstrMessageEvent):
        """切换默认账号：/lol 切换 2"""
        a = parse_args(self._rest(event, {"切换", "switch", "默认"}))
        if not a.rest.isdigit():
            yield self._plain(event, "用法：/lol 切换 <序号>（序号见 /lol 账号）")
            return
        acc = await self.f.store.set_default(self._user_key(event), int(a.rest) - 1)
        yield self._plain(event, f"✅ 默认账号已切换为 {account_label(acc)}" if acc else "序号不存在")

    @lol.command("解绑", alias={"unbind"})
    async def lol_unbind(self, event: AstrMessageEvent):
        """解绑账号：/lol 解绑 [序号]，不填则全部解绑"""
        a = parse_args(self._rest(event, {"解绑", "unbind"}))
        idx = int(a.rest) - 1 if a.rest.isdigit() else None
        removed = await self.f.store.unbind(self._user_key(event), idx)
        if not removed:
            yield self._plain(event, "没有可解绑的账号")
            return
        yield self._plain(event, "✅ 已解绑：" + "、".join(account_label(x) for x in removed))

    @lol.command("模式", alias={"mode", "输出"})
    async def lol_mode(self, event: AstrMessageEvent):
        """设置本会话输出方式：/lol 模式 文本|md|图片|默认"""
        a = parse_args(self._rest(event, {"模式", "mode", "输出"}))
        umo = event.unified_msg_origin
        word = a.rest.lower()
        if not word:
            cur = self.f.store.mode(umo)
            yield self._plain(event, f"当前会话输出方式：{MODE_ZH.get(cur or self.settings.default_mode)}{'（跟随默认）' if not cur else ''}\n可选：文本 / md / 图片 / 默认")
            return
        if word in ("默认", "default", "reset"):
            await self.f.store.set_mode(umo, None)
            yield self._plain(event, f"✅ 已恢复默认输出方式（{MODE_ZH[self.settings.default_mode]}）")
            return
        mode = MODE_WORDS.get(word)
        if not mode:
            yield self._plain(event, "可选：文本 / md / 图片 / 默认")
            return
        await self.f.store.set_mode(umo, mode)
        note = ""
        if mode == "markdown" and event.get_platform_name() not in self.settings.markdown_platforms:
            note = "\n注意：当前平台不在 Markdown 平台列表中，将自动按纯文本发送（可在 output.markdown_platforms 中添加）"
        yield self._plain(event, f"✅ 本会话输出方式已设为 {MODE_ZH[mode]}{note}")

    @lol.command("大区", alias={"regions", "region"})
    async def lol_regions(self, event: AstrMessageEvent):
        """查看可用大区"""
        intl = " ".join(f"{k}({v})" for k, v in OPGG_REGIONS.items())
        cn = " ".join(f"{v[0]}({k.split('_', 1)[1]})" for k, v in CN_SERVERS.items())
        legacy = "、".join(CN_LEGACY_SERVERS)
        yield self._plain(event, f"外服（OP.GG）：{intl}\n\n国服：{cn}\n\n旧大区自动映射：{legacy}")

    @lol.command("国服状态", alias={"cn", "代理状态"})
    async def lol_cn_status(self, event: AstrMessageEvent):
        """检查国服客户端连接"""
        a = parse_args(self._rest(event, {"国服状态", "cn", "代理状态"}))
        yield await self._answer(event, self.f.cn_status, a.mode or "text")

    def _can_subscribe(self, event: AstrMessageEvent) -> bool:
        if not self.settings.push_group_admin_only or event.is_private_chat():
            return True
        return event.is_admin()

    @lol.command("订阅", alias={"sub", "subscribe"})
    async def lol_subscribe(self, event: AstrMessageEvent):
        """订阅推送：/lol 订阅 战绩|赛事 [联赛]|梯队|云顶"""
        a = parse_args(self._rest(event, {"订阅", "sub", "subscribe"}))
        if not self.settings.push_enabled:
            yield self._plain(event, "推送功能未启用（插件配置 push.enabled）")
            return
        if not self._can_subscribe(event):
            yield self._plain(event, "本群仅管理员可以设置订阅")
            return
        kind_word = a.words[0] if a.words else ""
        umo = event.unified_msg_origin
        by = self._user_key(event)
        if kind_word in ("战绩", "对局", "match"):
            user = by
            accs = self.f.store.accounts(user)
            acc = self.f.store.default_account(user)
            if len(a.words) > 1 and a.words[1].isdigit() and 0 < int(a.words[1]) <= len(accs):
                acc = accs[int(a.words[1]) - 1]
            if not acc:
                yield self._plain(event, "请先绑定账号：/lol 绑定 名字#标签 [大区]")
                return
            sub, new = await self.f.store.add_sub(umo, "match", {"account": acc}, by)
            desc = f"{account_label(acc)} 的新对局"
        elif kind_word in ("赛事", "赛程", "比赛", "esports"):
            leagues, bad = parse_leagues(a.words[1:])
            if bad:
                yield self._plain(event, f"未知联赛「{' '.join(bad)}」，例如 LPL / LCK / S赛 / MSI")
                return
            target = {} if not leagues else {"league": leagues[0]} if len(leagues) == 1 else {"leagues": sorted(leagues)}
            sub, new = await self.f.store.add_sub(umo, "esports", target, by)
            desc = f"{' / '.join(league_label(x) for x in leagues) or '全部联赛'} 开赛提醒与赛果（含每局数据）"
        elif kind_word in ("梯队", "版本", "lol"):
            sub, new = await self.f.store.add_sub(umo, "lol_daily", {}, by)
            desc = f"每天 {self.settings.push_daily_hour}:00 的 LOL 梯队日报"
        elif kind_word in ("云顶", "阵容", "tft"):
            sub, new = await self.f.store.add_sub(umo, "tft_daily", {}, by)
            desc = f"每天 {self.settings.push_daily_hour}:00 的云顶阵容日报"
        else:
            yield self._plain(event, "用法：/lol 订阅 战绩 [账号序号] | 赛事 [联赛] | 梯队 | 云顶")
            return
        yield self._plain(event, f"{'✅ 已订阅' if new else 'ℹ️ 已经订阅过'}：{desc}（ID {sub['id']}）")
        if sub["type"] == "esports":
            s = self.settings
            yield self._plain(event, f"推送：每天 {s.push_daily_hour}:00 赛程预告 · 开赛前 {int(s.push_esports_remind // 60)} 分钟提醒 · 每局结束推送比分 · 整场结束推送赛果。近期赛程如下：")
            target_leagues = sub_league_list(sub) or None
            yield await self._answer(event, lambda: self.f.esports("schedule", target_leagues), None)

    @lol.command("订阅列表", alias={"subs"})
    async def lol_subs(self, event: AstrMessageEvent):
        """查看本会话订阅"""
        subs = self.f.store.subs(umo=event.unified_msg_origin)
        if not subs:
            yield self._plain(event, "本会话没有订阅")
            return
        lines = ["本会话订阅："]
        for s in subs:
            t = s.get("target") or {}
            detail = account_label(t["account"]) if t.get("account") else " / ".join(league_label(x) for x in sub_league_list(s))
            lines.append(f"[{s['id']}] {SUB_TYPES.get(s['type'], s['type'])} {detail}".rstrip())
        lines.append("退订：/lol 退订 ID 或 /lol 退订 all")
        yield self._plain(event, "\n".join(lines))

    @lol.command("退订", alias={"unsub", "unsubscribe"})
    async def lol_unsubscribe(self, event: AstrMessageEvent):
        """取消订阅：/lol 退订 <ID|all>"""
        a = parse_args(self._rest(event, {"退订", "unsub", "unsubscribe"}))
        if not a.rest:
            yield self._plain(event, "用法：/lol 退订 <ID|all>（ID 见 /lol 订阅列表）")
            return
        if not self._can_subscribe(event):
            yield self._plain(event, "本群仅管理员可以修改订阅")
            return
        n = await self.f.store.remove_sub(event.unified_msg_origin, a.rest.strip())
        yield self._plain(event, f"✅ 已取消 {n} 个订阅" if n else "没有找到该订阅")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @lol.command("刷新", alias={"refresh"})
    async def lol_refresh(self, event: AstrMessageEvent):
        """清空缓存并刷新游戏数据（管理员）"""
        try:
            await self.f.refresh()
        except Exception as exc:
            yield self._plain(event, f"⚠️ 刷新失败：{exc}")
            return
        yield self._plain(event, f"✅ 已刷新。Data Dragon 版本 {self.f.gd.version}，云顶 S{self.f.gd.tft.get('set', '?')}")

    @lol.command("状态", alias={"status"})
    async def lol_status(self, event: AstrMessageEvent):
        """插件运行状态"""
        s = self.settings
        lines = [
            f"联盟助手 v{VERSION}",
            f"OP.GG MCP：{len(self.f.mcp.tools)} 个工具" + ("" if self.f.mcp.tools else "（尚未连接）"),
            f"Data Dragon：{self.f.gd.version or '未加载'}",
            f"默认输出：{MODE_ZH[s.default_mode]}　本会话：{MODE_ZH.get(self.f.store.mode(event.unified_msg_origin) or '', '跟随默认')}",
            f"国服：{self.f.bridge.label if s.cn_enabled else '未启用'}",
            f"订阅总数：{len(self.f.store.data['subs'])}",
        ]
        yield self._plain(event, "\n".join(lines))

    # ========================================================= compat entry
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def compat_entry(self, event: AstrMessageEvent):
        """兼容入口：唤醒前缀为空（如 QQ 官方机器人）时，也能识别 /lol、／lol 以及不 @ 的「lol 子指令」"""
        text = (event.get_message_str() or "").strip()
        explicit = text[:1] in ("/", "／")
        body = text.lstrip("/／").strip()
        words = body.split()
        if not words or words[0].lower() not in ENTRY_WORDS or event.get_extra("lolhelper_handled"):
            return
        if event.is_at_or_wake_command and not explicit:
            return  # AstrBot's own command matching handles this one
        handler = self._match_command(event, body)
        if handler is None:
            return
        event.set_extra("lolhelper_handled", True)
        original = event.message_str
        event.message_str, event.is_at_or_wake_command = body, True
        try:
            async for result in getattr(self, handler.handler_name)(event):
                yield result
        finally:
            event.message_str = original
        event.stop_event()

    def _match_command(self, event: AstrMessageEvent, body: str):
        """The plugin's own sub-command handler matching the text, honouring permissions and WebUI switches."""
        from astrbot.core.star.star_handler import star_handlers_registry

        get_config = getattr(self.context, "get_config", None)
        cfg = get_config(event.unified_msg_origin) if get_config else self.config
        original, wake = event.message_str, event.is_at_or_wake_command
        event.message_str, event.is_at_or_wake_command = body, True
        try:
            for h in star_handlers_registry:
                if h.handler_module_path != self.__module__ or not h.enabled or h.handler_name == "compat_entry":
                    continue
                kinds = {type(f).__name__ for f in h.event_filters}
                if "CommandFilter" not in kinds or "CommandGroupFilter" in kinds:
                    continue
                try:
                    if all(f.filter(event, cfg) for f in h.event_filters):
                        return h
                except Exception:  # a filter we cannot evaluate here
                    continue
        finally:
            event.message_str, event.is_at_or_wake_command = original, wake
        return None

    # ============================================================ LLM tools
    @filter.llm_tool(name="lolhelper_champion_build")
    async def llm_champion(self, event: AstrMessageEvent, champion: str, position: str = "") -> str:
        """查询英雄联盟英雄在当前版本的出装、符文、技能加点和克制关系（数据来自 OP.GG）。

        Args:
            champion(string): 英雄名，中文名、称号、外号或英文名均可，例如 亚索、疾风剑豪、Yasuo
            position(string): 位置，可选 上单/打野/中单/下路/辅助，不确定时留空
        """
        pos, _ = pick_position([position]) if position else (None, [])
        try:
            return self.f.text(await self.f.champion(champion, pos))
        except LolHelperError as exc:
            return f"查询失败：{exc}"

    @filter.llm_tool(name="lolhelper_summoner")
    async def llm_summoner(self, event: AstrMessageEvent, riot_id: str, region: str = "") -> str:
        """查询英雄联盟召唤师的段位和最近对局。外服通过 OP.GG，国服需要大区名（如 艾欧尼亚）。

        Args:
            riot_id(string): 召唤师 Riot ID，格式 名字#标签，例如 Hide on bush#KR1
            region(string): 大区，例如 kr、na、euw、jp，或国服大区名 艾欧尼亚；不确定时留空
        """
        try:
            words = [riot_id] + ([region] if region else [])
            acc = await self.f.account_from_target(parse_target(words, self.settings.cn_servers_extra))
            return self.f.text(await self.f.summoner(acc, 5, detail=True))
        except LolHelperError as exc:
            return f"查询失败：{exc}"

    @filter.llm_tool(name="lolhelper_tier_list")
    async def llm_tier(self, event: AstrMessageEvent, position: str = "") -> str:
        """查询英雄联盟当前版本各位置的强势英雄梯队（OP.GG 单双排数据）。

        Args:
            position(string): 位置，可选 上单/打野/中单/下路/辅助，留空返回全部位置前五
        """
        pos, _ = pick_position([position]) if position else (None, [])
        try:
            return self.f.text(await self.f.tier(pos))
        except LolHelperError as exc:
            return f"查询失败：{exc}"

    @filter.llm_tool(name="lolhelper_tft_meta")
    async def llm_tft(self, event: AstrMessageEvent, limit: int = 8) -> str:
        """查询云顶之弈当前版本的强势阵容（OP.GG 数据），包含均名、前四率和核心棋子。

        Args:
            limit(number): 返回的阵容数量，默认 8
        """
        try:
            return self.f.text(await self.f.tft.meta(int(limit or 8)))
        except LolHelperError as exc:
            return f"查询失败：{exc}"

    @filter.llm_tool(name="lolhelper_esports")
    async def llm_esports(self, event: AstrMessageEvent, mode: str = "schedule", league: str = "") -> str:
        """查询英雄联盟电竞赛程或赛果。

        Args:
            mode(string): schedule 表示赛程，result 表示赛果
            league(string): 联赛，可用空格分隔多个，例如 "worlds lpl"，留空表示全部
        """
        lg, _ = parse_leagues(league.replace(",", " ").split()) if league else (None, [])
        try:
            return self.f.text(await self.f.esports("result" if mode == "result" else "schedule", lg or None))
        except LolHelperError as exc:
            return f"查询失败：{exc}"

