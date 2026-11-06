"""Small formatting helpers shared by the views."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone, tzinfo

from .constants import APEX_TIERS, DIVISION_ROMAN, QUEUE_TYPES_ZH, TIERS_ZH

_TZ_CACHE: dict[str, tzinfo] = {}
_DEFAULT_TZ = ["Asia/Shanghai"]


def set_default_tz(name: str) -> None:
    _DEFAULT_TZ[0] = name or "Asia/Shanghai"


def get_tz(name: str = "Asia/Shanghai") -> tzinfo:
    if name not in _TZ_CACHE:
        try:
            from zoneinfo import ZoneInfo

            _TZ_CACHE[name] = ZoneInfo(name)
        except Exception:  # Windows without tzdata
            _TZ_CACHE[name] = timezone(timedelta(hours=8))
    return _TZ_CACHE[name]


def pct(x, digits: int = 1) -> str:
    if x is None or x == "":
        return "-"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    if v <= 1.0:
        v *= 100
    s = f"{v:.{digits}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s + "%"


def num(x, digits: int = 2) -> str:
    if x is None:
        return "-"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    if v.is_integer():
        return str(int(v))
    s = f"{v:.{digits}f}"
    return s.rstrip("0").rstrip(".")


def big(n) -> str:
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "-"
    if v >= 10000:
        return f"{v / 10000:.1f}万"
    return str(int(v))


def kda(k, d, a) -> str:
    try:
        k, d, a = int(k or 0), int(d or 0), int(a or 0)
    except (TypeError, ValueError):
        return "-"
    if d == 0:
        return "Perfect"
    return f"{(k + a) / d:.2f}"


def tier_text(tier: str | None, division=None, lp=None) -> str:
    if not tier:
        return "未定级"
    up = str(tier).upper()
    name = TIERS_ZH.get(up, up)
    parts = [name]
    if up not in APEX_TIERS and division not in (None, "", "NA"):
        parts.append(DIVISION_ROMAN.get(division, str(division)) if isinstance(division, int) else str(division))
    if lp not in (None, ""):
        parts.append(f"{lp}LP")
    return " ".join(parts)


def queue_type(name: str | None) -> str:
    if not name:
        return "-"
    return QUEUE_TYPES_ZH.get(name, QUEUE_TYPES_ZH.get(name.upper(), name))


def duration(seconds) -> str:
    try:
        s = int(seconds or 0)
    except (TypeError, ValueError):
        return "-"
    if s > 100000:  # milliseconds
        s //= 1000
    return f"{s // 60}:{s % 60:02d}"


def parse_time(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v > 1e11:
            v /= 1000
        return datetime.fromtimestamp(v, tz=timezone.utc)
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def local_time(value, tz_name: str = "Asia/Shanghai", fmt: str = "%m-%d %H:%M") -> str:
    dt = parse_time(value)
    if not dt:
        return "-"
    return dt.astimezone(get_tz(tz_name)).strftime(fmt)


def ago(value) -> str:
    dt = parse_time(value)
    if not dt:
        return ""
    sec = time.time() - dt.timestamp()
    if sec < 0:
        return local_time(value)
    if sec < 3600:
        return f"{max(1, int(sec // 60))}分钟前"
    if sec < 86400:
        return f"{int(sec // 3600)}小时前"
    if sec < 86400 * 30:
        return f"{int(sec // 86400)}天前"
    return local_time(value, fmt="%Y-%m-%d")


def freshness(created_at: float) -> str:
    """Exact update time in the configured timezone, e.g. 数据更新于 11-06 18:35."""
    dt = datetime.fromtimestamp(created_at or time.time(), tz=get_tz(_DEFAULT_TZ[0]))
    fmt = "%m-%d %H:%M" if dt.year == datetime.now(dt.tzinfo).year else "%Y-%m-%d %H:%M"
    return f"数据更新于 {dt.strftime(fmt)}"


TIER_EMBLEM = "https://opgg-static.akamaized.net/images/medals_new/{}.png"


def tier_emblem(tier: str | None) -> str | None:
    if not tier or str(tier).upper() in ("NONE", "UNRANKED"):
        return None
    return TIER_EMBLEM.format(str(tier).lower())


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = s.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
    return re.sub(r"\s+", " ", _TAG_RE.sub("", s)).strip()


STAT_MODS = {
    5001: "生命成长", 5002: "护甲", 5003: "魔抗", 5005: "攻速", 5007: "技能急速",
    5008: "适应之力", 5010: "移速", 5011: "生命值", 5013: "韧性",
}


def rate_cls(x, good: float = 0.53, bad: float = 0.47) -> str:
    """Colour class for a win rate: hi (green) / lo (red) / ''."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    if v > 1:
        v /= 100
    return "hi" if v >= good else ("lo" if v <= bad else "")


def placement_cls(x, good: float = 3.6, bad: float = 4.4) -> str:
    """Colour class for a TFT average placement (lower is better)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    return "hi" if v <= good else ("lo" if v >= bad else "")


def relative_cls(values, lower_is_better: bool = True, share: float = 0.25) -> list[str]:
    """Colour the best / worst quarter of a column relative to each other."""
    nums = []
    for v in values:
        try:
            nums.append(float(v))
        except (TypeError, ValueError):
            nums.append(None)
    valid = sorted((n, i) for i, n in enumerate(nums) if n is not None)
    if len(valid) < 3 or valid[0][0] == valid[-1][0]:
        return [""] * len(nums)
    if not lower_is_better:
        valid.reverse()
    k = max(1, round(len(valid) * share))
    out = [""] * len(nums)
    for _, i in valid[:k]:
        out[i] = "hi"
    for _, i in valid[-k:]:
        out[i] = "lo"
    return out


def kda_cls(k, d, a) -> str:
    """OP.GG style KDA colours: >=5 orange, >=4 blue, >=3 teal."""
    try:
        k, d, a = int(k or 0), int(d or 0), int(a or 0)
    except (TypeError, ValueError):
        return ""
    v = (k + a) / d if d else 99
    return "kda5" if v >= 5 else ("kda4" if v >= 4 else ("kda3" if v >= 3 else ""))


def tier_pill(tier) -> str:
    """Pill class for LoL tiers 0..5 (0 = OP)."""
    if tier in (None, ""):
        return ""
    return "op" if str(tier) == "0" else f"tp t{tier}"


TFT_TIER_PILL = {"OP": "op", "S": "tp t1", "A": "tp t2", "B": "tp t3", "C": "tp t4", "D": "tp t5"}


def rank_cls(rank) -> str:
    return {1: "r1", 2: "r2", 3: "r3"}.get(int(rank)) if str(rank).isdigit() and int(rank) <= 3 else ""

