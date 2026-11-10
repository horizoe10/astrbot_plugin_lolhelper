"""AstrBot 联盟助手 (lolhelper) — LoL / TFT data with text, Markdown and image output."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time

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
from .lolhelper.ai import AiReviewer
from .lolhelper.group import GroupManager
from .lolhelper.doc import Cell as Comp_cell
from .lolhelper.wegame import parse_cookie
from .lolhelper.wegame_login import DONE, EXPIRED, FAILED, SCANNED, WeChatQrLogin, WeGameQrLogin
from .lolhelper import guide

WG_CHECK_EVERY = 1800  # seconds between WeGame credential checks (also keeps it in use)


def local_dt(ts: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ts))


def fmt_span(sec: float) -> str:
    h = sec / 3600
    return f"{h / 24:.1f} 天" if h >= 48 else (f"{h:.1f} 小时" if h >= 1 else f"{max(1, round(sec / 60))} 分钟")


# first words that start a plugin command (group names and aliases)
ENTRY_WORDS = {"lol", "tft", "云顶"}

MODE_ZH = {"text": "纯文本", "markdown": "Markdown", "image": "图片"}


def _as_markdown(result):
    """Mark a result as Markdown on AstrBot versions that support it (e.g. QQ official)."""
    fn = getattr(result, "use_markdown", None)
    return fn(True) if callable(fn) else result


_button_action = getattr(filter, "button_action", None)


def _on_button(fn):
    """Register fn for button clicks on AstrBot versions with portable buttons (4.29+); a plain method otherwise."""
    return _button_action()(fn) if callable(_button_action) else fn


class _BareGroup:
    """Custom filter on the /lol and /tft groups: a bare "/lol" no longer matches the group (AstrBot would answer with
    its whole command tree), so compat_entry can show the help menu instead. Sub-commands are not affected."""

    def __init__(self, group):
        self.group = group

    def filter(self, event, cfg) -> bool:
        return not self.group.equals(event.message_str.strip())


class LolHelperPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        self.settings = Settings.from_config(dict(self.config))
        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.f = Facade(self.settings, data_dir, html_render=self.html_render, logger=logger)
        self.push = PushManager(self.f, self._push_send, logger)
        self._wg_login_busy = False
        self.push.hooks.append(self._wegame_tick)
        self.ai = AiReviewer(self._llm, logger)
        self._ai_last: dict[str, float] = {}
        self.push.ai_hook = self._ai_push
        self.groups = GroupManager(self.f, self._push_send, self._send_chain, logger)
        self.groups.ai_comment = self._group_ai
        self.push.hooks.append(self.groups.tick)
        self._menu_wait: dict[str, float] = {}  # "umo|sender" -> until when a bare number opens a menu category
        self._cmd_cache: list[tuple[str, str, set]] | None = None
        for grp in (type(self).lol, type(self).tft):
            gf = getattr(grp, "parent_group", None)
            if gf is not None and hasattr(gf, "add_custom_filter") and not any(
                    isinstance(x, _BareGroup) for x in getattr(gf, "custom_filter_list", [])):
                gf.add_custom_filter(_BareGroup(gf))

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
        if acc:
            self._group_touch(event, user if mention else None)
        if not acc:
            who = "对方" if mention else "你"
            raise UsageError(f"{who}还没有绑定账号。用法：/lol 绑定 名字#标签 [大区]，或直接 /lol 战绩 名字#标签 大区")
        return acc

    # ================================================================ guide
    def _guide_ctx(self, event: AstrMessageEvent) -> guide.Ctx:
        s = self.settings
        acc = self.f.store.default_account(self._user_key(event))
        try:
            admin = bool(event.is_admin())
        except Exception:
            admin = False
        return guide.Ctx(version=VERSION, account=account_label(acc) if acc else "", in_group=not event.is_private_chat(),
                         ai=s.ai_enabled, group_play=s.group_enabled, cn=s.cn_enabled, admin=admin, push=s.push_enabled)

    def _usage(self, event: AstrMessageEvent, name: str, group: str = "lol", extra: str = ""):
        return self._plain(event, guide.usage_text(name, group, extra))

    def _commands(self) -> list[tuple[str, str, set]]:
        """(group, name, aliases) of this plugin's sub-commands, read from AstrBot's registry."""
        if self._cmd_cache is None:
            from astrbot.core.star.star_handler import star_handlers_registry

            out = []
            for h in star_handlers_registry:
                if h.handler_module_path != self.__module__:
                    continue
                for f in h.event_filters:
                    if type(f).__name__ != "CommandFilter":
                        continue
                    parents = " ".join(str(x) for x in (getattr(f, "parent_command_names", None) or []))
                    grp = "tft" if re.search(r"(^|\s)(tft|云顶)(\s|$)", parents) else "lol"
                    out.append((grp, f.command_name, set(getattr(f, "alias", None) or ())))
            self._cmd_cache = out
        return self._cmd_cache

    def _command_words(self, group: str = "lol") -> dict[str, str]:
        """Every spelling of the group's sub-commands (names and aliases) -> the command name."""
        out: dict[str, str] = {}
        for grp, name, aliases in self._commands():
            if grp == group:
                out[name] = name
                out.update({a: name for a in aliases})
        return out

    def _aliases(self, cmd: guide.Cmd) -> list[str]:
        return sorted(next((a for g, n, a in self._commands() if g == cmd.group and n == cmd.name), set()))

    def _menu_key(self, event: AstrMessageEvent) -> str:
        return f"{event.unified_msg_origin}|{event.get_sender_id()}"

    def _buttons_ok(self, event: AstrMessageEvent) -> bool:
        return (hasattr(Comp, "ActionRow") and hasattr(Comp, "Button") and callable(_button_action)
                and event.get_platform_name() in self.settings.button_platforms)

    def _button_rows(self, ctx: guide.Ctx, current: str | None) -> list:
        rows = []
        for row in guide.button_rows(ctx, current):
            rows.append(Comp.ActionRow(buttons=[
                Comp.Button(id=f"lolhelper:menu:{key}", label=label, action=Comp.Button.CallbackAction(data={"k": key}),
                            style=Comp.Button.Style.PRIMARY if key == "main" else Comp.Button.Style.DEFAULT)
                for key, label in row]))
        if rows:
            rows[0].fallback_text = "回复序号或发送 /lol 帮助 <序号> 查看分类"
        return rows

    async def _menu_out(self, event: AstrMessageEvent, text: str, ctx: guide.Ctx, current: str | None = None):
        """A menu message: remembers the asker for number replies and adds category buttons where supported.
        Returns the result to yield, or None when it was already sent with buttons."""
        now = time.time()
        if len(self._menu_wait) > 200:
            self._menu_wait = {k: v for k, v in self._menu_wait.items() if v > now}
        self._menu_wait[self._menu_key(event)] = now + guide.REPLY_SECS
        if self._buttons_ok(event):
            try:
                await event.send(MessageChain(chain=[Comp.Plain(text), *self._button_rows(ctx, current)]))
                return None
            except Exception as exc:
                logger.warning(f"[lolhelper] 发送按钮菜单失败，改发纯文本：{exc}")
        return self._plain(event, text)

    async def _help(self, event: AstrMessageEvent, arg: str, mode: str | None = None, bare: bool = False, group: str = "lol"):
        ctx = self._guide_ctx(event)
        res = guide.resolve(arg, ctx, group)
        if res is None:
            names = {c.name: c.name for _, c in guide.all_commands()}
            hits = guide.suggest(arg, names)
            tip = ("，你是不是想找：" + " · ".join(f"/lol 帮助 {h}" for h in hits)) if hits else ""
            return self._plain(event, f"帮助里没有「{arg}」{tip}\n发送 /lol 帮助 查看主菜单")
        if mode in ("image", "markdown") and res[0] in ("main", "all"):
            return await self._reply(event, View("help_menu", {}, guide.menu_doc(ctx), time.time()), mode)
        if res[0] == "main":
            return await self._menu_out(event, guide.main_menu(ctx, bare), ctx)
        if res[0] == "all":
            return self._plain(event, guide.full_text(ctx))
        if res[0] == "cat":
            return await self._menu_out(event, guide.category_text(res[1], ctx), ctx, res[1].key)
        return self._plain(event, guide.command_text(res[1], res[2], ctx, self._aliases(res[2])))

    @_on_button
    async def menu_button(self, event: AstrMessageEvent):
        """帮助菜单的分类按钮"""
        it = event.get_button_interaction() if hasattr(event, "get_button_interaction") else None
        action = str(getattr(it, "action_id", "") or "")
        if not action.startswith("lolhelper:menu:"):
            return
        key = action.rsplit(":", 1)[-1]
        ctx = self._guide_ctx(event)
        cat = next((c for c in guide.CATEGORIES if c.key == key), None)
        if cat is None:
            r = await self._menu_out(event, guide.main_menu(ctx), ctx)
        else:
            r = await self._menu_out(event, guide.category_text(cat, ctx), ctx, cat.key)
        if r is not None:
            yield r
        event.stop_event()

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
            yield self._usage(event, "英雄")
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
            yield self._usage(event, "海克斯")
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

    # ============================================================ AI review
    async def _llm(self, system: str, prompt: str, umo) -> tuple[str, str]:
        pid = self.settings.ai_provider
        if not pid:
            try:
                pid = await self.context.get_current_chat_provider_id(umo) if umo else ""
            except Exception:
                pid = ""
        if not pid:
            prov = self.context.get_using_provider()
            pid = prov.meta().id if prov else ""
        if not pid:
            raise UsageError("没有可用的大模型，请先在 AstrBot 配置聊天模型，或在插件配置「AI 点评 → 使用的模型」中选择")
        resp = await self.context.llm_generate(chat_provider_id=pid, prompt=prompt, system_prompt=system)
        model = pid
        try:
            prov = self.context.get_provider_by_id(pid)
            model = (prov.meta().model if prov else "") or pid
        except Exception:
            pass
        return (getattr(resp, "completion_text", "") or ""), str(model)

    def _ai_gate(self, user: str):
        def gate() -> None:
            wait = self.settings.ai_cooldown - (time.time() - self._ai_last.get(user, 0))
            if wait > 0:
                raise UsageError(f"AI 点评冷却中，{int(wait) + 1} 秒后再试")
            self._ai_last[user] = time.time()
        return gate

    def _ai_ready(self, event: AstrMessageEvent):
        if not self.settings.ai_enabled:
            return self._plain(event, "AI 点评未开启，可在插件配置「AI 点评 → 启用 AI 点评」中开启")
        return None

    async def _ai_reply(self, event: AstrMessageEvent, make, mode: str | None):
        """Run a review job with a short notice first when the model is called (5~20 s)."""
        try:
            view = await make()
        except LolHelperError as exc:
            return event.plain_result(f"⚠️ {exc}").use_t2i(False)
        except Exception as exc:
            logger.exception("[lolhelper] AI 点评出错")
            return event.plain_result(f"⚠️ AI 点评失败：{type(exc).__name__}").use_t2i(False)
        return await self._reply(event, view, mode)

    @lol.command("点评", alias={"锐评", "评价", "review"})
    async def lol_review(self, event: AstrMessageEvent):
        """AI 点评单局：/lol 点评 [序号] [名字#标签] [大区] [专业|幽默|锐评]"""
        from .lolhelper.ai import attach, pick_style

        if (r := self._ai_ready(event)) is not None:
            yield r
            return
        head = (event.get_message_str() or "").split()
        a = parse_args(self._rest(event, {"点评", "锐评", "评价", "review"}))
        style, words = pick_style(a.words)
        if style is None and any(w.lstrip("/") == "锐评" for w in head[:3]):
            style = "锐评"
        style = style or self.settings.ai_style
        index = 1
        if words and words[0].isdigit():
            index = int(words.pop(0))
        elif words and words[-1].isdigit() and "#" not in words[-1]:
            index = int(words.pop())
        yield self._plain(event, f"🤖 正在用「{style}」风格点评第 {index} 局，稍等几秒…")

        async def make():
            acc = await self._account_for(event, words)
            key, brief, game = await self.f.ai_game_brief(acc, index)
            out = await self.ai.review("game", key, brief, style, event.unified_msg_origin, self._ai_gate(self._user_key(event)))
            from .lolhelper.game import build_game_view

            view = build_game_view(game, self.f.gd, "cn" if acc.get("kind") == "cn" else "lol", self.settings.timezone)
            view.doc.title = view.doc.title.replace("对局详情", "AI 点评")
            view.doc.badges.insert(0, Comp_cell(f"🤖 AI 点评 · {style}", "accent"))
            view.doc.tips = []
            view.doc.tip(f"/lol 点评 {index} {'锐评' if style != '锐评' else '幽默'}", "换个风格")
            view.doc.tip("/lol 诊断", "近期战绩诊断")
            attach(view.doc, out)
            view.doc.sections.insert(0, view.doc.sections.pop())  # review first, then the 10 players
            return view

        yield await self._ai_reply(event, make, a.mode)

    @lol.command("诊断", alias={"体检", "近况", "diagnose"})
    async def lol_diagnose(self, event: AstrMessageEvent):
        """AI 诊断近期战绩：/lol 诊断 [名字#标签] [大区] [局数] [专业|幽默|锐评]"""
        from .lolhelper.ai import attach, pick_style

        if (r := self._ai_ready(event)) is not None:
            yield r
            return
        a = parse_args(self._rest(event, {"诊断", "体检", "近况", "diagnose"}))
        style, words = pick_style(a.words)
        style = style or self.settings.ai_style
        count = a.count or self.settings.ai_recent_games
        nums = [w for w in words if w.isdigit() and "#" not in w]
        if nums:
            count = int(nums[-1])
            words = [w for w in words if w != nums[-1]]
        count = max(5, min(count, 20))
        yield self._plain(event, f"🤖 正在分析最近 {count} 局（{style}风格），稍等几秒…")

        async def make():
            acc = await self._account_for(event, words)
            key, brief, rows, st = await self.f.ai_recent_brief(acc, count)
            out = await self.ai.review("recent", key, brief, style, event.unified_msg_origin, self._ai_gate(self._user_key(event)))
            view = self._diagnose_view(acc, rows, st)
            attach(view.doc, out)
            view.doc.sections.insert(0, view.doc.sections.pop())
            return view

        yield await self._ai_reply(event, make, a.mode)

    def _diagnose_view(self, acc: dict, rows: list[dict], st: dict) -> View:
        from .lolhelper.ai import METRICS, fmt_metric, metric
        from .lolhelper.constants import cn_server_name
        from .lolhelper.doc import Cell, Doc

        gd = self.f.gd
        who = f"{acc.get('game_name')}#{acc.get('tag_line')}"
        doc = Doc(title=f"{who} · 近期诊断", subtitle=f"{account_label(acc)} · 最近 {st['n']} 局 {st['wins']} 胜 {st['n'] - st['wins']} 负",
                  theme="cn" if acc.get("kind") == "cn" else "lol", width=820)
        top = st["champs"][0][1] if st["champs"] else None
        top_c = gd.champion(top["id"]) if top and top.get("id") else None
        doc.icon = gd.champ_icon(top_c) if top_c else None
        n, kind = st["streak"]
        if n >= 2:
            doc.badge(f"{n} 连{'胜' if kind else '败'}", "win" if kind else "lose")
        wr_all = st["metrics"]["winrate"][0] or 0
        doc.badge(f"胜率 {fmt_metric('winrate', wr_all)}", "hx " + ("win" if st["wins"] * 2 >= st["n"] else "lose"))
        doc.badge(f"KDA {fmt_metric('kda', st['metrics']['kda'][0])}", "hx")
        # banner: most played champion's art, win rate on the side, averages in the strip
        if acc.get("kind") == "cn":
            chips = [{"text": "国服", "cls": "acc"}] + ([{"text": cn_server_name(acc["server"])}] if acc.get("server") else [])
        else:
            chips = [{"text": OPGG_REGIONS.get(acc.get("region", ""), acc.get("region", "")), "cls": "acc"}]
        chips.append({"text": "近期诊断"})
        stats = [{"parts": [[str(st["wins"]), "胜"], [str(st["n"] - st["wins"]), "负"]], "k": f"近 {st['n']} 场"}]
        for key, label in (("kda", "KDA"), ("kp", "参团率"), ("dmg_share", "伤害占比"), ("score", "平均评分")):
            if st["metrics"][key][0] is not None:
                stats.append({"v": fmt_metric(key, st["metrics"][key][0]), "k": label})
        icons = [gd.champ_icon(gd.champion(c["id"])) for _, c in st["champs"][:3] if c.get("id")]
        if any(icons):
            stats.append({"icons": [u for u in icons if u], "k": "常用英雄"})
        doc.hero = {"art": gd.splash(top_c), "icon": doc.icon, "name": acc.get("game_name") or "?",
                    "tag": f"#{acc['tag_line']}" if acc.get("tag_line") else "", "chips": chips, "stats": stats[:6],
                    "side": {"kind": "big", "small": "近期胜率", "big": fmt_metric("winrate", wr_all), "cls": "win" if wr_all >= 0.5 else "lose",
                             "sub": f"最近 5 局 {fmt_metric('winrate', st['metrics']['winrate'][1])}"}}
        bars, lines = [], []
        for key, label, top_v, higher, _ in METRICS:
            v_all, v_recent = st["metrics"][key]
            if v_all is None:
                continue
            cls = ""
            if v_recent is not None and abs(v_recent - v_all) > 0.05 * max(abs(v_all), 0.01):
                cls = "up" if (v_recent > v_all) == higher else "down"
            bars.append({"label": label, "all": fmt_metric(key, v_all), "recent": fmt_metric(key, v_recent), "cls": cls,
                         "w_all": round(min(v_all / top_v, 1) * 100), "w_recent": round(min((v_recent or 0) / top_v, 1) * 100)})
            lines.append(f"{label}：整体 {fmt_metric(key, v_all)} · 最近5局 {fmt_metric(key, v_recent)}")
        doc.add_block("数据概览", "form", {"rows": bars, "streak": [r["win"] for r in rows][:20]}, lines)
        champ_rows = []
        for name, c in st["champs"][:6]:
            rs = c["rows"]
            wr = metric(rs, "winrate")
            champ_rows.append([Cell(text=name, img=self.f.gd.champ_icon(self.f.gd.champion(c["id"])) if c.get("id") else None),
                               f"{len(rs)} 局", Cell(text=fmt_metric("winrate", wr), cls="win" if wr and wr >= 0.5 else "lose"),
                               fmt_metric("kda", metric(rs, "kda")), fmt_metric("kp", metric(rs, "kp"))])
        doc.add_table("常用英雄", ["英雄", "场次", "胜率", "KDA", "参团率"], champ_rows, widths=["34%", "16%", "16%", "16%", "18%"])
        doc.footer = f"数据更新于 {time.strftime('%m-%d %H:%M')}"
        doc.tip("/lol 点评", "AI 点评最近一局")
        doc.tip("/lol 战绩", "最近战绩")
        return View("lol_diagnose", {"account": acc, "rows": rows}, doc, time.time())

    @lol.command("赛况点评", alias={"比赛点评", "赛事点评"})
    async def lol_match_review(self, event: AstrMessageEvent):
        """AI 点评比赛：/lol 赛况点评 [联赛] [序号|战队] [局号] [专业|幽默|锐评]，不写局号点评整场"""
        from .lolhelper.ai import attach, pick_style
        from .lolhelper.esports import build_esports_game_view

        if (r := self._ai_ready(event)) is not None:
            yield r
            return
        a = parse_args(self._rest(event, {"赛况点评", "比赛点评", "赛事点评"}))
        style, words = pick_style(a.words)
        style = style or self.settings.ai_style
        leagues, index, team, match_id, game_no = self._match_args(event, words)
        yield self._plain(event, f"🤖 正在用「{style}」风格点评{'第 ' + str(game_no) + ' 局' if game_no else '整场比赛'}，稍等几秒…")

        async def make():
            g, games, created = await self.f.ai_esports(leagues, index, team, match_id, game_no)
            gate = self._ai_gate(self._user_key(event))
            if game_no:
                game = next((x for x in games if x.get("set") == game_no), None)
                if not game:
                    raise UsageError(f"第 {game_no} 局暂无数据（本场共有 {len(games)} 局数据）")
                key, brief = self.f.ai_esports_game_brief(g, game)
                out = await self.ai.review("esports_game", key, brief, style, event.unified_msg_origin, gate)
                view = build_esports_game_view(g, game, self.f.gd, self.settings.timezone, created, games)
            else:
                key, brief = self.f.ai_series_brief(g, games)
                out = await self.ai.review("series", key, brief, style, event.unified_msg_origin, gate)
                h = (g.get("homeTeam") or {}).get("acronym") or ""
                view = await self.f.svc.esports_match(g, key=h)
            attach(view.doc, out)
            view.doc.sections.insert(0, view.doc.sections.pop())
            return view

        yield await self._ai_reply(event, make, a.mode)

    async def _ai_push(self, kind: str, view: View, ctx: dict) -> None:
        """PushManager hook: add a review to match / esports push cards when enabled in the config."""
        from .lolhelper.ai import attach

        s = self.settings
        if not s.ai_enabled:
            return
        if kind == "match":
            if not s.ai_push_match:
                return
            acc, m = ctx["account"], ctx["match"]
            game = (view.data or {}).get("game")
            key, brief, _ = await self.f.ai_game_brief(acc, 1, game, m) if game else await self.f.ai_game_brief(acc, 1)
            out = await self.ai.review("game", key, brief, s.ai_style, None)
        elif kind in ("esports_game", "series"):
            if not s.ai_push_esports:
                return
            if kind == "esports_game":
                key, brief = self.f.ai_esports_game_brief(ctx["match"], ctx["game"])
            else:
                if not ctx.get("games"):
                    return
                key, brief = self.f.ai_series_brief(ctx["match"], ctx["games"])
            out = await self.ai.review(kind, key, brief, s.ai_style, None)
        else:
            return
        attach(view.doc, out)


    # ============================================================ group play
    def _in_group(self, event: AstrMessageEvent) -> bool:
        return self.settings.group_enabled and not event.is_private_chat()

    def _group_touch(self, event: AstrMessageEvent, user: str | None = None) -> None:
        if self._in_group(event):
            uid = user or self._user_key(event)
            nick = event.get_sender_name() if user is None else ""
            if self.f.store.default_account(uid):
                self.groups.touch(event.unified_msg_origin, uid, nick or "")

    def _group_only(self, event: AstrMessageEvent):
        if not self.settings.group_enabled:
            return self._plain(event, "群玩法未开启，可在插件配置「群玩法 → 启用群玩法」中开启")
        if event.is_private_chat():
            return self._plain(event, "这是群聊玩法，请在群里使用")
        return None

    def _mentions(self, event: AstrMessageEvent) -> list[tuple[str, str]]:
        self_id = str(getattr(event.message_obj, "self_id", "") or "")
        out = []
        for comp in event.message_obj.message or []:
            if isinstance(comp, Comp.At) and str(comp.qq) not in (self_id, "all"):
                out.append((str(comp.qq), str(getattr(comp, "name", "") or "")))
        return out

    async def _send_chain(self, umo: str, text: str, image: str | None) -> None:
        chain = MessageChain([Comp.Plain(text)])
        if image:
            chain.chain.append(Comp.Image.fromURL(image) if image.startswith("http") else Comp.Image.fromFileSystem(image))
        chain.use_t2i(False)
        await self.context.send_message(umo, chain)

    async def _group_ai(self, kind: str, key: str, brief: str, style: str):
        if not self.settings.ai_enabled:
            return None
        return await self.ai.review(kind, key, brief, style, None)

    @lol.command("日榜", alias={"今日榜", "daily"})
    async def lol_daily_board(self, event: AstrMessageEvent):
        """群日榜：/lol 日榜 [昨天]"""
        if (r := self._group_only(event)) is not None:
            yield r
            return
        a = parse_args(self._rest(event, {"日榜", "今日榜", "daily"}))
        offset = 1 if any(w in ("昨天", "昨日") for w in a.words) else 0
        yield await self._board(event, "day", offset, a.mode)

    @lol.command("周榜", alias={"weekly"})
    async def lol_weekly_board(self, event: AstrMessageEvent):
        """群周榜：/lol 周榜 [上周]"""
        if (r := self._group_only(event)) is not None:
            yield r
            return
        a = parse_args(self._rest(event, {"周榜", "weekly"}))
        offset = 1 if any(w in ("上周", "上週") for w in a.words) else 0
        yield await self._board(event, "week", offset, a.mode)

    async def _board(self, event: AstrMessageEvent, kind: str, offset: int, mode: str | None):
        self._group_touch(event)
        umo = event.unified_msg_origin
        if not self.groups.members(umo):
            return self._plain(event, "本群还没有群友上榜。群友在群里发送 /lol 绑定 名字#标签 后会自动加入榜单")

        async def make():
            view = await self.groups.board_view(umo, kind, offset, with_ranks=kind == "day" and self.settings.group_rank_change)
            if view is None:
                when = {("day", 0): "今天", ("day", 1): "昨天", ("week", 0): "本周", ("week", 1): "上周"}[(kind, offset)]
                raise UsageError(f"{when}还没有群友打过排位或匹配，没法出榜")
            return view

        return await self._answer(event, make, mode)

    @lol.command("榜单", alias={"排行榜", "排行"})
    async def lol_boards(self, event: AstrMessageEvent):
        """群榜单：/lol 榜单 [日|周|竞猜|猜英雄] [昨天|上周]"""
        if (r := self._group_only(event)) is not None:
            yield r
            return
        a = parse_args(self._rest(event, {"榜单", "排行榜", "排行"}))
        umo = event.unified_msg_origin
        kind = "day"
        for w in a.words:
            if w in ("周", "周榜", "本周", "上周", "上週", "week"):
                kind = "week"
            elif w in ("竞猜", "竞猜榜", "押注", "bet"):
                kind = "bet"
            elif w in ("猜英雄", "猜英雄榜", "答题", "quiz"):
                kind = "quiz"
        if kind == "bet":
            yield await self._answer(event, lambda: self._async(self.groups.bet_board_view(umo)), a.mode)
        elif kind == "quiz":
            yield await self._answer(event, lambda: self._async(self.groups.quiz_board_view(umo)), a.mode)
        else:
            offset = 1 if any(w in ("昨天", "昨日", "上周", "上週") for w in a.words) else 0
            yield await self._board(event, kind, offset, a.mode)

    @lol.command("上榜", alias={"加入榜单"})
    async def lol_join_board(self, event: AstrMessageEvent):
        """加入本群榜单与群友播报"""
        if (r := self._group_only(event)) is not None:
            yield r
            return
        user = self._user_key(event)
        if not self.f.store.default_account(user):
            yield self._plain(event, "你还没有绑定账号，先发送 /lol 绑定 名字#标签 [大区]")
            return
        self.groups.opt(event.unified_msg_origin, user, True, event.get_sender_name() or "")
        yield self._plain(event, "✅ 已加入本群榜单，日榜、周榜和群友播报会包含你的对局")

    @lol.command("下榜", alias={"退出榜单"})
    async def lol_leave_board(self, event: AstrMessageEvent):
        """退出本群榜单与群友播报"""
        if (r := self._group_only(event)) is not None:
            yield r
            return
        self.groups.opt(event.unified_msg_origin, self._user_key(event), False)
        yield self._plain(event, "✅ 已退出本群榜单，之后的榜单和群友播报都不会包含你。想回来发送 /lol 上榜")

    @lol.command("群成员", alias={"榜单成员"})
    async def lol_group_members(self, event: AstrMessageEvent):
        """查看本群上榜的群友"""
        if (r := self._group_only(event)) is not None:
            yield r
            return
        self._group_touch(event)
        umo = event.unified_msg_origin
        ms = self.groups.members(umo)
        if not ms:
            yield self._plain(event, "本群还没有群友上榜。群友在群里发送 /lol 绑定 名字#标签 后会自动加入")
            return
        lines = [f"本群上榜群友 {len(ms)} 人（群友播报：{'开' if self.groups.broadcast_on(umo) else '关'}）："]
        lines += [f"{i}. {self.groups.label(umo, u, a)} · {account_label(a)}" for i, (u, a) in enumerate(ms, 1)]
        lines.append("不想上榜发送 /lol 下榜")
        yield self._plain(event, "\n".join(lines))

    @lol.command("群播报", alias={"群友播报"})
    async def lol_group_broadcast(self, event: AstrMessageEvent):
        """开关本群的群友播报：/lol 群播报 开|关"""
        if (r := self._group_only(event)) is not None:
            yield r
            return
        a = parse_args(self._rest(event, {"群播报", "群友播报"}))
        word = (a.words[0] if a.words else "").lower()
        umo = event.unified_msg_origin
        if word not in ("开", "关", "on", "off", "开启", "关闭"):
            state = "开" if self.groups.broadcast_on(umo) else "关"
            yield self._plain(event, f"本群群友播报当前为「{state}」。用法：/lol 群播报 开|关\n播报内容：群友一起开黑、五杀 / 超神 / 惨案、连胜连败")
            return
        if not self._can_subscribe(event):
            yield self._plain(event, "本群仅管理员可以修改播报设置")
            return
        on = word in ("开", "on", "开启")
        self.groups.group(umo)["broadcast"] = on
        self.groups.save()
        yield self._plain(event, f"✅ 本群群友播报已{'开启' if on else '关闭'}")

    @lol.command("对比", alias={"PK", "pk", "比一比"})
    async def lol_pk(self, event: AstrMessageEvent):
        """群友 PK：/lol 对比 @甲 [@乙]，或写 名字#标签；只写一个人时和自己比"""
        from .lolhelper.ai import attach, pick_style
        from .lolhelper.group import pk_view

        a = parse_args(self._rest(event, {"对比", "PK", "pk", "比一比"}))
        style, words = pick_style(a.words)
        mentions = self._mentions(event)
        ids = [w for w in words if "#" in w or "＃" in w]
        targets: list[tuple[str, dict]] = []
        try:
            for qq, name in mentions[:2]:
                acc = self.f.store.default_account(self._user_key(event, qq))
                if not acc:
                    raise UsageError(f"{name or qq} 还没有绑定账号")
                targets.append((self.groups.nick(event.unified_msg_origin, self._user_key(event, qq)) or acc["game_name"], acc))
            i = 0
            while len(targets) < 2 and i < len(ids):
                rest = words[words.index(ids[i]):]
                acc = await self.f.account_from_target(parse_target(rest[:2] if len(rest) > 1 and "#" not in rest[1] else rest[:1],
                                                                   self.settings.cn_servers_extra))
                targets.append((acc["game_name"], acc))
                i += 1
            if len(targets) == 1:
                me = self.f.store.default_account(self._user_key(event))
                if not me:
                    raise UsageError("你还没有绑定账号，或者写两个人：/lol 对比 @甲 @乙")
                targets.insert(0, (event.get_sender_name() or me["game_name"], me))
        except LolHelperError as exc:
            yield self._plain(event, f"⚠️ {exc}")
            return
        if len(targets) < 2:
            yield self._usage(event, "对比", extra="也可以写 名字#标签")
            return
        self._group_touch(event)
        use_ai = self.settings.ai_enabled
        yield self._plain(event, f"⚔️ 正在对比 {targets[0][0]} 和 {targets[1][0]} 的近期数据{'，并请 AI 当裁判' if use_ai else ''}…")

        async def make():
            sides = []
            for label, acc in targets:
                key, brief, rows, st = await self.f.ai_recent_brief(acc, 15)
                sides.append({"label": label, "acc": acc, "rows": rows, "st": st, "key": key, "brief": brief})
            view = pk_view(sides[0], sides[1], self.f.gd)
            if use_ai:
                brief = "两位玩家近期数据对比，判断谁更强并点评：\n\n" + "\n\n".join(f"【{x['label']}】\n{x['brief']}" for x in sides)
                try:
                    out = await self.ai.review("pk", f"{sides[0]['key']}|{sides[1]['key']}", brief, style or "锐评",
                                               event.unified_msg_origin, self._ai_gate(self._user_key(event)))
                    attach(view.doc, out)
                    view.doc.sections[-1].title = "AI 裁判"
                except LolHelperError as exc:
                    view.doc.footer = f"AI 裁判暂不可用：{exc}"
            return view

        yield await self._answer(event, make, a.mode)

    @lol.command("竞猜", alias={"押注", "bet"})
    async def lol_bet(self, event: AstrMessageEvent):
        """赛事竞猜：/lol 竞猜 查看可竞猜的比赛；/lol 竞猜 <序号|战队> [战队] [比分]"""
        if (r := self._group_only(event)) is not None:
            yield r
            return
        a = parse_args(self._rest(event, {"竞猜", "押注", "bet"}))
        umo = event.unified_msg_origin
        leagues, words = parse_leagues(a.words)
        leagues = leagues or self.f.sub_leagues(umo)
        if not words:
            async def make():
                games = await self.groups.upcoming(leagues)
                if not games:
                    raise UsageError("未来 3 天没有可竞猜的比赛" + ("（按本群订阅的赛事筛选，可写赛事名，例如 /lol 竞猜 LCK）" if leagues else ""))
                return self.groups.bet_list_view(umo, games)

            yield await self._answer(event, make, a.mode)
            return
        try:
            msg = await self.groups.place_bet(umo, self._user_key(event), event.get_sender_name() or "", words, leagues)
        except LolHelperError as exc:
            msg = f"⚠️ {exc}"
        yield self._plain(event, msg)

    @lol.command("竞猜榜", alias={"押注榜"})
    async def lol_bet_board(self, event: AstrMessageEvent):
        """本群竞猜积分榜"""
        if (r := self._group_only(event)) is not None:
            yield r
            return
        a = parse_args(self._rest(event, {"竞猜榜", "押注榜"}))
        yield await self._answer(event, lambda: self._async(self.groups.bet_board_view(event.unified_msg_origin)), a.mode)

    @staticmethod
    async def _async(value):
        return value

    @lol.command("猜英雄", alias={"猜猜看", "quiz"})
    async def lol_quiz(self, event: AstrMessageEvent):
        """猜英雄小游戏：/lol 猜英雄 [技能|原画|技能名]"""
        if (r := self._group_only(event)) is not None:
            yield r
            return
        a = parse_args(self._rest(event, {"猜英雄", "猜猜看", "quiz"}))
        try:
            text, img = await self.groups.quiz_start(event.unified_msg_origin, a.words[0] if a.words else None)
        except LolHelperError as exc:
            yield self._plain(event, f"⚠️ {exc}")
            return
        chain = [Comp.Plain(text)] + ([Comp.Image.fromFileSystem(img)] if img else [])
        yield event.chain_result(chain).use_t2i(False)

    @lol.command("不猜了", alias={"公布答案", "揭晓"})
    async def lol_quiz_end(self, event: AstrMessageEvent):
        """结束本群的猜英雄并公布答案"""
        q = self.groups.quiz_end(event.unified_msg_origin)
        if not q:
            yield self._plain(event, "本群现在没有进行中的猜英雄，发送 /lol 猜英雄 出题")
            return
        chain = [Comp.Plain(f"答案是 {q['name']}")] + ([Comp.Image.fromURL(q["icon"])] if q.get("icon") else [])
        yield event.chain_result(chain).use_t2i(False)

    @lol.command("猜英雄榜", alias={"答题榜"})
    async def lol_quiz_board(self, event: AstrMessageEvent):
        """本群猜英雄排行榜"""
        if (r := self._group_only(event)) is not None:
            yield r
            return
        a = parse_args(self._rest(event, {"猜英雄榜", "答题榜"}))
        yield await self._answer(event, lambda: self._async(self.groups.quiz_board_view(event.unified_msg_origin)), a.mode)

    @lol.command("今天玩什么", alias={"玩什么", "随机英雄", "roll"})
    async def lol_random(self, event: AstrMessageEvent):
        """随机抽一个当前版本的强势英雄：/lol 今天玩什么 [位置]"""
        a = parse_args(self._rest(event, {"今天玩什么", "玩什么", "随机英雄", "roll"}))
        pos, _ = pick_position(a.words)

        async def make():
            name, p, row = await self.groups.random_pick(pos)
            view = await self.f.champion(name, p)
            tier = row.get("tier")
            view.doc.title = f"🎰 今天玩 {view.doc.title}"
            view.doc.badges.insert(0, Comp_cell(f"随机抽中 · {POSITIONS.get(p, p)} T{tier}" if tier else f"随机抽中 · {POSITIONS.get(p, p)}", "accent"))
            view.doc.tips = []
            view.doc.tip(f"/lol 今天玩什么 {POSITIONS.get(p, '')}".strip(), "不满意？再抽一次")
            view.doc.tip("/lol 今天玩什么", "随机位置")
            return view

        yield await self._answer(event, make, a.mode)

    @lol.command("分队", alias={"内战分队", "team"})
    async def lol_split(self, event: AstrMessageEvent):
        """内战分队：/lol 分队 @甲 @乙 …（4~10 人，可混写名字），按单双排段位平衡"""
        a = parse_args(self._rest(event, {"分队", "内战分队", "team"}))
        umo = event.unified_msg_origin
        players: list[dict] = []
        for qq, name in self._mentions(event):
            uid = self._user_key(event, qq)
            acc = self.f.store.default_account(uid)
            label = self.groups.nick(umo, uid) or name or (acc or {}).get("game_name") or qq[-6:]
            players.append({"name": label, "acc": acc})
        for w in a.words:
            if w.startswith("@"):
                continue
            acc = None
            if "#" in w or "＃" in w:
                try:
                    acc = await self.f.account_from_target(parse_target([w], self.settings.cn_servers_extra))
                except LolHelperError:
                    acc = None
            players.append({"name": w.split("#")[0][:12], "acc": acc})
        yield await self._answer(event, lambda: self.groups.split_view(players), a.mode)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=-1)
    async def quiz_answer(self, event: AstrMessageEvent):
        """猜英雄作答：群里有题目时，直接发英雄名就算作答"""
        if not self.groups.quizzes:
            return
        text = (event.get_message_str() or "").strip()
        if not text or len(text) > 12 or text[:1] in ("/", "／"):
            return
        q = self.groups.quiz_check(event.unified_msg_origin, text, self._user_key(event), event.get_sender_name() or "")
        if not q:
            return
        who = event.get_sender_name() or "这位群友"
        chain = [Comp.Plain(f"🎉 {who} 答对了！答案是 {q['name']}，用时 {q['secs']} 秒，累计答对 {q['pts']} 题")]
        if q.get("icon"):
            chain.append(Comp.Image.fromURL(q["icon"]))
        yield event.chain_result(chain).use_t2i(False)
        event.stop_event()


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
        leagues, index, team, match_id, game_no = self._match_args(event, a.words)
        yield await self._answer(event, lambda: self.f.esports_match(leagues, index, team, match_id, game_no), a.mode)

    def _match_args(self, event: AstrMessageEvent, all_words: list[str]):
        words = [w for w in all_words if w.upper() != "AL"]  # AL is both a league and a team; treat it as the team
        leagues, rest = parse_leagues(words)
        rest += [w for w in all_words if w.upper() == "AL"]
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
        return leagues, index, team, match_id, game_no

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
            yield self._usage(event, "积分")
            return
        yield await self._answer(event, lambda: self.f.standings(league), a.mode)

    @lol.command("帮助", alias={"help", "菜单", "menu"})
    async def lol_help(self, event: AstrMessageEvent):
        """帮助菜单：/lol 帮助 [分类|指令|全部]，加 -i 发图片版"""
        a = parse_args(self._rest(event, {"帮助", "help", "菜单", "menu"}))
        r = await self._help(event, a.rest, a.mode)
        if r is not None:
            yield r

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
            yield self._usage(event, "英雄", "tft")
            return
        yield await self._answer(event, lambda: self.f.tft.champion(a.rest), a.mode)

    @tft.command("装备", alias={"item", "items"})
    async def tft_item(self, event: AstrMessageEvent):
        """装备适合谁：/tft 装备 大天使"""
        a = parse_args(self._rest(event, {"装备", "item", "items"}))
        if not a.rest:
            yield self._usage(event, "装备", "tft")
            return
        yield await self._answer(event, lambda: self.f.tft.item(a.rest), a.mode)

    @tft.command("强化", alias={"augment", "海克斯", "符文"})
    async def tft_augment(self, event: AstrMessageEvent):
        """强化符文搜索：/tft 强化 贷款"""
        a = parse_args(self._rest(event, {"强化", "augment", "海克斯", "符文"}))
        yield await self._answer(event, lambda: self.f.tft.augment(a.rest), a.mode)

    @tft.command("帮助", alias={"help", "菜单"})
    async def tft_help(self, event: AstrMessageEvent):
        """云顶之弈指令：/tft 帮助"""
        a = parse_args(self._rest(event, {"帮助", "help", "菜单"}))
        r = await self._help(event, a.rest or "云顶", a.mode, group="tft")
        if r is not None:
            yield r

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
        extra = ""
        if self._in_group(event):
            if self.groups.touch(event.unified_msg_origin, self._user_key(event), event.get_sender_name() or ""):
                extra = "\n🏆 已加入本群榜单（日榜 / 周榜 / 群友播报），不想上榜发送 /lol 下榜"
        return self._plain(event, f"✅ 已绑定 {account_label(acc)}（序号 {idx + 1}，已设为默认）\n发送 /lol 战绩 查看最近对局" + extra)

    @lol.command("绑定", alias={"bind"})
    async def lol_bind(self, event: AstrMessageEvent):
        """绑定账号：/lol 绑定 名字#标签 [大区]"""
        a = parse_args(self._rest(event, {"绑定", "bind"}))
        if not a.words:
            yield self._usage(event, "绑定", extra="国服请用：/lol 绑定国服 名字#编号 [大区]")
            return
        yield await self._bind(event, a.words, force_cn=False)

    @lol.command("绑定国服", alias={"bindcn", "国服绑定"})
    async def lol_bind_cn(self, event: AstrMessageEvent):
        """绑定国服账号：/lol 绑定国服 名字#编号 [大区]"""
        a = parse_args(self._rest(event, {"绑定国服", "bindcn", "国服绑定"}))
        if not a.words:
            names = "、".join(v[0] for v in CN_SERVERS.values())
            yield self._usage(event, "绑定国服", extra=f"可用大区：{names}\n（旧大区名如 祖安、德玛西亚 会自动映射到合并后的大区）")
            return
        if not self.settings.cn_enabled:
            yield self._plain(event, "国服战绩查询未开启，可在插件配置「国服 → 启用国服战绩查询」中开启（开启前请阅读风险提示）")
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
            yield self._usage(event, "切换")
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

    @filter.permission_type(filter.PermissionType.ADMIN)
    @lol.command("WeGame登录", alias={"wegame登录", "扫码登录", "wglogin"})
    async def lol_wegame_login(self, event: AstrMessageEvent):
        """扫码登录 WeGame 并自动保存国服凭证（管理员）：/lol WeGame登录 [QQ|微信]"""
        rest = self._rest(event, {"WeGame登录", "wegame登录", "扫码登录", "wglogin"}).strip().lower()
        wechat = rest in ("微信", "wx", "wechat", "weixin", "vx")
        if not self.settings.cn_enabled:
            yield self._plain(event, "国服战绩查询未开启，可在插件配置「国服 → 启用国服战绩查询」中开启（开启前请阅读风险提示）")
            return
        if self._wg_login_busy:
            yield self._plain(event, "已有一个 WeGame 扫码登录正在进行，请先完成或等它过期")
            return
        flow = self._wegame_flow(wechat, event.unified_msg_origin, event.is_private_chat())
        try:
            async for text, image in flow:
                chain = [Comp.Plain(text)] + ([Comp.Image.fromFileSystem(image)] if image else [])
                yield event.chain_result(chain).use_t2i(False)
        finally:
            await flow.aclose()  # release the login lock even if the handler is cancelled

    async def _wegame_flow(self, wechat: bool, umo: str, private: bool, reason: str = ""):
        """QR login steps as (text, image_path|None) messages; shared by the command and auto renewal."""
        app, cls = ("微信", WeChatQrLogin) if wechat else ("QQ", WeGameQrLogin)
        again = f"/lol WeGame登录{' 微信' if wechat else ''}"
        self._wg_login_busy = True
        path = ""
        try:
            async with cls(timeout=self.settings.cn_timeout, logger=logger) as qr:
                try:
                    png = await qr.start()
                except LolHelperError as exc:
                    yield f"⚠️ {exc}", None
                    return
                path = self._qr_file(png)
                tip = (reason + "\n" if reason else "") + f"📱 请用手机{app}扫码登录 WeGame，2 分钟内有效。登录后凭证会自动保存到插件配置。"
                if not wechat:
                    tip += "\n想用微信扫码请发送 /lol WeGame登录 微信"
                if not private:
                    tip += "\n⚠️ 谁扫码就保存谁的账号凭证，建议私聊机器人使用，请勿让他人扫码。"
                yield tip, path
                deadline, told = time.time() + 120, False
                while time.time() < deadline:
                    await asyncio.sleep(2)
                    state, msg = await qr.poll()
                    if state == SCANNED and not told:
                        told = True
                        yield f"✅ 已扫码，请在手机{app}上确认登录", None
                    elif state in (EXPIRED, FAILED):
                        yield f"⚠️ 登录未完成：{msg}。可重新发送 {again}", None
                        return
                    elif state == DONE:
                        break
                else:
                    yield f"⌛ 二维码已过期，可重新发送 {again}", None
                    return
                try:
                    cookie = await qr.finish()
                except LolHelperError as exc:
                    yield f"⚠️ {exc}", None
                    return
                self._save_wegame_cookie(cookie)
                expires_at = qr.info.get("expires_at")
                await self._wegame_record(umo, app, qr.nick, expires_at)
                who = f"（{qr.nick}）" if qr.nick else ""
                until = f"\n预计有效至 {local_dt(expires_at)}，过期前后会自动提醒续期。" if expires_at else \
                    "\n凭证过期后会自动把新的二维码发到这里，扫码即可续期。"
                yield f"🎉 WeGame {app}登录成功{who}，凭证已保存，国服查询现在可以使用 WeGame 数据源。{until}", None
        except Exception:
            logger.exception("[lolhelper] WeGame 扫码登录出错")
            yield "⚠️ WeGame 扫码登录出错，请稍后重试", None
        finally:
            self._wg_login_busy = False
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass

    # ---------------------------------------------------- WeGame keep-alive
    def _wg_key(self) -> str:
        return hashlib.md5(self.f.wegame.cookie.encode()).hexdigest()[:12]

    async def _wegame_record(self, umo: str, app: str, nick: str, expires_at: float | None) -> None:
        old = self.f.store.state("wegame") or {}
        meta = {"key": self._wg_key(), "umo": umo, "app": app, "nick": nick, "logged_at": time.time(),
                "expires_at": expires_at, "last_ok": time.time(), "last_check": time.time(),
                "lifetimes": list(old.get("lifetimes") or [])[-5:]}
        self.f.store.set_state("wegame", meta)
        await self.f.store.save()

    async def _wegame_tick(self) -> None:
        """Every 30 min: check (and so keep using) the WeGame ticket; remind before expiry, re-send a QR after."""
        s = self.settings
        if not s.cn_enabled or not self.f.wegame.configured or self._wg_login_busy:
            return
        now = time.time()
        meta = dict(self.f.store.state("wegame") or {})
        key = self._wg_key()
        if meta.get("key") != key:  # credential replaced by hand in the config page
            meta = {"key": key, "umo": meta.get("umo"), "app": meta.get("app"), "logged_at": now,
                    "lifetimes": meta.get("lifetimes") or []}
        if now - meta.get("last_check", 0) < WG_CHECK_EVERY:
            if meta != (self.f.store.state("wegame") or {}):
                self.f.store.set_state("wegame", meta)
            return
        meta["last_check"] = now
        try:
            ok = await self.f.wegame.check()
        except LolHelperError as exc:
            logger.info(f"[lolhelper] WeGame 凭证检查失败（网络）：{exc}")
            self.f.store.set_state("wegame", meta)
            return
        umo, auto = meta.get("umo"), s.cn_wegame_auto_relogin
        if ok:
            meta["last_ok"] = now
            exp = meta.get("expires_at")
            if auto and umo and exp and 0 < exp - now < 7200 and meta.get("reminded") != key:
                meta["reminded"] = key
                await self._send_text(umo, f"⏰ WeGame 凭证将在 {local_dt(exp)} 过期，可发送 /lol WeGame登录 提前续期")
        else:
            if not meta.get("expired_at"):
                meta["expired_at"] = now
                life = (meta.get("last_ok") or now) - (meta.get("logged_at") or now)
                if life > 0:
                    meta["lifetimes"] = (list(meta.get("lifetimes") or []) + [round(life)])[-6:]
                logger.info(f"[lolhelper] WeGame 凭证已过期，本次有效约 {fmt_span(life)}")
            if auto and umo and meta.get("notified") != key:
                meta["notified"] = key
                life = (meta.get("last_ok") or now) - (meta.get("logged_at") or now)
                reason = "⚠️ WeGame 凭证已过期" + (f"（本次有效约 {fmt_span(life)}）" if life > 0 else "") + "，扫码即可续期。"
                asyncio.get_running_loop().create_task(
                    self._wegame_auto(umo, meta.get("app") == "微信", reason))
        self.f.store.set_state("wegame", meta)
        await self.f.store.save()

    async def _wegame_auto(self, umo: str, wechat: bool, reason: str) -> None:
        private = "Friend" in umo or "Private" in umo or ":C2C" in umo.upper()
        flow = self._wegame_flow(wechat, umo, private, reason)
        try:
            async for text, image in flow:
                chain = [Comp.Plain(text)] + ([Comp.Image.fromFileSystem(image)] if image else [])
                try:
                    await self.context.send_message(umo, MessageChain(chain))
                except Exception as exc:
                    logger.warning(f"[lolhelper] 发送 WeGame 续期二维码到 {umo} 失败：{exc}")
                    return
        finally:
            await flow.aclose()

    async def _send_text(self, umo: str, text: str) -> None:
        try:
            await self.context.send_message(umo, MessageChain([Comp.Plain(text)]))
        except Exception as exc:
            logger.warning(f"[lolhelper] 发送提醒到 {umo} 失败：{exc}")

    def _qr_file(self, png: bytes) -> str:
        """Save the QR code enlarged with a white margin so phones scan it easily."""
        out = self.f.data_dir / "render" / f"wegame_qr_{int(time.time())}.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            import io

            from PIL import Image

            img = Image.open(io.BytesIO(png)).convert("RGB")
            if img.width < 300:  # QQ codes are tiny; WeChat ones are already large
                img = img.resize((img.width * 3, img.height * 3), Image.NEAREST)
            canvas = Image.new("RGB", (img.width + 60, img.height + 60), "white")
            canvas.paste(img, (30, 30))
            canvas.save(out)
        except Exception:
            out.write_bytes(png)
        return str(out)

    def _save_wegame_cookie(self, cookie: str) -> None:
        cn = self.config.get("cn")
        if not isinstance(cn, dict):
            cn = {}
            self.config["cn"] = cn
        cn["wegame_cookie"] = cookie
        save = getattr(self.config, "save_config", None)
        if callable(save):
            save()
        self.settings.cn_wegame_cookie = cookie
        self.f.wegame.cookie = parse_cookie(cookie)
        self.f.wegame.expired = False

    def _can_subscribe(self, event: AstrMessageEvent) -> bool:
        if not self.settings.push_group_admin_only or event.is_private_chat():
            return True
        return event.is_admin()

    @lol.command("订阅", alias={"sub", "subscribe"})
    async def lol_subscribe(self, event: AstrMessageEvent):
        """订阅推送：/lol 订阅 战绩|赛事 [联赛]|梯队|云顶"""
        a = parse_args(self._rest(event, {"订阅", "sub", "subscribe"}))
        if a.words and a.words[0] in ("列表", "list"):
            async for r in self.lol_subs(event):
                yield r
            return
        if a.words and a.words[0] in ("取消", "退订", "删除", "cancel"):
            async for r in self._unsubscribe(event, " ".join(a.words[1:])):
                yield r
            return
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
            yield self._usage(event, "订阅")
            return
        yield self._plain(event, f"{'✅ 已订阅' if new else 'ℹ️ 已经订阅过'}：{desc}（ID {sub['id']}）")
        # show the subscribed content right away instead of waiting for the next trigger
        s = self.settings
        if sub["type"] == "esports":
            yield self._plain(event, f"推送：每天 {s.push_daily_hour}:00 赛程预告 · 开赛前 {int(s.push_esports_remind // 60)} 分钟提醒 · 每局结束推送比分 · 整场结束推送赛果。近期赛程如下：")
            target_leagues = sub_league_list(sub) or None
            yield await self._answer(event, lambda: self.f.esports("schedule", target_leagues), None)
            for view in await self.push.live_views(target_leagues):
                yield await self._reply(event, view)
        elif sub["type"] == "match":
            yield self._plain(event, f"推送：每 {int(s.push_match_interval // 60)} 分钟检查一次，有新对局时发送对局卡片。最近一局：")

            async def latest():
                acc_ = (sub.get("target") or {}).get("account")
                ms = await self.f.latest_matches(acc_, 1)
                if not ms:
                    raise UsageError("暂时没有查到最近的对局")
                return await self.push.match_view(acc_, ms[0])

            yield await self._answer(event, latest, None)
        elif sub["type"] == "lol_daily":
            yield await self._answer(event, lambda: self.f.tier(None), None)
        elif sub["type"] == "tft_daily":
            yield await self._answer(event, lambda: self.f.tft.meta(10), None)

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
        lines.append("取消：/lol 订阅 取消 ID，或 /lol 订阅 取消 all")
        yield self._plain(event, "\n".join(lines))

    @lol.command("退订", alias={"unsub", "unsubscribe"})
    async def lol_unsubscribe(self, event: AstrMessageEvent):
        """取消订阅：/lol 退订 <ID|all>"""
        a = parse_args(self._rest(event, {"退订", "unsub", "unsubscribe"}))
        async for r in self._unsubscribe(event, a.rest):
            yield r

    async def _unsubscribe(self, event: AstrMessageEvent, arg: str):
        if not arg.strip():
            yield self._usage(event, "订阅", extra="取消订阅：/lol 订阅 取消 <ID|all>，ID 见 /lol 订阅 列表")
            return
        if not self._can_subscribe(event):
            yield self._plain(event, "本群仅管理员可以修改订阅")
            return
        n = await self.f.store.remove_sub(event.unified_msg_origin, arg.strip())
        yield self._plain(event, f"✅ 已取消 {n} 个订阅" if n else "没有找到该订阅，ID 见 /lol 订阅 列表")

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
        """兼容入口：唤醒前缀为空（如 QQ 官方机器人）时也能识别 /lol、／lol 以及不 @ 的「lol 子指令」；
        另外处理单发 /lol（帮助菜单）、菜单的序号回复、/lol 亚索 这类直接查询和打错的指令"""
        text = (event.get_message_str() or "").strip()
        if self._menu_wait and len(text) <= 3 and (text.isdigit() or text in guide.BACK_WORDS):
            until = self._menu_wait.get(self._menu_key(event), 0)
            if time.time() < until:
                ctx = self._guide_ctx(event)
                res = guide.resolve(text, ctx)
                if res and res[0] in ("main", "cat"):
                    event.set_extra("lolhelper_handled", True)
                    if res[0] == "main":
                        r = await self._menu_out(event, guide.main_menu(ctx), ctx)
                    else:
                        r = await self._menu_out(event, guide.category_text(res[1], ctx), ctx, res[1].key)
                    if r is not None:
                        yield r
                    event.stop_event()
                    return
        explicit = text[:1] in ("/", "／")
        body = text.lstrip("/／").strip()
        words = body.split()
        if not words or words[0].lower() not in ENTRY_WORDS or event.get_extra("lolhelper_handled"):
            return
        handler = self._match_command(event, body)
        if handler is not None:
            if event.is_at_or_wake_command and not explicit:
                return  # AstrBot's own command matching handles this one
            event.set_extra("lolhelper_handled", True)
            async for result in self._run_handler(event, handler, body):
                yield result
            event.stop_event()
            return
        if not (event.is_at_or_wake_command or explicit):
            return  # "lol 笑死" in a group is chat, not a command
        event.set_extra("lolhelper_handled", True)
        async for result in self._fallback(event, words):
            yield result
        event.stop_event()

    async def _run_handler(self, event: AstrMessageEvent, handler, body: str):
        original = event.message_str
        event.message_str, event.is_at_or_wake_command = body, True
        try:
            async for result in getattr(self, handler.handler_name)(event):
                yield result
        finally:
            event.message_str = original

    async def _fallback(self, event: AstrMessageEvent, words: list[str]):
        """/lol without a known sub-command: the help menu, a direct query (/lol 亚索 · Faker#KR1 · BLG · LPL · 中单)
        or "did you mean"."""
        group = "lol" if words[0].lower() == "lol" else "tft"
        a = parse_args(" ".join(words[1:]))
        flags = [w for w in words[1:] if w not in a.words]
        ctx = self._guide_ctx(event)
        if not a.words:
            if group == "lol" and self._mentioned(event):
                body = " ".join(["lol", "战绩", *flags])
            else:
                cat = next(c for c in guide.CATEGORIES if c.key == "tft") if group == "tft" else None
                r = await (self._menu_out(event, guide.category_text(cat, ctx), ctx, cat.key) if cat
                           else self._menu_out(event, guide.main_menu(ctx, bare=True), ctx))
                if r is not None:
                    yield r
                return
        else:
            known = self._command_words(group)
            if a.words[0] in known:  # a real command that is switched off in the WebUI or needs a permission
                yield self._plain(event, f"/{group} {known[a.words[0]]} 当前不可用（可能被管理员关闭，或需要管理员权限）")
                return
            hits = guide.suggest(a.words[0], known)
            guess, note = None, ""
            try:
                guess, note = await (self._guess_lol(a.words) if group == "lol" else self._guess_tft(a.words, bool(hits)))
            except Exception as exc:
                logger.info(f"[lolhelper] 识别直接查询失败：{exc}")
            if not guess:
                yield self._plain(event, guide.suggestion_text(a.words[0], hits, group))
                return
            body = " ".join([group, guess, *flags])
        handler = self._match_command(event, body)
        if handler is None:
            yield self._plain(event, guide.suggestion_text(a.words[0] if a.words else group, [], group))
            return
        async for result in self._run_handler(event, handler, body):
            yield result
        if note:
            yield self._plain(event, note)

    async def _guess_lol(self, words: list[str]) -> tuple[str | None, str]:
        text = " ".join(words)
        if "#" in text or "＃" in text:
            return f"战绩 {text}", ""
        leagues, rest = parse_leagues(words)
        if leagues and not rest:
            return f"赛程 {text}", ""
        pos, rest = pick_position(words)
        region, tier, rest = pick_scope(rest)
        if (pos or region or tier) and not rest:
            return f"梯队 {text}", ""
        _, rest = pick_game_mode(rest)
        await self.f.gd.ensure_lol()
        name = " ".join(rest)
        if name and self.f.gd.champion_by_name(name):
            note = ""
            if len(rest) == 1 and await self._is_team(rest[0], network=False):
                note = f"↑ 按英雄查询。要看战队 {rest[0].upper()} 的比赛请发 /lol 赛况 {rest[0].upper()}"
            return f"英雄 {text}", note
        if len(rest) == 1 and await self._is_team(rest[0]):
            return f"赛况 {text}", ""
        return None, ""

    async def _guess_tft(self, words: list[str], typo: bool) -> tuple[str | None, str]:
        text = " ".join(words)
        if words[0].isdigit():
            return f"阵容 {text}", ""
        await self.f.tft._ready()
        for resolve, cmd in ((self.f.gd.resolve_tft_champion, "英雄"), (self.f.gd.resolve_tft_item, "装备")):
            try:
                resolve(text)
                return f"{cmd} {text}", ""
            except LolHelperError:
                pass
        return (None, "") if typo else (f"强化 {text}", "")

    async def _is_team(self, word: str, network: bool = True) -> bool:
        """A team code such as BLG or T1: known from the LoL Esports ranking or earlier OP.GG queries, else (network)
        found among recent matches."""
        w = word.strip().lower()
        if not re.fullmatch(r"[a-z0-9.]{2,6}", w):
            return False
        codes = {str(t.get("code") or "").lower() for t in (self.f.les.data.get("teams") or [])}
        if self.f.svc.esp:
            codes |= {str(t.get("acronym") or "").lower() for t in self.f.svc.esp.teams.values()}
        if w in codes:
            return True
        if not network:
            return False
        try:
            await self.f.svc.esports_find(None, None, w, None)
            return True
        except Exception:
            return False

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

    @filter.llm_tool(name="lolhelper_command_help")
    async def llm_command_help(self, event: AstrMessageEvent, topic: str = "") -> str:
        """查询联盟助手（英雄联盟 / 云顶之弈机器人插件）的指令用法。用户问机器人怎么用、有哪些指令、某个功能用什么指令时调用，并把返回的指令原样告诉用户。

        Args:
            topic(string): 想了解的分类或指令，例如 赛事、竞猜、点评、订阅；留空返回全部指令
        """
        ctx = self._guide_ctx(event)
        res = guide.resolve(topic, ctx) if topic else ("all",)
        if res is None or res[0] in ("all", "main"):
            return guide.full_text(ctx)
        if res[0] == "cat":
            return guide.category_text(res[1], ctx)
        return guide.command_text(res[1], res[2], ctx, self._aliases(res[2]))

    @filter.llm_tool(name="lolhelper_standings")
    async def llm_standings(self, event: AstrMessageEvent, league: str) -> str:
        """查询英雄联盟职业联赛的积分榜（大场、小场、积分）。

        Args:
            league(string): 联赛，例如 LPL、LCK、LEC
        """
        try:
            return self.f.text(await self.f.standings(resolve_league(league) or league.lower()))
        except LolHelperError as exc:
            return f"查询失败：{exc}"

    @filter.llm_tool(name="lolhelper_esports_match")
    async def llm_esports_match(self, event: AstrMessageEvent, team: str = "", game: int = 0) -> str:
        """查询一场英雄联盟职业比赛的详细数据：每局阵容、击杀、资源和 MVP；指定局号时返回该局双方 10 名选手的数据。

        Args:
            team(string): 战队简称，例如 BLG、T1；留空表示最近一场比赛
            game(number): 局号，0 表示整场系列赛
        """
        try:
            return self.f.text(await self.f.esports_match(None, None, team or None, None, int(game or 0) or None))
        except LolHelperError as exc:
            return f"查询失败：{exc}"

    @filter.llm_tool(name="lolhelper_recent_form")
    async def llm_recent_form(self, event: AstrMessageEvent, riot_id: str = "", region: str = "", count: int = 10) -> str:
        """查询召唤师近期战绩的统计：胜率、KDA、参团率、伤害 / 经济 / 死亡占比、常用英雄和位置，以及最近 5 局与整体的对比。适合回答“某人最近打得怎么样”。

        Args:
            riot_id(string): Riot ID，格式 名字#标签；留空表示提问者自己绑定的账号
            region(string): 大区，例如 kr、na、艾欧尼亚；不确定时留空
            count(number): 统计的局数，5~20，默认 10
        """
        try:
            if riot_id:
                words = [riot_id] + ([region] if region else [])
                acc = await self.f.account_from_target(parse_target(words, self.settings.cn_servers_extra))
            else:
                acc = self.f.store.default_account(self._user_key(event))
                if not acc:
                    return "提问者还没有绑定账号，请提供 Riot ID（名字#标签）"
            _, brief, _, _ = await self.f.ai_recent_brief(acc, max(5, min(int(count or 10), 20)))
            return brief
        except LolHelperError as exc:
            return f"查询失败：{exc}"

    @filter.llm_tool(name="lolhelper_tft_lookup")
    async def llm_tft_lookup(self, event: AstrMessageEvent, kind: str, name: str) -> str:
        """查询云顶之弈的英雄出装、某件装备适合哪些英雄，或搜索强化符文。

        Args:
            kind(string): champion 表示英雄出装，item 表示装备，augment 表示强化符文
            name(string): 英雄名、装备名或强化符文关键词
        """
        fn = {"champion": self.f.tft.champion, "item": self.f.tft.item, "augment": self.f.tft.augment}.get(str(kind).lower())
        if fn is None:
            return "kind 只能是 champion、item 或 augment"
        try:
            return self.f.text(await fn(name))
        except LolHelperError as exc:
            return f"查询失败：{exc}"

    @filter.llm_tool(name="lolhelper_group_ranking")
    async def llm_group_ranking(self, event: AstrMessageEvent, period: str = "day") -> str:
        """查询当前群的群友英雄联盟排行榜：胜场、胜率、KDA、评分和称号。只能在群聊中使用。

        Args:
            period(string): day 表示今天的日榜，week 表示本周的周榜
        """
        if not self.settings.group_enabled or event.is_private_chat():
            return "群榜单只能在开启了群玩法的群聊中查询"
        kind = "week" if str(period).lower().startswith("w") or "周" in str(period) else "day"
        try:
            view = await self.groups.board_view(event.unified_msg_origin, kind)
        except LolHelperError as exc:
            return f"查询失败：{exc}"
        return self.f.text(view) if view else "这段时间还没有群友打过排位或匹配"

