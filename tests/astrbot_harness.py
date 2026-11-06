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
    check(set(tools) == {"lolhelper_champion_build", "lolhelper_summoner", "lolhelper_tier_list", "lolhelper_tft_meta", "lolhelper_esports"}, "llm tools registered")
    check("champion" in (tools["lolhelper_champion_build"].parameters or {}).get("properties", {}), "llm tool params parsed from docstring")

    runner, bridge_url = await start_fake_bridge()
    ctx = FakeContext()
    await ctx.html_renderer.initialize()
    conf = {"cn": {"bridge_url": f"{bridge_url}?token=tk"}}
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
        ("lol 英雄 亚索 无限火力 -t", "lol_champion", "暂不提供"),
        ("lol 海克斯 亚索 -t", "lol_mayhem", "海克斯强化 · 棱彩"),
        ("lol 英雄 盲僧 海克斯大乱斗 -t", "lol_champion", "海克斯大乱斗"),
        ("lol 梯队 中单 -i", "lol_tier", "<image"),
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
        ("lol 订阅 战绩", "lol_subscribe", "已订阅"),
        ("lol 订阅 赛事 LPL", "lol_subscribe", "LPL"),
        ("lol 订阅列表", "lol_subs", "新对局"),
        ("lol 退订 all", "lol_unsubscribe", "已取消 2"),
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
        ("lol 状态", "lol_status", "联盟助手 v1.1"),
        ("lol 帮助", "lol_help", "/lol 英雄"),
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

    await plugin.terminate()
    await runner.cleanup()
    print(f"\n{len(FAILS)} failure(s)")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

