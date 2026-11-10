"""AI review helpers: model output parsing, cooldown / cache, China client timeline summary."""
import asyncio

import pytest

from lolhelper.ai import AiError, AiReviewer, parse_review, pick_style, summarize_timeline
from lolhelper.errors import UsageError


def test_parse_review_accepts_fenced_and_plain_output():
    fenced = '好的：\n```json\n{"summary": "总评", "highlights": ["a", "b", "c", "d"], "issues": "x；y", "advice": "建议"}\n```'
    r = parse_review(fenced)
    assert r["summary"] == "总评" and r["highlights"] == ["a", "b", "c"] and r["issues"] == ["x", "y"]
    plain = parse_review("模型没按格式，直接说了一段话")
    assert plain["summary"].startswith("模型没按格式") and plain["highlights"] == []


def test_pick_style_aliases():
    assert pick_style(["2", "毒舌", "Faker#KR1"]) == ("锐评", ["2", "Faker#KR1"])
    assert pick_style(["Faker#KR1"]) == (None, ["Faker#KR1"])


def test_reviewer_caches_and_only_gates_real_calls():
    calls = []

    async def complete(system, prompt, umo):
        calls.append(prompt)
        return '{"summary": "s", "highlights": ["h"], "issues": [], "advice": ""}', "m"

    gates = []

    def gate():
        gates.append(1)
        if len(gates) > 1:
            raise UsageError("冷却中")

    async def run():
        ai = AiReviewer(complete)
        a = await ai.review("game", "k1", "brief", "专业", gate=gate)
        b = await ai.review("game", "k1", "brief", "专业", gate=gate)  # cache hit: no gate, no call
        assert a is b and len(calls) == 1 and len(gates) == 1
        with pytest.raises(UsageError):
            await ai.review("game", "k2", "brief", "专业", gate=gate)
        await ai.review("game", "k1", "brief", "锐评")  # another style is another review
        assert len(calls) == 2

    asyncio.run(run())


def test_reviewer_rejects_empty_output():
    async def complete(system, prompt, umo):
        return "", "m"

    with pytest.raises(AiError):
        asyncio.run(AiReviewer(complete).review("game", "k", "b"))


def test_summarize_timeline_gold_diff_and_events():
    def frame(minute, blue, red, events=()):
        pf = {str(i): {"totalGold": blue if i <= 5 else red, "minionsKilled": minute * 7, "jungleMinionsKilled": 0} for i in range(1, 11)}
        return {"timestamp": minute * 60000, "participantFrames": pf, "events": list(events)}

    frames = [frame(m, 500 + m * 400, 500 + m * 380) for m in range(0, 21)]
    frames[3]["events"].append({"type": "CHAMPION_KILL", "timestamp": 185000, "killerId": 7})
    frames[8]["events"].append({"type": "ELITE_MONSTER_KILL", "timestamp": 490000, "killerTeamId": 100, "monsterType": "DRAGON"})
    raw = {"info": {"frames": frames, "participants": [{"participantId": 3, "puuid": "me"}]}}
    tl = summarize_timeline(raw, "BLUE", "me")
    assert tl["gold_diff"][10] == 5 * (10 * 400 - 10 * 380)
    assert tl["first_blood"] == "一血 第3分钟 由敌方拿下"
    assert tl["objectives"] == ["8分我方小龙"]
    assert tl["me"][15]["cs"] == 105
