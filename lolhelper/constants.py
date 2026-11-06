"""Static tables: regions, positions, tiers, queues and China servers.

China server data follows League Akari's built-in SGP configuration
(src/main/shards/akari-api/builtin.ts) and its platform name table
(src/shared/utils/platform-names.ts).
"""

from __future__ import annotations

# OP.GG regions accepted by the MCP summoner tools.
OPGG_REGIONS: dict[str, str] = {
    "kr": "韩服",
    "na": "北美",
    "euw": "西欧",
    "eune": "北欧&东欧",
    "jp": "日服",
    "br": "巴西",
    "lan": "拉美北",
    "las": "拉美南",
    "oce": "大洋洲",
    "ru": "俄服",
    "tr": "土服",
    "sg": "新加坡",
    "ph": "菲律宾",
    "tw": "台服",
    "vn": "越南",
    "th": "泰服",
    "me": "中东",
}

REGION_ALIASES: dict[str, str] = {
    "韩服": "kr", "韩国": "kr", "kr1": "kr",
    "美服": "na", "北美": "na", "na1": "na",
    "欧服": "euw", "西欧": "euw", "euw1": "euw",
    "北欧": "eune", "东欧": "eune", "eun1": "eune",
    "日服": "jp", "日本": "jp", "jp1": "jp",
    "巴西": "br", "br1": "br",
    "拉美北": "lan", "la1": "lan",
    "拉美南": "las", "la2": "las",
    "大洋洲": "oce", "澳服": "oce", "oc1": "oce",
    "俄服": "ru", "ru1": "ru",
    "土服": "tr", "tr1": "tr",
    "新加坡": "sg", "sg2": "sg",
    "菲律宾": "ph", "ph2": "ph",
    "台服": "tw", "台湾": "tw", "tw2": "tw",
    "越南": "vn", "越服": "vn", "vn2": "vn",
    "泰服": "th", "泰国": "th", "th2": "th",
    "中东": "me", "me1": "me",
}


def resolve_opgg_region(token: str) -> str | None:
    if not token:
        return None
    t = token.strip().lower()
    if t in OPGG_REGIONS:
        return t
    return REGION_ALIASES.get(t) or REGION_ALIASES.get(token.strip())


# --- China (Tencent) servers -------------------------------------------------

# sgp server id -> (display name, sgp base url). Same values as League Akari.
CN_SERVERS: dict[str, tuple[str, str]] = {
    "TENCENT_HN1": ("艾欧尼亚", "https://hn1-k8s-sgp.lol.qq.com:21019"),
    "TENCENT_HN10": ("黑色玫瑰", "https://hn10-k8s-sgp.lol.qq.com:21019"),
    "TENCENT_NJ100": ("联盟一区", "https://nj100-sgp.lol.qq.com:21019"),
    "TENCENT_GZ100": ("联盟二区", "https://gz100-sgp.lol.qq.com:21019"),
    "TENCENT_CQ100": ("联盟三区", "https://cq100-sgp.lol.qq.com:21019"),
    "TENCENT_TJ100": ("联盟四区", "https://tj100-sgp.lol.qq.com:21019"),
    "TENCENT_TJ101": ("联盟五区", "https://tj101-sgp.lol.qq.com:21019"),
    "TENCENT_BGP2": ("峡谷之巅", "https://bgp2-k8s-sgp.lol.qq.com:21019"),
    "TENCENT_PBE": ("体验服", "https://pbe-sgp.lol.qq.com:21019"),
}

# Old servers were merged into the five "联盟 X 区" servers.
CN_LEGACY_SERVERS: dict[str, str] = {
    "祖安": "TENCENT_NJ100", "皮尔特沃夫": "TENCENT_NJ100", "巨神峰": "TENCENT_NJ100",
    "教育网": "TENCENT_NJ100", "男爵领域": "TENCENT_NJ100", "均衡教派": "TENCENT_NJ100",
    "影流": "TENCENT_NJ100", "守望之海": "TENCENT_NJ100",
    "卡拉曼达": "TENCENT_GZ100", "暗影岛": "TENCENT_GZ100", "征服之海": "TENCENT_GZ100",
    "诺克萨斯": "TENCENT_GZ100", "战争学院": "TENCENT_GZ100", "雷瑟守备": "TENCENT_GZ100",
    "班德尔城": "TENCENT_CQ100", "裁决之地": "TENCENT_CQ100", "水晶之痕": "TENCENT_CQ100",
    "钢铁烈阳": "TENCENT_CQ100", "皮城警备": "TENCENT_CQ100",
    "比尔吉沃特": "TENCENT_TJ100", "弗雷尔卓德": "TENCENT_TJ100", "扭曲丛林": "TENCENT_TJ100",
    "德玛西亚": "TENCENT_TJ101", "无畏先锋": "TENCENT_TJ101", "恕瑞玛": "TENCENT_TJ101",
    "巨龙之巢": "TENCENT_TJ101",
}

CN_SERVER_SHORT_ALIASES: dict[str, str] = {
    "艾欧": "TENCENT_HN1", "电一": "TENCENT_HN1",
    "黑玫": "TENCENT_HN10", "网通": "TENCENT_HN10",
    "峡谷": "TENCENT_BGP2",
    "联一": "TENCENT_NJ100", "一区": "TENCENT_NJ100",
    "联二": "TENCENT_GZ100", "二区": "TENCENT_GZ100",
    "联三": "TENCENT_CQ100", "三区": "TENCENT_CQ100",
    "联四": "TENCENT_TJ100", "四区": "TENCENT_TJ100",
    "联五": "TENCENT_TJ101", "五区": "TENCENT_TJ101",
}

CN_GENERIC_WORDS = ("国服", "腾讯", "cn", "tencent")


def resolve_cn_server(token: str, extra: dict[str, tuple[str, str]] | None = None) -> str | None:
    """Return a TENCENT_* id for a user-typed server token, or None."""
    if not token:
        return None
    servers = dict(CN_SERVERS)
    if extra:
        servers.update(extra)
    raw = token.strip()
    up = raw.upper()
    if up in servers:
        return up
    if f"TENCENT_{up}" in servers:
        return f"TENCENT_{up}"
    for sid, (name, _) in servers.items():
        if raw == name:
            return sid
    if raw in CN_LEGACY_SERVERS:
        return CN_LEGACY_SERVERS[raw]
    if raw in CN_SERVER_SHORT_ALIASES:
        return CN_SERVER_SHORT_ALIASES[raw]
    return None


def cn_server_name(server_id: str, extra: dict[str, tuple[str, str]] | None = None) -> str:
    servers = dict(CN_SERVERS)
    if extra:
        servers.update(extra)
    item = servers.get((server_id or "").upper())
    return item[0] if item else (server_id or "未知大区")


# --- Positions / modes --------------------------------------------------------

POSITIONS: dict[str, str] = {
    "top": "上单",
    "jungle": "打野",
    "mid": "中单",
    "adc": "下路",
    "support": "辅助",
}

POSITION_ALIASES: dict[str, str] = {
    "上": "top", "上单": "top", "上路": "top", "top": "top",
    "野": "jungle", "打野": "jungle", "jg": "jungle", "jug": "jungle", "jungle": "jungle",
    "中": "mid", "中单": "mid", "中路": "mid", "mid": "mid", "middle": "mid",
    "下": "adc", "下路": "adc", "射手": "adc", "adc": "adc", "ad": "adc", "bot": "adc",
    "bottom": "adc",
    "辅": "support", "辅助": "support", "sup": "support", "support": "support",
    "utility": "support",
}

# Positions returned by OP.GG / Riot data, normalised to our keys.
POSITION_FROM_API: dict[str, str] = {
    "TOP": "top", "JUNGLE": "jungle", "MID": "mid", "MIDDLE": "mid",
    "ADC": "adc", "BOTTOM": "adc", "BOT": "adc", "SUPPORT": "support", "UTILITY": "support",
}

GAME_MODES: dict[str, str] = {
    "ranked": "单双排",
    "flex": "灵活组排",
    "aram": "极地大乱斗",
    "urf": "无限火力",
    "nexus_blitz": "极限闪击",
    "mayhem": "海克斯大乱斗",
}

GAME_MODE_ALIASES: dict[str, str] = {
    "排位": "ranked", "单排": "ranked", "单双排": "ranked", "ranked": "ranked",
    "灵活": "flex", "灵活排位": "flex", "组排": "flex", "flex": "flex",
    "大乱斗": "aram", "乱斗": "aram", "aram": "aram",
    "海克斯大乱斗": "mayhem", "海克斯乱斗": "mayhem", "海克斯": "mayhem", "mayhem": "mayhem",
    "无限火力": "urf", "无限": "urf", "urf": "urf",
    "闪击": "nexus_blitz", "极限闪击": "nexus_blitz",
}

# --- Ranked tiers ---------------------------------------------------------------

TIERS_ZH: dict[str, str] = {
    "IRON": "坚韧黑铁",
    "BRONZE": "英勇黄铜",
    "SILVER": "不屈白银",
    "GOLD": "荣耀黄金",
    "PLATINUM": "华贵铂金",
    "EMERALD": "流光翡翠",
    "DIAMOND": "璀璨钻石",
    "MASTER": "超凡大师",
    "GRANDMASTER": "傲世宗师",
    "CHALLENGER": "最强王者",
}

DIVISION_ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV"}

APEX_TIERS = {"MASTER", "GRANDMASTER", "CHALLENGER"}

QUEUE_TYPES_ZH: dict[str, str] = {
    "SOLORANKED": "单双排",
    "RANKED_SOLO_5x5": "单双排",
    "FLEXRANKED": "灵活组排",
    "RANKED_FLEX_SR": "灵活组排",
    "ARENA": "斗魂竞技场",
    "CHERRY": "斗魂竞技场",
    "RANKED_TFT": "云顶之弈",
    "RANKED_TFT_DOUBLE_UP": "云顶双人",
    "RANKED_TFT_TURBO": "云顶狂暴",
    "NORMAL": "匹配",
    "ARAM": "极地大乱斗",
    "ARAM_MAYHEM": "海克斯大乱斗",
    "KIWI": "海克斯大乱斗",
    "URF": "无限火力",
    "BOT": "人机",
    "CLASH": "冠军杯赛",
}

QUEUE_ID_ZH: dict[int, str] = {
    0: "自定义",
    400: "匹配(征召)",
    420: "单双排",
    430: "匹配",
    440: "灵活组排",
    450: "极地大乱斗",
    480: "快速匹配",
    490: "快速匹配",
    700: "冠军杯赛",
    720: "冠军杯赛(大乱斗)",
    830: "人机入门",
    840: "人机新手",
    850: "人机一般",
    870: "人机入门",
    880: "人机新手",
    890: "人机一般",
    900: "无限火力",
    1020: "克隆模式",
    1090: "云顶之弈(匹配)",
    1100: "云顶之弈(排位)",
    1130: "云顶之弈(狂暴)",
    1160: "云顶之弈(双人)",
    1300: "极限闪击",
    1400: "终极魔典",
    1700: "斗魂竞技场",
    1710: "斗魂竞技场",
    1900: "无限火力",
    2300: "神木之门",
    2400: "海克斯大乱斗",
}

ESPORTS_LEAGUES = [
    "lck", "kespa", "ewc", "lpl", "lec", "lcs", "ljl", "vcs", "cblol", "lcl", "lla", "tcl",
    "pcs", "lco", "lta south", "lta north", "lcp", "first stand", "fst", "al", "msi",
    "worlds", "lta",
]

ESPORTS_LEAGUE_ALIASES: dict[str, str] = {
    "全球总决赛": "worlds", "世界赛": "worlds", "s赛": "worlds", "总决赛": "worlds",
    "季中赛": "msi", "季中冠军赛": "msi",
    "电竞世界杯": "ewc",
    "先锋赛": "first stand",
}


def resolve_league(token: str) -> str | None:
    if not token:
        return None
    t = token.strip().lower()
    if t in ESPORTS_LEAGUES:
        return t
    return ESPORTS_LEAGUE_ALIASES.get(t)



# --------------------------------------------------------------- esports leagues
_SYM = "https://s-qwer.op.gg/images/symbols/"
_LG = "https://s-qwer.op.gg/images/lol/leagues/"
# key -> (short Chinese name, full Chinese name, theme color, logo for dark backgrounds)
LEAGUE_INFO: dict[str, tuple[str, str, str, str]] = {
    "worlds": ("全球总决赛", "英雄联盟全球总决赛", "#3b7bff", _SYM + "WORLD@white.png"),
    "msi": ("季中冠军赛", "英雄联盟季中冠军赛", "#d4a73c", _SYM + "MSI@white.png"),
    "first stand": ("先锋赛", "英雄联盟先锋赛", "#f07a28", _LG + "141_1783606937219.png"),
    "ewc": ("电竞世界杯", "电竞世界杯", "#22c55e", _SYM + "EWC.png"),
    "lpl": ("LPL", "LPL 中国职业联赛", "#e5262f", _SYM + "LPL@white.png"),
    "lck": ("LCK", "LCK 韩国冠军联赛", "#19c3e6", _SYM + "LCK@white.png"),
    "lec": ("LEC", "LEC 欧洲冠军联赛", "#14d9b8", _SYM + "LEC@white.png"),
    "lcs": ("LCS", "LCS 北美冠军联赛", "#6a7dff", _SYM + "LCS@white.png"),
    "lcp": ("LCP", "LCP 亚太冠军联赛", "#ff5a8a", _LG + "138_1783606935767.png"),
    "lta north": ("LTA 北区", "LTA 美洲冠军联赛 · 北区", "#f5a623", _LG + "140_1783606938094.png"),
    "lta south": ("LTA 南区", "LTA 美洲冠军联赛 · 南区", "#f5a623", _LG + "139_1783606939593.png"),
    "cblol": ("CBLOL", "CBLOL 巴西联赛", "#84cc16", _SYM + "CBLOL@white.png"),
    "vcs": ("VCS", "VCS 越南联赛", "#facc15", _SYM + "VCS@white.png"),
    "ljl": ("LJL", "LJL 日本联赛", "#e11d48", _SYM + "LJL@white.png"),
    "kespa": ("KeSPA 杯", "KeSPA 杯", "#3b82f6", _SYM + "KESPA.png"),
    "al": ("AL", "AL 阿拉伯联赛", "#0ea5a4", _SYM + "AL.png"),
    "tcl": ("TCL", "TCL 土耳其联赛", "#ef4444", _SYM + "TCL@white.png"),
}
_LEAGUE_KEY_ALIAS = {"fst": "first stand", "world championship": "worlds", "mid-season invitational": "msi"}


def league_info(name: str | None) -> dict:
    """Display info for a league name as returned by OP.GG ("Worlds", "LPL", "LTA North" ...)."""
    raw = str(name or "").strip()
    key = raw.lower()
    key = _LEAGUE_KEY_ALIAS.get(key, key)
    info = LEAGUE_INFO.get(key)
    if not info:
        return {"key": key, "short": raw, "full": raw, "color": "", "logo": None}
    short, full, color, logo = info
    return {"key": key, "short": short, "full": full, "color": color, "logo": logo}


_STAGE_RULES: list[tuple[str, str]] = [
    (r"\bgrand finals?\b", "总决赛"),
    (r"\b(upper|winners?)\s+bracket\b", "胜者组"),
    (r"\b(lower|losers?)\s+bracket\b", "败者组"),
    (r"\bquarter-?finals?\b", "八强赛"),
    (r"\bsemi-?finals?\b", "半决赛"),
    (r"\bfinals?\b", "决赛"),
    (r"\bplay-?ins?\b", "入围赛"),
    (r"\bplay-?offs?\b", "季后赛"),
    (r"\bswiss stage\b", "瑞士轮"),
    (r"\bswiss\b", "瑞士轮"),
    (r"\s*\bstage\b", ""),
    (r"\bregional merit match\b", "区域资格赛"),
    (r"\btie-?breakers?\b", "加赛"),
    (r"\bqualifiers?\b", "资格赛"),
    (r"\bregular season\b", "常规赛"),
    (r"\bsplit\s+(\d+)\b", r"第\1赛段"),
    (r"\bspring\b", "春季赛"),
    (r"\bsummer\b", "夏季赛"),
    (r"\bgroup\s+([a-z])\b", r"\1 组"),
    (r"\bround\s+(\d+)\b", r"第\1轮"),
    (r"\bweek\s+(\d+)\b", r"第\1周"),
    (r"\bday\s+(\d+)\b", r"第\1天"),
    (r"\bmatch\s+(\d+)\b", r"第\1场"),
]


def stage_zh(stage: str | None) -> str:
    """Translate OP.GG stage names, e.g. "Quarterfinal 1" -> "八强赛 第1场"."""
    import re

    s = str(stage or "").strip()
    for pat, rep in _STAGE_RULES:
        s = re.sub(pat, rep, s, flags=re.I)
    s = re.sub(r"(八强赛|半决赛|决赛|总决赛)\s*(\d+)$", r"\1 第\2场", s)
    return re.sub(r"\s{2,}", " ", s).strip()
