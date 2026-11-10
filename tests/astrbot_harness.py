"""Load the plugin inside a real AstrBot install and drive its commands.

    python tests/astrbot_harness.py <work_dir>

Requires the AstrBot package (pip install AstrBot) and network access. It
copies the plugin into <work_dir>/data/plugins, imports it the way AstrBot
does, checks command / LLM tool registration, runs real AstrMessageEvent
objects through the command filters and handlers, and exercises the China
flow against a fake bridge that serves SGP-shaped data.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import shutil
import sys
import time
from pathlib import Path

from aiohttp import web

ROOT = Path(__file__).resolve().parent.parent
WORK = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / ".astrbot_work").resolve()
os.environ["ASTRBOT_ROOT"] = str(WORK)
PLUGIN_DIR = WORK / "data" / "plugins" / "astrbot_plugin_lolhelper"
if PLUGIN_DIR.exists():
    shutil.rmtree(PLUGIN_DIR)
shutil.rmtree(WORK / "data" / "plugin_data" / "astrbot_plugin_lolhelper", ignore_errors=True)
shutil.copytree(ROOT, PLUGIN_DIR, ignore=shutil.ignore_patterns(".git", "__pycache__", ".smoke", ".astrbot_work", "tests"))
os.chdir(WORK)
sys.path.insert(0, str(WORK))

import astrbot.api.message_components as Comp  # noqa: E402
from astrbot.core.platform.astr_message_event import AstrMessageEvent  # noqa: E402
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember  # noqa: E402
from astrbot.core.platform.message_type import MessageType  # noqa: E402
from astrbot.core.platform.platform_metadata import PlatformMetadata  # noqa: E402
from astrbot.core.provider.register import llm_tools  # noqa: E402
from astrbot.core.star.filter.command import CommandFilter  # noqa: E402
from astrbot.core.star.star_handler import star_handlers_registry  # noqa: E402
from astrbot.core.utils.t2i.renderer import HtmlRenderer  # noqa: E402

MODULE = "data.plugins.astrbot_plugin_lolhelper.main"
FAILS: list[str] = []


def check(cond, label):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


class FakeContext:
    def __init__(self):
        self.html_renderer = HtmlRenderer()
        self.sent = []

    def get_config(self, umo=None):
        return {}

    def get_platform_inst(self, pid):
        return None

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))
        return True

    # AI review: a fake chat model that answers with a fixed JSON review
    llm_calls = []

    async def get_current_chat_provider_id(self, umo):
        return "fake-llm"

    def get_using_provider(self, umo=None):
        return None

    def get_provider_by_id(self, pid):
        return None

    async def llm_generate(self, *, chat_provider_id, prompt=None, system_prompt=None, **kw):
        from types import SimpleNamespace
        FakeContext.llm_calls.append((chat_provider_id, system_prompt, prompt))
        return SimpleNamespace(completion_text='{"summary": "测试总评", "highlights": ["亮点一 KDA 3.25"], "issues": ["问题一"], "advice": "建议一"}')


def make_event(text: str, platform: str = "aiocqhttp", sender: str = "10001", group: bool = True, at: str | None = None):
    msg = AstrBotMessage()
    msg.type = MessageType.GROUP_MESSAGE if group else MessageType.FRIEND_MESSAGE
    msg.self_id = "999"
    msg.session_id = "g1" if group else sender
    msg.message_id = str(time.time())
    msg.sender = MessageMember(user_id=sender, nickname="tester")
    msg.message = [Comp.Plain(text)] + ([Comp.At(qq=at)] if at else [])
    msg.message_str = text
    msg.raw_message = None
    ev = AstrMessageEvent(text, msg, PlatformMetadata(name=platform, description="", id="testbot"), msg.session_id)
    ev.is_at_or_wake_command = True
    return ev


def describe(result) -> str:
    out = []
    for c in result.chain:
        if isinstance(c, Comp.Plain):
            out.append(c.text)
        elif isinstance(c, Comp.Image):
            out.append(f"<image {str(c.file)[:90]}>")
        else:
            out.append(f"<{type(c).__name__}>")
    return "".join(out)


def find_handler(handlers, text):
    ev = make_event(text)
    for md in handlers:
        for flt in md.event_filters:
            if isinstance(flt, CommandFilter):
                try:
                    if flt.filter(ev, {}):
                        return md
                except Exception as exc:
                    print("  filter raised", md.handler_name, exc)
    return None


# ------------------------------------------------------------------ fake bridge
ME = "cn-puuid-1"


async def start_fake_bridge():
    async def status(_):
        return web.json_response({"ok": True, "data": {"version": "1.0", "connected": True, "server": "TENCENT_HN1", "server_name": "艾欧尼亚",
                                                       "current": {"gameName": "测试玩家", "tagLine": "12345"}, "token_entitlements": True,
                                                       "token_league-session": True, "source": "fake"}})

    async def summoner(req):
        if req.headers.get("Authorization") != "Bearer tk":
            return web.json_response({"ok": False, "error": "bad token"}, status=401)
        return web.json_response({"ok": True, "data": {"puuid": ME, "gameName": req.query.get("name"), "tagLine": req.query.get("tag"),
                                                       "summonerLevel": 321, "profileIconId": 29}})

    async def ranked(_):
        return web.json_response({"ok": True, "data": {"source": "lcu", "data": {"queueMap": {"RANKED_SOLO_5x5": {
            "queueType": "RANKED_SOLO_5x5", "tier": "DIAMOND", "division": "II", "leaguePoints": 66, "wins": 120, "losses": 101}}}}})

    async def matches(_):
        def p(puuid, team, champ, k, win):
            return {"puuid": puuid, "teamId": team, "championId": champ, "kills": k, "deaths": 3, "assists": 7, "win": win,
                    "totalMinionsKilled": 180, "neutralMinionsKilled": 12, "teamPosition": "MIDDLE", "item0": 3031,
                    "item1": 6673, "totalDamageDealtToChampions": 25000}
        games = []
        for i in range(3):
            games.append({"json": {"gameId": 8000 + i, "gameCreation": int(time.time() * 1000) - i * 3600_000, "gameDuration": 1700,
                                   "queueId": 420 if i else 450,
                                   "participants": [p(ME, 100, 157, 8 - i, i != 1), p("x", 100, 238, 4, i != 1), p("y", 200, 62, 6, i == 1)]}})
        return web.json_response({"ok": True, "data": {"source": "sgp", "server": "TENCENT_HN1", "data": {"games": games}}})

    app = web.Application()
    app.router.add_get("/v1/status", status)
    app.router.add_get("/v1/summoner", summoner)
    app.router.add_get("/v1/ranked", ranked)
    app.router.add_get("/v1/matches", matches)

    async def game(req):
        gid = int(req.query.get("game_id"))
        parts = []
        for i, champ in enumerate((157, 238, 62, 777, 1, 2, 3, 4, 5, 6)):
            team = 100 if i < 5 else 200
            parts.append({"puuid": ME if i == 0 else f"p{i}", "teamId": team, "championId": champ, "kills": 5, "deaths": 2, "assists": 4,
                          "win": team == 100, "riotIdGameName": "测试玩家" if i == 0 else f"玩家{i}", "riotIdTagline": "1",
                          "champLevel": 14, "goldEarned": 9000, "totalDamageDealtToChampions": 15000 + i, "totalDamageTaken": 12000,
                          "teamPosition": ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"][i % 5], "summoner1Id": 4, "summoner2Id": 14,
                          "item0": 3031, "totalMinionsKilled": 150})
        teams = [{"teamId": 100, "win": True, "objectives": {"tower": {"kills": 8}, "dragon": {"kills": 3}, "baron": {"kills": 1}}, "bans": [{"championId": 238}]},
                 {"teamId": 200, "win": False, "objectives": {"tower": {"kills": 2}}, "bans": []}]
        return web.json_response({"ok": True, "data": {"source": "sgp", "data": {"json": {"gameId": gid, "gameCreation": int(time.time() * 1000),
                                                                                         "gameDuration": 1800, "queueId": 420, "teams": teams, "participants": parts}}}})

    app.router.add_get("/v1/game", game)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


async def main() -> int:
    mod = importlib.import_module(MODULE)
    handlers = [h for h in star_handlers_registry if h.handler_module_path == MODULE]
    names = sorted(h.handler_name for h in handlers)
    print("handlers:", names)
    check(len(handlers) >= 30, f"registered {len(handlers)} handlers")
    tools = {t.name: t for t in llm_tools.func_list if t.name.startswith("lolhelper_")}
    check(set(tools) == {"lolhelper_champion_build", "lolhelper_summoner", "lolhelper_tier_list", "lolhelper_tft_meta", "lolhelper_esports",
                         "lolhelper_command_help", "lolhelper_standings", "lolhelper_esports_match", "lolhelper_recent_form",
                         "lolhelper_tft_lookup", "lolhelper_group_ranking"}, "llm tools registered")
    check("champion" in (tools["lolhelper_champion_build"].parameters or {}).get("properties", {}), "llm tool params parsed from docstring")

    runner, bridge_url = await start_fake_bridge()
    ctx = FakeContext()
    await ctx.html_renderer.initialize()
    conf = {"cn": {"enabled": True, "bridge_url": f"{bridge_url}?token=tk"}, "ai": {"cooldown_sec": 0}}
    plugin = mod.LolHelperPlugin(ctx, conf)
    await plugin.initialize()

    async def run(text, **kw):
        md = find_handler(handlers, text)
        if not md:
            return None, "<no handler>"
        ev = make_event(text, **kw)
        outs = []
        async for res in md.handler(plugin, ev):
            outs.append(describe(res))
        return md.handler_name, "\n".join(outs)

    cases = [
        ("lol 英雄 快乐风男 中单 -t", "lol_champion", "疾风剑豪"),
        ("lol 出装 盲僧 打野 -m", "lol_champion", "盲僧"),
        ("lol 英雄 亚索 无限火力 -t", "lol_champion", "无限火力"),
        ("lol 海克斯 亚索 -t", "lol_mayhem", "海克斯强化 · 棱彩"),
        ("lol 英雄 盲僧 海克斯大乱斗 -t", "lol_champion", "海克斯大乱斗"),
        ("lol 梯队 中单 -i", "lol_tier", "<image"),
        ("lol 梯队 中单 韩服 钻石+ -t", "lol_tier", "韩服 | 钻石+"),
        ("lol 英雄 3 -t", "lol_champion", "钻石+"),
        ("lol 英雄 亚索 中单 -t", "lol_champion", "全球 | 翡翠+"),
        ("lol 战绩 Hide on bush#KR1 kr -t", "lol_matches", "Hide on bush#KR1"),
        ("lol 战绩 -t", "lol_matches", "还没有绑定"),
        ("lol 绑定 hide on bush#kr1 韩服", "lol_bind", "已绑定 Hide on bush#KR1"),
        ("lol 资料 -t", "lol_profile", "本赛季常用英雄"),
        ("lol 绑定国服 测试玩家#12345 艾欧尼亚", "lol_bind_cn", "国服·艾欧尼亚"),
        ("lol 账号", "lol_accounts", "★2."),
        ("lol 战绩 -t", "lol_matches", "国服 · 艾欧尼亚"),
        ("lol 战绩 -i", "lol_matches", "<image"),
        ("lol 对局 -t", "lol_game", "胜利 蓝队"),
        ("lol 对局 2 -i", "lol_game", "<image"),
        ("lol 切换 1", "lol_switch", "Hide on bush"),
        ("lol 对局 -t", "lol_game", "MVP"),
        ("lol 模式 文本", "lol_mode", "纯文本"),
        ("lol 梯队", "lol_tier", "【版本梯队】"),
        ("lol 模式 默认", "lol_mode", "恢复默认"),
        ("lol 订阅 战绩", "lol_subscribe", "最近一局"),
        ("lol 订阅 梯队", "lol_subscribe", "<image"),
        ("lol 订阅 赛事 LPL", "lol_subscribe", "LPL"),
        ("lol 订阅列表", "lol_subs", "新对局"),
        ("lol 退订 all", "lol_unsubscribe", "已取消 3"),
        ("lol 订阅 列表", "lol_subscribe", "没有订阅"),
        ("lol 订阅 赛事 LCK", "lol_subscribe", "已订阅"),
        ("lol 订阅 取消 all", "lol_subscribe", "已取消 1"),
        ("lol 国服状态", "lol_cn_status", "已连接客户端"),
        ("lol 赛程 -t", "lol_schedule", "赛程"),
        ("lol 积分 LCK -t", "lol_standings", "积分榜"),
        ("lol 赛果 S赛 LPL -t", "lol_results", "全球总决赛 / LPL"),
        ("lol 赛况 worlds 1 -t", "lol_match", "每局数据"),
        ("lol 赛况 BLG 2 -t", "lol_match", "第2局"),
        ("lol 赛程 火星联赛", "lol_schedule", "未知联赛"),
        ("tft 阵容 -t", "tft_meta", "版本强势阵容"),
        ("云顶 阵容 2 -t", "tft_meta", "最终阵容"),
        ("tft 英雄 凯特琳 -t", "tft_champion", "登顶率最高的出装"),
        ("tft 装备 大天使 -t", "tft_item", "携带英雄排行"),
        ("tft 强化 贷款 -t", "tft_augment", "贷款"),
        ("tft 阵容 -i", "tft_meta", "<image"),
        ("lol 大区", "lol_regions", "艾欧尼亚"),
        ("lol 状态", "lol_status", "联盟助手 v2.0.0"),
        ("lol 帮助", "lol_help", "/lol 英雄"),
        ("lol 帮助 赛事", "lol_help", "/lol 赛况 [序号|战队] [局号]"),
        ("lol 帮助 竞猜", "lol_help", "别名："),
        ("lol 帮助 全部", "lol_help", "全部指令"),
        ("lol 帮助 -i", "lol_help", "<image"),
        ("lol 菜单", "lol_help", "帮助菜单"),
        ("tft 帮助", "tft_help", "/tft 阵容"),
        ("lol 积分 -t", "lol_standings", "示例：/lol 积分 LPL"),
        ("lol 点评 Hide on bush#KR1 kr -t", "lol_review", "测试总评"),
        ("lol 锐评 Hide on bush#KR1 kr -t", "lol_review", "锐评"),
        ("lol 诊断 Hide on bush#KR1 kr 8 -t", "lol_diagnose", "测试总评"),
        ("lol 赛况点评 worlds 1 1 幽默 -t", "lol_match_review", "测试总评"),
        ("lol 赛况点评 worlds 1 -t", "lol_match_review", "测试总评"),
        ("lol 群成员", "lol_group_members", "上榜群友"),
        ("lol 日榜 -t", "lol_daily_board", "群"),
        ("lol 周榜 上周 -t", "lol_weekly_board", "周"),
        ("lol 榜单 -t", "lol_boards", "群"),
        ("lol 榜单 竞猜 -t", "lol_boards", "竞猜"),
        ("lol 群播报", "lol_group_broadcast", "群友播报"),
        ("lol 群播报 关", "lol_group_broadcast", "已关闭"),
        ("lol 对比 Kingen#KR1 kr -t", "lol_pk", "vs"),
        ("lol 今天玩什么 中单 -t", "lol_random", "今天玩"),
        ("lol 分队 甲 乙 丙 丁 戊 -t", "lol_split", "蓝队"),
        ("lol 竞猜 -t", "lol_bet", "竞猜"),
        ("lol 猜英雄榜", "lol_quiz_board", "猜英雄"),
        ("lol 下榜", "lol_leave_board", "已退出"),
        ("lol 上榜", "lol_join_board", "已加入"),
    ]
    for text, want_handler, want in cases:
        t0 = time.time()
        try:
            name, out = await run(text)
        except Exception as exc:
            name, out = None, f"EXC {type(exc).__name__}: {exc}"
        ok = name == want_handler and want in out
        check(ok, f"{text!r} -> {name} ({time.time() - t0:.1f}s)")
        if not ok or os.environ.get("VERBOSE"):
            print("   ", out[:600].replace("\n", "\n    "))

    # @mention resolves the mentioned user's binding
    md = find_handler(handlers, "lol 战绩")
    ev = make_event("lol 战绩 -t", sender="20002", at="10001")
    out = "".join([describe(r) async for r in md.handler(plugin, ev)])
    check("Hide on bush" in out, "@mention uses the mentioned user's binding")

    # LLM tool returns text
    txt = await plugin.llm_tier(make_event("x"), "中单")
    check("中单梯队" in txt, "llm tool returns text")

    # push of a new CN match through context.send_message
    acc = plugin.f.store.accounts("testbot:10001")[1]
    view = await plugin.push.match_view(acc, (await plugin.f.latest_matches(acc, 1))[0])
    await plugin._push_send("testbot:GroupMessage:g1", view, "text")
    check(ctx.sent and "赢下了一局" in ctx.sent[-1][1].chain[0].text, "push message sent via context")

    # remote CN bridge address with the token in the URL
    check(plugin.f.bridge.mode == "remote" and plugin.f.bridge.token == "tk", "remote cn address carries token")

    # AI review: cached reviews skip the model; the cooldown only applies to real model calls
    n_calls = len(FakeContext.llm_calls)
    _, out = await run("lol 点评 Hide on bush#KR1 kr -t")
    check("测试总评" in out and len(FakeContext.llm_calls) == n_calls, "ai review served from cache")
    plugin.settings.ai_cooldown = 60
    _, out = await run("lol 点评 2 Hide on bush#KR1 kr -t")
    _, out2 = await run("lol 点评 3 Hide on bush#KR1 kr -t")
    check("冷却中" in out2, "ai review cooldown")
    plugin.settings.ai_cooldown = 0
    # guess-the-champion: a plain group message with the answer wins the round
    _, out = await run("lol 猜英雄 技能名")
    umo_q = next(iter(plugin.groups.quizzes), None)
    answer = plugin.groups.quizzes[umo_q]["name"].split(" ")[-1] if umo_q else ""
    wrong = [r async for r in plugin.quiz_answer(make_event("这题好难"))]
    right = [describe(r) async for r in plugin.quiz_answer(make_event(answer))]
    check("猜英雄" in out and not wrong and right and "答对了" in right[0] and not plugin.groups.quizzes, f"quiz answered ({answer})")
    _, out = await run("lol 不猜了")
    check("没有进行中" in out, "quiz end without a quiz")
    sys_prompt = FakeContext.llm_calls[-1][1] if FakeContext.llm_calls else ""
    check("只使用数据里出现的事实" in sys_prompt, "ai system prompt forbids made-up facts")

    # WeGame QR login: routes to the handler and first replies with a QR image (stop before scanning)
    md = find_handler(handlers, "lol WeGame登录")
    check(md is not None and md.handler_name == "lol_wegame_login", "wegame login command routed")
    gen = md.handler(plugin, make_event("lol WeGame登录"))
    first = await gen.__anext__()
    await gen.aclose()
    kinds = [type(c).__name__ for c in (first.chain or [])]
    check("Image" in kinds and "扫码" in describe(first), f"wegame login sends QR image {kinds}")
    check(plugin._wg_login_busy is False, "wegame login releases its lock")
    gen = md.handler(plugin, make_event("lol WeGame登录 微信"))
    first = await gen.__anext__()
    await gen.aclose()
    kinds = [type(c).__name__ for c in (first.chain or [])]
    check("Image" in kinds and "微信扫码" in describe(first), f"wegame wechat login sends QR image {kinds}")
    plugin._save_wegame_cookie("a=1; tgp_id=123; tgp_ticket=ABC")
    check(plugin.config["cn"]["wegame_cookie"].startswith("a=1") and plugin.f.wegame.cookie == "tgp_id=123; tgp_ticket=ABC",
          "wegame cookie saved to config and client")

    # WeGame keep-alive: reminder before expiry, auto QR after expiry, only once per credential
    import time as _t
    dm = "testbot:FriendMessage:10001"
    await plugin._wegame_record(dm, "QQ", "tester", _t.time() + 3600)
    state = {"ok": True}

    async def fake_check():
        return state["ok"]

    plugin.f.wegame.check = fake_check
    autos = []

    async def fake_auto(umo, wechat, reason):
        autos.append((umo, wechat, reason))

    plugin._wegame_auto = fake_auto
    n0 = len(ctx.sent)
    plugin.f.store.data["push_state"]["wegame"]["last_check"] = 0
    await plugin._wegame_tick()
    check(len(ctx.sent) == n0 + 1 and "过期" in ctx.sent[-1][1].chain[0].text, "wegame reminds before expiry")
    plugin.f.store.data["push_state"]["wegame"]["last_check"] = 0
    await plugin._wegame_tick()
    check(len(ctx.sent) == n0 + 1, "wegame reminder sent only once")
    state["ok"] = False
    plugin.f.store.data["push_state"]["wegame"]["last_check"] = 0
    await plugin._wegame_tick()
    await asyncio.sleep(0.05)
    check(len(autos) == 1 and autos[0][0] == dm and "已过期" in autos[0][2], "wegame expiry triggers auto QR")
    plugin.f.store.data["push_state"]["wegame"]["last_check"] = 0
    await plugin._wegame_tick()
    await asyncio.sleep(0.05)
    check(len(autos) == 1, "wegame auto QR only once per credential")
    meta = plugin.f.store.state("wegame")
    check(meta.get("lifetimes") and meta.get("expired_at"), "wegame lifetime recorded")
    view = await plugin.f.cn_status()
    txt = plugin.f.text(view)
    check("以往有效时长" in txt and "上次扫码的会话" in txt, "cn status shows wegame credential details")

    # compat entry: empty wake prefix (QQ official bots) -> slash commands / bare commands without @
    async def compat(text, woken):
        ev = make_event(text)
        ev.is_at_or_wake_command = woken
        outs = [describe(r) async for r in mod.LolHelperPlugin.compat_entry(plugin, ev)]
        return "\n".join(outs), ev.is_stopped()

    out, stopped = await compat("/lol 帮助", False)
    check("联盟助手" in out and stopped, "compat: /lol without @ works")
    out, _ = await compat("lol 帮助", False)
    check("联盟助手" in out, "compat: bare lol sub-command without @ works")
    out, _ = await compat("／lol 大区", True)
    check("外服" in out, "compat: @bot + full-width slash works")
    out, _ = await compat("lol 笑死", False)
    check(out == "", "compat: chat starting with lol is ignored")
    out, _ = await compat("lol 帮助", True)
    check(out == "", "compat: woken plain command left to AstrBot (no double reply)")

    # guide: bare /lol, number replies, direct queries and typo suggestions
    gf = mod.LolHelperPlugin.lol.parent_group
    check(gf.filter(make_event("lol"), {}) is False and gf.filter(make_event("lol 战绩"), {}) is True,
          "guide: bare /lol no longer triggers AstrBot's command tree")
    out, stopped = await compat("lol", True)
    check("主菜单" in out and "1. 📜 战绩与账号" in out and stopped, "guide: bare /lol shows the help menu")
    out, _ = await compat("3", False)
    check("🏆 赛事" in out and "/lol 赛程" in out, "guide: number reply opens a category")
    out, _ = await compat("0", False)
    check("帮助菜单" in out, "guide: 0 goes back to the main menu")
    other = make_event("3", sender="20002")
    other.is_at_or_wake_command = False
    outs = [describe(r) async for r in mod.LolHelperPlugin.compat_entry(plugin, other)]
    check(not outs, "guide: number replies only count for the person who opened the menu")
    out, _ = await compat("lol 战积", True)
    check("你是不是想找" in out and "/lol 战绩" in out, "guide: typo suggests the command")
    out, _ = await compat("lol 哈哈哈", False)
    check(out == "", "guide: unwoken chat is still ignored")
    out, _ = await compat("lol 亚索 -t", True)
    check("疾风剑豪" in out, "guide: /lol <champion> shows the build")
    out, _ = await compat("lol LPL -t", True)
    check("赛程" in out, "guide: /lol <league> shows the schedule")
    out, _ = await compat("lol 中单 -t", True)
    check("中单" in out and "梯队" in out, "guide: /lol <position> shows the tier list")
    out, _ = await compat("lol Hide on bush#KR1 kr -t", True)
    check("Hide on bush#KR1" in out, "guide: /lol <riot id> shows the matches")
    out, _ = await compat("lol BLG -t", True)
    check("每局数据" in out or "BLG" in out, "guide: /lol <team> shows the series")
    out, _ = await compat("tft", True)
    check("云顶之弈" in out and "/tft 阵容" in out, "guide: bare /tft shows the TFT commands")
    out, _ = await compat("tft 大天使 -t", True)
    check("携带英雄排行" in out, "guide: /tft <item> shows the item")

    # menu buttons (AstrBot 4.29+): built from the categories, a click opens the category with its own buttons
    if hasattr(Comp, "ActionRow"):
        check(any(h.handler_name == "menu_button" and any(type(f).__name__ == "ButtonActionFilter" for f in h.event_filters)
                  for h in handlers), "guide: button handler registered")
        rows = plugin._button_rows(plugin._guide_ctx(make_event("x")), None)
        check(len(rows) == 2 and rows[0].buttons[0].id == "lolhelper:menu:account" and rows[0].fallback_text, "guide: menu buttons built")
        ev = make_event("", platform="telegram")
        ev.message_obj.message = [Comp.ButtonInteraction(action_id="lolhelper:menu:esports", data={"k": "esports"}, interaction_id="1")]
        sent = []

        async def fake_send(chain):
            sent.append(chain)

        ev.send = fake_send
        outs = [describe(r) async for r in plugin.menu_button(ev)]
        kinds = [type(c).__name__ for c in sent[0].chain] if sent else []
        check(sent and "🏆 赛事" in sent[0].chain[0].text and "ActionRow" in kinds and not outs, f"guide: button click opens the category {kinds}")
        ev = make_event("lol", platform="telegram")
        ev.send = fake_send
        sent.clear()
        out = [describe(r) async for r in mod.LolHelperPlugin.compat_entry(plugin, ev)]
        check(sent and "主菜单" in sent[0].chain[0].text and not out, "guide: menu goes out with buttons on supported platforms")

        async def broken_send(chain):
            raise RuntimeError("keyboard not allowed")

        ev = make_event("lol", platform="telegram")
        ev.send = broken_send
        out = "".join([describe(r) async for r in mod.LolHelperPlugin.compat_entry(plugin, ev)])
        check("主菜单" in out, "guide: menu falls back to text when buttons fail")
    txt = await plugin.llm_command_help(make_event("x"), "竞猜")
    check("/lol 竞猜" in txt, "guide: llm command help tool")

    await plugin.terminate()
    await runner.cleanup()
    print(f"\n{len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

