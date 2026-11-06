import asyncio
import json
from pathlib import Path

from lolhelper.settings import Settings
from lolhelper.storage import StateStore, account_key, account_label

SCHEMA = Path(__file__).resolve().parent.parent / "_conf_schema.json"


def defaults_from_schema() -> dict:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    return {g: {k: v.get("default") for k, v in spec["items"].items()} for g, spec in schema.items()}


def test_settings_from_schema_defaults():
    s = Settings.from_config(defaults_from_schema())
    assert s.default_mode == "image" and s.default_region == "kr"
    assert s.cn_bridge_url == "http://127.0.0.1:6198"
    assert s.ttl_meta == 180 * 60 and s.push_match_interval == 300
    assert "telegram" in s.markdown_platforms


def test_settings_parsing():
    s = Settings.from_config({
        "data_source": {"tool_overrides": "a=b\nc=d"},
        "aliases": {"champion_aliases": "托儿索=Yasuo，小鱼=Fizz"},
        "cn": {"servers_extra": "TENCENT_NEW1=新大区"},
        "output": {"default_mode": "weird"},
    })
    assert s.tool_overrides == {"a": "b", "c": "d"}
    assert s.champion_aliases == {"托儿索": "Yasuo", "小鱼": "Fizz"}
    assert s.cn_servers_extra["TENCENT_NEW1"][0] == "新大区"
    assert s.default_mode == "image"


def test_bindings_and_subs(tmp_path):
    async def run():
        st = StateStore(tmp_path / "state.json")
        await st.load()
        a = {"kind": "global", "region": "kr", "game_name": "Hide on bush", "tag_line": "KR1"}
        b = {"kind": "cn", "server": "TENCENT_HN1", "puuid": "p1", "game_name": "玩家", "tag_line": "12345"}
        assert await st.bind("qq:1", a) == 0
        assert await st.bind("qq:1", b) == 1
        assert await st.bind("qq:1", dict(a)) == 0  # rebinding updates in place
        assert st.default_account("qq:1")["game_name"] == "Hide on bush"
        assert "国服·艾欧尼亚" in account_label(b)
        sub, new = await st.add_sub("umo", "match", {"account": b})
        _, new2 = await st.add_sub("umo", "match", {"account": b})
        assert new and not new2
        st2 = StateStore(tmp_path / "state.json")
        await st2.load()
        assert len(st2.accounts("qq:1")) == 2 and len(st2.subs()) == 1
        assert await st2.remove_sub("umo", sub["id"]) == 1
        assert account_key(a) == "g:kr:hide on bush#kr1"

    asyncio.run(run())

