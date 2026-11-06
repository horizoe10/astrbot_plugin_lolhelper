"""Live smoke test against OP.GG MCP (needs network). Not run by pytest.

    python tests/smoke_live.py [out_dir]

Writes text / markdown / html renderings of every view into out_dir.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lolhelper.gamedata import GameData  # noqa: E402
from lolhelper.mcp_client import OpggMcpClient  # noqa: E402
from lolhelper.net import HttpClient  # noqa: E402
from lolhelper.render import render_markdown, render_text  # noqa: E402
from lolhelper.render.html import render_html  # noqa: E402
from lolhelper.service import OpggService  # noqa: E402
from lolhelper.tft import TftService  # noqa: E402


async def main(out: Path) -> int:
    logging.basicConfig(level=logging.INFO)
    out.mkdir(parents=True, exist_ok=True)
    http = HttpClient(timeout=60)
    mcp = OpggMcpClient(http)
    gd = GameData(http, out / "data")
    svc = OpggService(mcp, gd)
    tft = TftService(svc)
    await mcp.list_tools()
    print("tools:", len(mcp.tools))
    cases = {
        "champion_yasuo": lambda: svc.champion("快乐风男"),
        "champion_wukong_jg": lambda: svc.champion("猴子", "jungle"),
        "champion_aram": lambda: svc.champion("瞎子", None, "aram"),
        "champion_urf": lambda: svc.champion("ez", None, "urf"),
        "tier_all": lambda: svc.tier(None),
        "tier_mid": lambda: svc.tier("mid"),
        "summoner": lambda: svc.summoner("Hide on bush", "KR1", "kr"),
        "summoner_detail": lambda: svc.summoner("Hide on bush", "KR1", "kr", detail=True),
        "esports_schedule": lambda: svc.esports("schedule"),
        "esports_result": lambda: svc.esports("result", "worlds"),
        "standings_lck": lambda: svc.standings("lck"),
        "tft_meta": lambda: tft.meta(),
        "tft_deck": lambda: tft.deck(1),
        "tft_champion": lambda: tft.champion("凯特琳"),
        "tft_item": lambda: tft.item("大天使"),
        "tft_augment": lambda: tft.augment("贷款"),
    }
    only = set(sys.argv[2:])
    failed = 0
    for name, fn in cases.items():
        if only and name not in only:
            continue
        t0 = time.time()
        try:
            view = await fn()
        except Exception as exc:
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
            continue
        (out / f"{name}.txt").write_text(render_text(view.doc), encoding="utf-8")
        (out / f"{name}.md").write_text(render_markdown(view.doc), encoding="utf-8")
        (out / f"{name}.html").write_text(render_html(view.doc), encoding="utf-8")
        print(f"ok   {name} ({time.time() - t0:.1f}s) sections={len(view.doc.sections)}")
    await http.close()
    return failed


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / ".smoke"
    sys.exit(asyncio.run(main(target)))

