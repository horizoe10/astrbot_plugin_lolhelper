"""Image preparation before rendering: logo cropping, team logo swaps and their fallbacks."""
import asyncio
import base64
import io

from PIL import Image

from lolhelper.render.art import ArtProcessor, trim_logo

OPGG = "https://s-qwer.op.gg/images/lol/teams/blg.png"
LES = "https://static.lolesports.com/teams/blg.png"


def png(w=200, h=100, box=(80, 40, 120, 60), color=(0, 200, 255, 255)) -> bytes:
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if box:
        im.paste(Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), color), box[:2])
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def decode(uri: str) -> Image.Image:
    assert uri.startswith("data:image/png;base64,")
    return Image.open(io.BytesIO(base64.b64decode(uri.split(",", 1)[1]))).convert("RGBA")


def test_trim_logo_crops_to_the_visible_area_and_keeps_colours():
    out = Image.open(io.BytesIO(trim_logo(png(), 128))).convert("RGBA")
    assert out.size == (40, 20)
    assert out.getpixel((20, 10)) == (0, 200, 255, 255)
    big = Image.open(io.BytesIO(trim_logo(png(1000, 600, (0, 100, 1000, 500)), 320)))
    assert big.size == (320, 128)
    assert trim_logo(png(box=None)) is None  # nothing visible


def make(tmp_path, files, logos=None, inline=True):
    calls = []

    async def fetch(url):
        calls.append(url)
        return (files[url], "image/png") if url in files else None

    async def team_logos():
        return logos or {}

    return ArtProcessor(tmp_path, fetch, team_logos, inline=inline), calls


def test_prepare_swaps_team_logos_and_sizes_them_by_place(tmp_path):
    proc, calls = make(tmp_path, {LES: png(1000, 600, (0, 100, 1000, 500))}, {OPGG: LES})
    doc = {"hero": {"l": {"name": "BLG", "logo": OPGG}},
           "sections": [{"block": {"rows": [{"home": {"name": "BLG", "logo": OPGG}}]}}], "link": OPGG}
    out = asyncio.run(proc.prepare(doc))
    assert calls and set(calls) == {LES}  # the LoL Esports logo replaces OP.GG's
    assert decode(out["hero"]["l"]["logo"]).size == (320, 128)
    assert decode(out["sections"][0]["block"]["rows"][0]["home"]["logo"]).size == (128, 51)
    assert out["link"] == OPGG  # links are left alone


def test_prepare_falls_back_to_the_original_logo(tmp_path):
    proc, calls = make(tmp_path, {OPGG: png()}, {OPGG: LES})  # LoL Esports logo unavailable
    out = asyncio.run(proc.prepare({"rows": [{"logo": OPGG}]}))
    assert calls == [LES, OPGG]
    assert decode(out["rows"][0]["logo"]).size == (40, 20)


def test_prepare_without_inlining_only_swaps_urls(tmp_path):
    proc, calls = make(tmp_path, {}, {OPGG: LES}, inline=False)
    out = asyncio.run(proc.prepare({"rows": [{"logo": OPGG}], "hero": {"art": "https://x/a.png"}}))
    assert out == {"rows": [{"logo": LES}], "hero": {"art": "https://x/a.png"}} and calls == []

