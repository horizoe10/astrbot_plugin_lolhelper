"""Argument parsing for chat commands.

Riot IDs may contain spaces ("Hide on bush#KR1"), so commands receive the raw
remainder of the message and parse it here instead of using AstrBot's
space-separated parameters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .constants import (
    CN_GENERIC_WORDS,
    GAME_MODE_ALIASES,
    POSITION_ALIASES,
    resolve_cn_server,
    resolve_opgg_region,
)
from .errors import UsageError

MODE_FLAGS = {
    "-t": "text", "--text": "text", "-文本": "text", "-文字": "text",
    "-m": "markdown", "-md": "markdown", "--md": "markdown", "--markdown": "markdown",
    "-i": "image", "-img": "image", "--image": "image", "-图": "image", "-图片": "image",
}

MODE_WORDS = {
    "text": "text", "txt": "text", "文本": "text", "文字": "text", "纯文本": "text",
    "md": "markdown", "markdown": "markdown",
    "image": "image", "img": "image", "图片": "image", "图": "image",
}

_COUNT_RE = re.compile(r"^(?:-n|-c|n=|count=)(\d+)$")


@dataclass
class Args:
    words: list[str] = field(default_factory=list)
    mode: str | None = None
    count: int | None = None

    @property
    def rest(self) -> str:
        return " ".join(self.words).strip()


def strip_command(message: str, paths: list[list[str]]) -> str:
    """Remove the command words (e.g. ["lol", "战绩"]) from the start of a message."""
    msg = re.sub(r"\s+", " ", (message or "").strip())
    if msg.startswith("/"):
        msg = msg[1:]
    for path in sorted(paths, key=len, reverse=True):
        prefix = " ".join(path)
        if msg == prefix:
            return ""
        if msg.startswith(prefix + " "):
            return msg[len(prefix) + 1:].strip()
    return msg


def parse_args(text: str) -> Args:
    tokens = (text or "").replace("\u3000", " ").split()
    out = Args()
    i = 0
    while i < len(tokens):
        t = tokens[i]
        low = t.lower()
        if low in MODE_FLAGS:
            out.mode = MODE_FLAGS[low]
        elif low in ("-n", "-c") and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            out.count = int(tokens[i + 1])
            i += 1
        elif _COUNT_RE.match(low):
            out.count = int(_COUNT_RE.match(low).group(1))
        else:
            out.words.append(t)
        i += 1
    return out


@dataclass
class Target:
    game_name: str = ""
    tag_line: str = ""
    region: str | None = None  # OP.GG region
    cn_server: str | None = None  # TENCENT_*
    is_cn: bool = False

    @property
    def riot_id(self) -> str:
        return f"{self.game_name}#{self.tag_line}" if self.tag_line else self.game_name


def split_riot_id(text: str) -> tuple[str, str]:
    s = (text or "").strip().replace("＃", "#")
    if "#" not in s:
        raise UsageError("请使用「名字#标签」格式，例如 Hide on bush#KR1")
    name, _, tag = s.rpartition("#")
    name, tag = name.strip(), tag.strip()
    if not name or not tag:
        raise UsageError("请使用「名字#标签」格式，例如 Hide on bush#KR1")
    return name, tag


def parse_target(words: list[str], cn_extra=None, require_id: bool = True) -> Target:
    """Parse "<名字#标签> [大区]" where the region may also come first."""
    w = list(words)
    tgt = Target()

    def take_region(tok: str) -> bool:
        low = tok.lower()
        if low in CN_GENERIC_WORDS:
            tgt.is_cn = True
            return True
        sid = resolve_cn_server(tok, cn_extra)
        if sid:
            tgt.is_cn, tgt.cn_server = True, sid
            return True
        reg = resolve_opgg_region(tok)
        if reg:
            tgt.region = reg
            return True
        return False

    # region tokens may appear at either end
    for _ in range(2):
        if w and "#" not in w[-1] and take_region(w[-1]):
            w.pop()
        if w and "#" not in w[0] and take_region(w[0]):
            w.pop(0)
    text = " ".join(w).strip()
    if text:
        tgt.game_name, tgt.tag_line = split_riot_id(text)
    elif require_id:
        raise UsageError("请提供「名字#标签」")
    return tgt


def pick_position(words: list[str]) -> tuple[str | None, list[str]]:
    rest, pos = [], None
    for t in words:
        p = POSITION_ALIASES.get(t.lower())
        if p and pos is None:
            pos = p
        else:
            rest.append(t)
    return pos, rest


def pick_game_mode(words: list[str]) -> tuple[str | None, list[str]]:
    rest, mode = [], None
    for t in words:
        m = GAME_MODE_ALIASES.get(t.lower())
        if m and mode is None:
            mode = m
        else:
            rest.append(t)
    return mode, rest

