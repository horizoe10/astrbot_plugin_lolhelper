from lolhelper.doc import Cell, Doc
from lolhelper.fmt import kda_cls, rate_cls, relative_cls, tier_pill
from lolhelper.render import render_markdown, render_text
from lolhelper.render.html import render_html


def make_doc():
    doc = Doc(title="疾风剑豪 亚索", subtitle="中单 · 单双排", icon="https://x/y.png")
    doc.badge("T2", "t2").badge("胜率 49%")
    doc.add_kv("符文", [("主系", "致命节奏"), ("空值", None)])
    doc.add_table("梯队", ["#", "英雄", "胜率"], [[str(i), Cell(f"英雄{i}", img="https://x/i.png", sub="↑1"), "50%"] for i in range(20)])
    doc.add_tiles("出装", [Cell("无尽之刃", img="https://x/3031.png")])
    doc.add_list("羁绊", ["日蚀骑士 3"])
    doc.footer = "数据刚刚更新"
    return doc


def test_text_truncates_and_shows_headers():
    text = render_text(make_doc(), max_rows=5)
    assert text.startswith("【疾风剑豪 亚索】 中单 · 单双排")
    assert "# / 英雄 / 胜率" in text
    assert "英雄4(↑1)" in text and "英雄5" not in text
    assert "共 20 条" in text
    assert "空值" not in text


def test_markdown_table():
    md = render_markdown(make_doc(), max_rows=3)
    assert "| # | 英雄 | 胜率 |" in md
    assert md.count("\n| ") == 4  # header + 3 rows
    assert "_数据刚刚更新_" in md


def test_html_escapes_user_text():
    doc = Doc(title="<script>alert(1)</script>#KR1")
    doc.add_list("", ["<b>x</b>"])
    html = render_html(doc)
    assert "<script>alert" not in html
    assert "&lt;b&gt;x&lt;/b&gt;" in html


def test_colour_helpers():
    assert relative_cls([4.0, 4.4, 4.8, 4.5]) == ["hi", "", "lo", ""]
    assert relative_cls([1, 1, 1]) == ["", "", ""]
    assert rate_cls(0.53) == "hi" and rate_cls(0.46) == "lo" and rate_cls(0.5) == ""
    assert kda_cls(10, 1, 5) == "kda5" and kda_cls(1, 5, 1) == ""
    assert tier_pill(0) == "op" and tier_pill(2) == "tp t2"


def test_html_pills_trends_and_icon_strip():
    doc = Doc(title="t")
    doc.add_table("", ["#", "评级", "英雄"], [[
        Cell("1", sub="↑2", sub_cls="up inl"),
        Cell("OP", cls="op"),
        Cell("亚索", imgs=["data:image/png;base64,AA"]),
    ]])
    html = render_html(doc)
    assert '<span class="pill op">OP</span>' in html
    assert '<small class="up inl">↑2</small>' in html
    assert 'class="strip"' in html

