"""Parser for the compact text format returned by OP.GG MCP LoL tools.

Most lol_* tools answer with a schema header followed by positional
constructor calls:

    class LolGetSummonerProfile: data
    class Data: summoner
    class Summoner: game_name,tagline,level

    LolGetSummonerProfile(Data(Summoner("Hide on bush","KR1",946)))

parse_opgg_text() turns this into plain dicts ({"data": {"summoner": {...}}})
so the rest of the code can use the same paths as the JSON tools. JSON payloads
(TFT and esports tools) are decoded directly.
"""

from __future__ import annotations

import json
import re
from json.decoder import scanstring
from typing import Any

_CLASS_RE = re.compile(r"^class\s+([A-Za-z_]\w*)\s*:\s*(.*)$")
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_NUMBER_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")


class DSLParseError(ValueError):
    pass


def parse_opgg_text(text: str) -> Any:
    """Decode one OP.GG MCP text block. Unknown formats are returned as-is."""
    s = (text or "").strip()
    if not s:
        return None
    if s[0] in "[{":
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            pass

    classes: dict[str, list[str]] = {}
    lines = s.splitlines()
    idx = 0
    while idx < len(lines):
        line = lines[idx].strip()
        if not line:
            idx += 1
            continue
        m = _CLASS_RE.match(line)
        if not m:
            break
        classes[m.group(1)] = [f.strip() for f in m.group(2).split(",") if f.strip()]
        idx += 1

    body = "\n".join(lines[idx:]).strip()
    if not classes:
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return body
    return _Parser(body, classes).parse()


class _Parser:
    def __init__(self, src: str, classes: dict[str, list[str]]):
        self.s = src
        self.i = 0
        self.n = len(src)
        self.classes = classes

    def parse(self) -> Any:
        value = self._value()
        self._ws()
        if self.i < self.n:
            raise DSLParseError(
                f"unexpected trailing data at {self.i}: {self.s[self.i:self.i + 30]!r}"
            )
        return value

    def _ws(self) -> None:
        while self.i < self.n and self.s[self.i] in " \t\r\n":
            self.i += 1

    def _peek(self) -> str:
        self._ws()
        if self.i >= self.n:
            raise DSLParseError("unexpected end of input")
        return self.s[self.i]

    def _expect(self, ch: str) -> None:
        if self._peek() != ch:
            raise DSLParseError(f"expected {ch!r} at {self.i}")
        self.i += 1

    def _value(self) -> Any:
        ch = self._peek()
        if ch == '"':
            value, end = scanstring(self.s, self.i + 1)
            self.i = end
            return value
        if ch == "[":
            return self._seq("[", "]")
        if ch == "{":
            value, end = json.JSONDecoder().raw_decode(self.s, self.i)
            self.i = end
            return value
        if ch == "-" or ch.isdigit():
            m = _NUMBER_RE.match(self.s, self.i)
            if not m:
                raise DSLParseError(f"bad number at {self.i}")
            self.i = m.end()
            raw = m.group(0)
            if any(c in raw for c in ".eE"):
                return float(raw)
            return int(raw)
        m = _IDENT_RE.match(self.s, self.i)
        if not m:
            raise DSLParseError(f"unexpected character {ch!r} at {self.i}")
        ident = m.group(0)
        self.i = m.end()
        if ident in ("null", "None", "undefined"):
            return None
        if ident in ("true", "True"):
            return True
        if ident in ("false", "False"):
            return False
        self._ws()
        if self.i < self.n and self.s[self.i] == "(":
            return self._build(ident, self._seq("(", ")"))
        return ident

    def _seq(self, open_ch: str, close_ch: str) -> list[Any]:
        self._expect(open_ch)
        items: list[Any] = []
        if self._peek() == close_ch:
            self.i += 1
            return items
        while True:
            items.append(self._value())
            ch = self._peek()
            if ch == ",":
                self.i += 1
                continue
            if ch == close_ch:
                self.i += 1
                return items
            raise DSLParseError(f"expected ',' or {close_ch!r} at {self.i}")

    def _build(self, name: str, args: list[Any]) -> dict[str, Any]:
        fields = self.classes.get(name)
        if fields is None:
            return {"_type": name, "_args": args}
        obj: dict[str, Any] = {}
        for pos, field in enumerate(fields):
            obj[field] = args[pos] if pos < len(args) else None
        if len(args) > len(fields):
            obj["_extra"] = args[len(fields):]
        return obj


def dig(obj: Any, path: str, default: Any = None) -> Any:
    """Safe nested lookup: dig(d, "data.summoner.level")."""
    cur = obj
    for part in path.split("."):
        if cur is None:
            return default
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if idx < len(cur) else None
        else:
            return default
    return default if cur is None else cur

