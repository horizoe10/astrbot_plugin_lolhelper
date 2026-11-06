"""Typed view of the plugin configuration (_conf_schema.json)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MD_PLATFORMS = ["telegram", "discord", "lark", "kook", "dingtalk", "slack", "mattermost", "webchat"]


def _group(conf: dict, name: str) -> dict:
    v = conf.get(name) if isinstance(conf, dict) else None
    return v if isinstance(v, dict) else {}


def _lines_to_map(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (text or "").replace("，", ",").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        for part in line.split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip() and v.strip():
                    out[k.strip()] = v.strip()
    return out


def _json_map(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return _lines_to_map(text)


@dataclass
class Settings:
    # data source
    mcp_endpoint: str = "https://mcp-api.op.gg/mcp"
    lang: str = "zh_CN"
    timeout: float = 20
    concurrency: int = 4
    min_interval: float = 0.15
    tool_overrides: dict[str, str] = field(default_factory=dict)
    # network
    proxy: str = ""
    ddragon_base: str = "https://ddragon.leagueoflegends.com"
    cdragon_base: str = "https://raw.communitydragon.org"
    gamedata_refresh_hours: float = 24
    # output
    default_mode: str = "image"
    markdown_platforms: list[str] = field(default_factory=lambda: list(DEFAULT_MD_PLATFORMS))
    text_max_rows: int = 10
    image_max_rows: int = 15
    image_renderer: str = "auto"
    image_theme: str = "dark"
    image_quality: int = 85
    image_fallback_markdown: bool = True
    image_inline_icons: bool = True
    show_footer: bool = True
    # query
    default_region: str = "kr"
    match_count: int = 8
    match_details: bool = True
    timezone: str = "Asia/Shanghai"
    # cache (seconds)
    ttl_meta: float = 3 * 3600
    ttl_build: float = 3600
    ttl_profile: float = 300
    ttl_matches: float = 120
    ttl_esports: float = 300
    ttl_tft: float = 3600
    # china
    cn_enabled: bool = True
    cn_bridge_url: str = "http://127.0.0.1:6198"
    cn_bridge_token: str = ""
    cn_timeout: float = 15
    cn_default_server: str = ""
    cn_servers_extra: dict[str, tuple[str, str]] = field(default_factory=dict)
    # local api
    api_enabled: bool = False
    api_host: str = "127.0.0.1"
    api_port: int = 6197
    api_token: str = ""
    api_allow_push: bool = False
    api_allow_mcp: bool = False
    # push
    push_enabled: bool = True
    push_match_interval: float = 300
    push_esports_interval: float = 600
    push_esports_remind: float = 1800
    push_daily_hour: int = 10
    push_max_new_matches: int = 3
    push_group_admin_only: bool = False
    # aliases
    champion_aliases: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_config(cls, conf: dict | None) -> "Settings":
        conf = conf or {}
        s = cls()
        ds, net, out, q, cache = (_group(conf, k) for k in ("data_source", "network", "output", "query", "cache"))
        cn, api, push, al = (_group(conf, k) for k in ("cn", "local_api", "push", "aliases"))

        def g(d: dict, key: str, default):
            v = d.get(key, default)
            return default if v is None or v == "" and not isinstance(default, str) else v

        s.mcp_endpoint = g(ds, "mcp_endpoint", s.mcp_endpoint) or s.mcp_endpoint
        s.lang = g(ds, "lang", s.lang) or s.lang
        s.timeout = float(g(ds, "timeout", s.timeout))
        s.concurrency = int(g(ds, "concurrency", s.concurrency))
        s.min_interval = float(g(ds, "min_interval_ms", 150)) / 1000
        s.tool_overrides = {str(k): str(v) for k, v in _json_map(g(ds, "tool_overrides", "")).items()}

        s.proxy = str(g(net, "proxy", "")).strip()
        s.ddragon_base = g(net, "ddragon_base", s.ddragon_base) or s.ddragon_base
        s.cdragon_base = g(net, "cdragon_base", s.cdragon_base) or s.cdragon_base
        s.gamedata_refresh_hours = float(g(net, "gamedata_refresh_hours", s.gamedata_refresh_hours))

        mode = str(g(out, "default_mode", s.default_mode)).lower()
        s.default_mode = mode if mode in ("text", "markdown", "image") else "image"
        mdp = g(out, "markdown_platforms", s.markdown_platforms)
        s.markdown_platforms = [str(x).strip() for x in mdp if str(x).strip()] if isinstance(mdp, list) else s.markdown_platforms
        s.text_max_rows = int(g(out, "text_max_rows", s.text_max_rows))
        s.image_max_rows = int(g(out, "image_max_rows", s.image_max_rows))
        s.image_renderer = str(g(out, "image_renderer", s.image_renderer))
        s.image_theme = str(g(out, "image_theme", s.image_theme))
        s.image_quality = int(g(out, "image_quality", s.image_quality))
        s.image_fallback_markdown = bool(g(out, "image_fallback_markdown", s.image_fallback_markdown))
        s.image_inline_icons = bool(g(out, "inline_icons", s.image_inline_icons))
        s.show_footer = bool(g(out, "show_footer", s.show_footer))

        s.default_region = str(g(q, "default_region", s.default_region)).lower() or "kr"
        s.match_count = int(g(q, "match_count", s.match_count))
        s.match_details = bool(g(q, "match_details", s.match_details))
        s.timezone = str(g(q, "timezone", s.timezone)) or s.timezone

        s.ttl_meta = float(g(cache, "meta_ttl_min", 180)) * 60
        s.ttl_build = float(g(cache, "build_ttl_min", 60)) * 60
        s.ttl_profile = float(g(cache, "profile_ttl_sec", 300))
        s.ttl_matches = float(g(cache, "matches_ttl_sec", 120))
        s.ttl_esports = float(g(cache, "esports_ttl_sec", 300))
        s.ttl_tft = float(g(cache, "tft_ttl_min", 60)) * 60

        s.cn_enabled = bool(g(cn, "enabled", s.cn_enabled))
        s.cn_bridge_url = str(g(cn, "bridge_url", s.cn_bridge_url)).strip()
        s.cn_bridge_token = str(g(cn, "bridge_token", "")).strip()
        s.cn_timeout = float(g(cn, "timeout", s.cn_timeout))
        s.cn_default_server = str(g(cn, "default_server", "")).strip()
        s.cn_servers_extra = {k.upper(): (v, "") for k, v in _lines_to_map(g(cn, "servers_extra", "")).items()}

        s.api_enabled = bool(g(api, "enabled", s.api_enabled))
        s.api_host = str(g(api, "host", s.api_host)) or "127.0.0.1"
        s.api_port = int(g(api, "port", s.api_port))
        s.api_token = str(g(api, "token", "")).strip()
        s.api_allow_push = bool(g(api, "allow_push", s.api_allow_push))
        s.api_allow_mcp = bool(g(api, "allow_mcp_passthrough", s.api_allow_mcp))

        s.push_enabled = bool(g(push, "enabled", s.push_enabled))
        s.push_match_interval = max(60.0, float(g(push, "match_interval_min", 5)) * 60)
        s.push_esports_interval = max(120.0, float(g(push, "esports_interval_min", 10)) * 60)
        s.push_esports_remind = float(g(push, "esports_remind_min", 30)) * 60
        s.push_daily_hour = int(g(push, "daily_hour", s.push_daily_hour)) % 24
        s.push_max_new_matches = int(g(push, "max_new_matches", s.push_max_new_matches))
        s.push_group_admin_only = bool(g(push, "group_admin_only", s.push_group_admin_only))

        s.champion_aliases = _lines_to_map(g(al, "champion_aliases", ""))
        return s

