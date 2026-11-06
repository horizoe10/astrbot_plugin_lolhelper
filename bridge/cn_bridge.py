#!/usr/bin/env python3
"""联盟助手国服本地代理 (China server bridge) v1.0

OP.GG does not cover Tencent servers. This bridge runs on a Windows PC where
the Tencent League client (WeGame) is logged in and exposes a small HTTP API
the AstrBot plugin can call.

How it works (same approach as League Akari):
1. Find the League client (LCU): lockfile in the install dir (found through
   HKCU\\Software\\Tencent\\LOL\\InstallPath), or the LeagueClientUx.exe
   command line (--app-port / --remoting-auth-token / --rso_platform_id).
2. Read two tokens from the LCU:
   /entitlements/v1/token                    -> match-history-query
   /lol-league-session/v1/league-session-token -> summoner-ledge, leagues-ledge
3. Call SGP (https://<server>-sgp.lol.qq.com:21019) with those tokens.

Only the Python standard library is used. Run:

    python cn_bridge.py              # creates bridge.json on first run
    python cn_bridge.py --config D:\\path\\bridge.json
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import logging
import os
import re
import secrets
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSION = "1.0"
log = logging.getLogger("lolhelper-cn-bridge")

# Same table as League Akari builtin.ts (BUILTIN_SGP_LEAGUE_SERVERS_CONFIG).
SGP_SERVERS = {
    "TENCENT_HN1": {"name": "艾欧尼亚", "url": "https://hn1-k8s-sgp.lol.qq.com:21019"},
    "TENCENT_HN10": {"name": "黑色玫瑰", "url": "https://hn10-k8s-sgp.lol.qq.com:21019"},
    "TENCENT_NJ100": {"name": "联盟一区", "url": "https://nj100-sgp.lol.qq.com:21019"},
    "TENCENT_GZ100": {"name": "联盟二区", "url": "https://gz100-sgp.lol.qq.com:21019"},
    "TENCENT_CQ100": {"name": "联盟三区", "url": "https://cq100-sgp.lol.qq.com:21019"},
    "TENCENT_TJ100": {"name": "联盟四区", "url": "https://tj100-sgp.lol.qq.com:21019"},
    "TENCENT_TJ101": {"name": "联盟五区", "url": "https://tj101-sgp.lol.qq.com:21019"},
    "TENCENT_BGP2": {"name": "峡谷之巅", "url": "https://bgp2-k8s-sgp.lol.qq.com:21019"},
    "TENCENT_PBE": {"name": "体验服", "url": "https://pbe-sgp.lol.qq.com:21019"},
}

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 6198,
    "token": "",
    "lockfile": "",
    "install_dir": "",
    "sgp_verify_tls": True,
    "sgp_servers": {},
    "allow_raw_lcu": False,
    "request_timeout": 15,
    "log_level": "INFO",
}

PROCESS_NAME = "LeagueClientUx.exe"
RE_PORT = re.compile(r"--app-port=(\d+)")
RE_TOKEN = re.compile(r"--remoting-auth-token=([\w\-_]+)")
RE_PID = re.compile(r"--app-pid=(\d+)")
RE_PLATFORM = re.compile(r"--rso[_-]platform[_-]id=([\w\-_]+)", re.I)
RE_REGION = re.compile(r"--region=([\w\-_]+)")


class BridgeError(Exception):
    def __init__(self, message: str, status: int = 502, code: str = "upstream_error"):
        super().__init__(message)
        self.status = status
        self.code = code


# --------------------------------------------------------------------------- LCU
class LcuAuth:
    def __init__(self, port: int, token: str, pid: int = 0, platform: str = "", region: str = "", source: str = ""):
        self.port = port
        self.token = token
        self.pid = pid
        self.platform = platform.upper()
        self.region = region.upper()
        self.source = source

    def __repr__(self) -> str:
        return f"LcuAuth(port={self.port}, pid={self.pid}, platform={self.platform}, source={self.source})"


def _run(cmd: list[str], timeout: int = 8) -> str:
    try:
        flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        out = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=flags)
        for enc in ("utf-8", "gbk"):
            try:
                return out.stdout.decode(enc)
            except UnicodeDecodeError:
                continue
        return out.stdout.decode("utf-8", "ignore")
    except Exception as exc:
        log.debug("run %s failed: %s", cmd[0], exc)
        return ""


def _from_cmdline(text: str, source: str) -> LcuAuth | None:
    port, token = RE_PORT.search(text), RE_TOKEN.search(text)
    if not port or not token:
        return None
    pid = RE_PID.search(text)
    plat = RE_PLATFORM.search(text)
    reg = RE_REGION.search(text)
    return LcuAuth(
        int(port.group(1)),
        token.group(1),
        int(pid.group(1)) if pid else 0,
        plat.group(1) if plat else "",
        reg.group(1) if reg else "",
        source,
    )


def _tencent_install_dirs(cfg: dict) -> list[Path]:
    dirs: list[Path] = []
    if cfg.get("install_dir"):
        dirs.append(Path(cfg["install_dir"]))
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Tencent\LOL") as key:
                value, _ = winreg.QueryValueEx(key, "InstallPath")
                if value:
                    dirs.append(Path(value))
        except OSError:
            pass
        for drive in "CDEFGH":
            dirs.append(Path(f"{drive}:/WeGameApps/英雄联盟"))
    return dirs


def discover_lcu(cfg: dict) -> LcuAuth | None:
    # 1) explicit or detected lockfile (no admin rights needed)
    candidates: list[Path] = []
    if cfg.get("lockfile"):
        candidates.append(Path(cfg["lockfile"]))
    for d in _tencent_install_dirs(cfg):
        candidates += [d / "LeagueClient" / "lockfile", d / "lockfile"]
    for lf in candidates:
        try:
            if lf.is_file():
                parts = lf.read_text(encoding="utf-8").strip().split(":")
                if len(parts) >= 5:
                    return LcuAuth(int(parts[2]), parts[3], int(parts[1]), source=f"lockfile:{lf}")
        except OSError:
            continue
    # 2) process command line (Tencent client runs elevated -> run bridge as admin)
    if os.name == "nt":
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='" + PROCESS_NAME + "'\" "
            "| Select-Object -ExpandProperty CommandLine"
        )
        text = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps])
        if not text.strip():
            text = _run(["wmic", "process", "where", f"name='{PROCESS_NAME}'", "get", "commandline"])
    else:
        text = _run(["ps", "-A", "-o", "args"])
    for line in text.splitlines():
        if "--app-port" in line:
            auth = _from_cmdline(line, "cmdline")
            if auth:
                return auth
    return None


class Lcu:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.auth: LcuAuth | None = None
        self.ctx = ssl.create_default_context()
        self.ctx.check_hostname = False
        self.ctx.verify_mode = ssl.CERT_NONE  # LCU uses a self-signed Riot certificate
        self.lock = threading.Lock()
        self._last_discover = 0.0

    def _ensure(self, force: bool = False) -> LcuAuth:
        with self.lock:
            if self.auth and not force:
                return self.auth
            if not force and time.time() - self._last_discover < 3 and not self.auth:
                raise BridgeError("未检测到已登录的英雄联盟客户端", 503, "client_not_found")
            self._last_discover = time.time()
            auth = discover_lcu(self.cfg)
            if not auth:
                self.auth = None
                raise BridgeError("未检测到已登录的英雄联盟客户端（请启动并登录国服客户端；若用命令行方式检测需管理员权限运行）", 503, "client_not_found")
            self.auth = auth
            self._fill_platform(auth)
            log.info("connected to LCU %s", auth)
            return auth

    def _fill_platform(self, auth: LcuAuth) -> None:
        if auth.platform:
            return
        try:
            args = self._raw(auth, "GET", "/riotclient/command-line-args")
            joined = " ".join(args) if isinstance(args, list) else str(args)
            m, r = RE_PLATFORM.search(joined), RE_REGION.search(joined)
            auth.platform = (m.group(1) if m else "").upper()
            auth.region = (r.group(1) if r else auth.region).upper()
        except Exception:
            pass
        if not auth.platform:
            try:
                pid = self._raw(auth, "GET", "/lol-platform-config/v1/namespaces/LoginDataPacket/platformId")
                auth.platform = str(pid or "").upper()
            except Exception:
                pass

    def _raw(self, auth: LcuAuth, method: str, path: str, body=None):
        url = f"https://127.0.0.1:{auth.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", "Basic " + base64.b64encode(f"riot:{auth.token}".encode()).decode())
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, context=self.ctx, timeout=self.cfg["request_timeout"]) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None

    def request(self, method: str, path: str, body=None):
        for attempt in range(2):
            auth = self._ensure(force=attempt > 0)
            try:
                return self._raw(auth, method, path, body)
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:300].decode("utf-8", "ignore")
                if exc.code == 404:
                    raise BridgeError(f"LCU 404: {detail}", 404, "not_found")
                raise BridgeError(f"LCU HTTP {exc.code}: {detail}", 502, "lcu_error")
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
                self.auth = None  # client restarted -> rediscover once
                if attempt:
                    raise BridgeError("无法连接英雄联盟客户端", 503, "client_not_found")
        raise BridgeError("无法连接英雄联盟客户端", 503, "client_not_found")

    @property
    def platform(self) -> str:
        return self._ensure().platform

    def sgp_server_id(self) -> str:
        auth = self._ensure()
        return f"TENCENT_{auth.platform}" if auth.platform else ""


# --------------------------------------------------------------------------- SGP
class Tokens:
    def __init__(self, lcu: Lcu):
        self.lcu = lcu
        self._cache: dict[str, tuple[float, str]] = {}
        self.lock = threading.Lock()

    def get(self, kind: str, force: bool = False) -> str:
        with self.lock:
            hit = self._cache.get(kind)
            if hit and not force and time.time() - hit[0] < 60:
                return hit[1]
            if kind == "entitlements":
                data = self.lcu.request("GET", "/entitlements/v1/token")
                token = (data or {}).get("accessToken") or ""
            else:
                token = self.lcu.request("GET", "/lol-league-session/v1/league-session-token") or ""
            if not token:
                raise BridgeError(f"客户端未提供 {kind} 令牌（请确认已登录进入客户端主界面）", 503, "token_missing")
            self._cache[kind] = (time.time(), token)
            return token


class Sgp:
    def __init__(self, cfg: dict, tokens: Tokens):
        self.cfg = cfg
        self.tokens = tokens
        self.servers = {**SGP_SERVERS, **{k.upper(): v for k, v in (cfg.get("sgp_servers") or {}).items()}}
        self.ctx = ssl.create_default_context()
        if not cfg.get("sgp_verify_tls", True):
            self.ctx.check_hostname = False
            self.ctx.verify_mode = ssl.CERT_NONE

    def request(self, server_id: str, path: str, token_kind: str, method: str = "GET", body=None, params=None):
        server = self.servers.get(server_id.upper())
        if not server:
            raise BridgeError(f"未知大区 {server_id}", 400, "bad_server")
        sub = server_id.split("_", 1)[1] if server_id.upper().startswith("TENCENT_") else server_id
        url = server["url"].rstrip("/") + path.replace("{sub}", sub)
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(2):
            token = self.tokens.get(token_kind, force=attempt > 0)
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", f"Bearer {token}")
            req.add_header("Accept", "application/json")
            if data is not None:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, context=self.ctx, timeout=self.cfg["request_timeout"]) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403) and attempt == 0:
                    continue  # token expired -> refresh once
                detail = exc.read()[:300].decode("utf-8", "ignore")
                status = 404 if exc.code == 404 else 502
                raise BridgeError(f"SGP HTTP {exc.code}: {detail}", status, "sgp_error")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise BridgeError(f"连接 SGP 失败：{exc}", 502, "sgp_unreachable")
        raise BridgeError("SGP 鉴权失败", 502, "sgp_auth")


# ----------------------------------------------------------------------- service
class Bridge:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lcu = Lcu(cfg)
        self.tokens = Tokens(self.lcu)
        self.sgp = Sgp(cfg, self.tokens)

    def _server(self, q: dict) -> str:
        sid = (q.get("server") or "").upper()
        return sid or self.lcu.sgp_server_id()

    def status(self, q):
        out = {"version": VERSION, "connected": False, "servers": list(self.sgp.servers)}
        try:
            auth = self.lcu._ensure()
            me = self.lcu.request("GET", "/lol-summoner/v1/current-summoner") or {}
            sid = self.lcu.sgp_server_id()
            out.update(
                connected=True,
                source=auth.source,
                region=auth.region,
                platform=auth.platform,
                server=sid,
                server_name=self.sgp.servers.get(sid, {}).get("name", sid),
                current={k: me.get(k) for k in ("gameName", "tagLine", "puuid", "summonerLevel", "profileIconId")},
            )
            for kind in ("entitlements", "league-session"):
                try:
                    self.tokens.get(kind)
                    out[f"token_{kind}"] = True
                except BridgeError:
                    out[f"token_{kind}"] = False
        except BridgeError as exc:
            out["error"] = str(exc)
        return out

    def servers(self, q):
        return [{"id": k, "name": v.get("name", k)} for k, v in self.sgp.servers.items()]

    def summoner(self, q):
        if q.get("puuid"):
            sid = self._server(q)
            if sid == self.lcu.sgp_server_id():
                return self.lcu.request("GET", f"/lol-summoner/v2/summoners/puuid/{q['puuid']}")
            data = self.sgp.request(sid, "/summoner-ledge/v1/regions/{sub}/summoners/puuids", "league-session", "POST", [q["puuid"]])
            if not data:
                raise BridgeError("找不到该召唤师", 404, "not_found")
            return data[0]
        name, tag = (q.get("name") or "").strip(), (q.get("tag") or "").strip()
        if not name:
            raise BridgeError("缺少 name 参数", 400, "bad_request")
        if tag:
            data = self.lcu.request("POST", "/lol-summoner/v1/summoners/aliases", [{"gameName": name, "tagLine": tag}])
            hit = data[0] if isinstance(data, list) and data else None
            if hit and hit.get("puuid"):
                return hit
        try:
            hit = self.lcu.request("GET", "/lol-summoner/v1/summoners?name=" + urllib.parse.quote(f"{name}#{tag}" if tag else name))
        except BridgeError as exc:
            if exc.status == 404:
                raise BridgeError(f"找不到召唤师 {name}#{tag}", 404, "not_found")
            raise
        if not hit or not hit.get("puuid"):
            raise BridgeError(f"找不到召唤师 {name}#{tag}", 404, "not_found")
        return hit

    def ranked(self, q):
        puuid = q.get("puuid") or ""
        sid = self._server(q)
        if sid == self.lcu.sgp_server_id():
            return {"source": "lcu", "data": self.lcu.request("GET", f"/lol-ranked/v1/ranked-stats/{puuid}")}
        # SGP leagues-ledge does not support cross-server queries (League Akari note)
        return {"source": "sgp", "data": self.sgp.request(sid, f"/leagues-ledge/v2/rankedStats/puuid/{puuid}", "league-session")}

    def matches(self, q):
        puuid = q.get("puuid") or ""
        sid = self._server(q)
        start = int(q.get("start") or 0)
        count = max(1, min(int(q.get("count") or 10), 50))
        try:
            data = self.sgp.request(
                sid,
                f"/match-history-query/v1/products/lol/player/{puuid}/SUMMARY",
                "entitlements",
                params={"startIndex": start, "count": count, "tag": q.get("tag")},
            )
            return {"source": "sgp", "server": sid, "data": data}
        except BridgeError as exc:
            if sid != self.lcu.sgp_server_id():
                raise
            log.warning("SGP match history failed (%s), falling back to LCU", exc)
            data = self.lcu.request(
                "GET", f"/lol-match-history/v1/products/lol/{puuid}/matches?begIndex={start}&endIndex={start + count - 1}"
            )
            return {"source": "lcu", "server": sid, "data": data}

    def game(self, q):
        gid = int(q.get("game_id") or 0)
        sid = self._server(q)
        return {"source": "sgp", "data": self.sgp.request(sid, f"/match-history-query/v1/products/lol/{{sub}}_{gid}/SUMMARY", "entitlements")}

    def raw_lcu(self, q):
        if not self.cfg.get("allow_raw_lcu"):
            raise BridgeError("raw LCU access is disabled (allow_raw_lcu=false)", 403, "forbidden")
        path = q.get("path") or ""
        if not path.startswith("/"):
            raise BridgeError("path must start with /", 400, "bad_request")
        return self.lcu.request("GET", path)


ROUTES = {
    "/v1/status": "status",
    "/v1/servers": "servers",
    "/v1/summoner": "summoner",
    "/v1/ranked": "ranked",
    "/v1/matches": "matches",
    "/v1/game": "game",
    "/v1/lcu": "raw_lcu",
}


def make_handler(bridge: Bridge, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"lolhelper-cn-bridge/{VERSION}"

        def log_message(self, fmt, *args):
            log.debug("%s - %s", self.address_string(), fmt % args)

        def _send(self, status: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if not token:
                return True
            got = self.headers.get("Authorization", "")
            if got.lower().startswith("bearer "):
                got = got[7:]
            else:
                got = self.headers.get("X-Bridge-Token", "")
            return hmac.compare_digest(got.strip(), token)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/health":
                return self._send(200, {"ok": True, "version": VERSION})
            if not self._authorized():
                return self._send(401, {"ok": False, "code": "unauthorized", "error": "token 错误"})
            name = ROUTES.get(parsed.path)
            if not name:
                return self._send(404, {"ok": False, "code": "not_found", "error": "unknown endpoint"})
            q = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            try:
                data = getattr(bridge, name)(q)
                self._send(200, {"ok": True, "data": data})
            except BridgeError as exc:
                self._send(exc.status, {"ok": False, "code": exc.code, "error": str(exc)})
            except Exception as exc:  # pragma: no cover - unexpected
                log.exception("handler error")
                self._send(500, {"ok": False, "code": "internal", "error": f"{type(exc).__name__}: {exc}"})

    return Handler


def load_config(path: Path) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    changed = not path.exists()
    if not cfg.get("token"):
        cfg["token"] = secrets.token_urlsafe(24)
        changed = True
    if changed:
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[bridge] 已生成配置文件 {path}")
    return cfg


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="联盟助手国服本地代理")
    ap.add_argument("--config", default=str(Path(__file__).with_name("bridge.json")))
    ap.add_argument("--host")
    ap.add_argument("--port", type=int)
    ap.add_argument("--check", action="store_true", help="只检测客户端并打印状态")
    args = ap.parse_args(argv)
    cfg = load_config(Path(args.config))
    if args.host:
        cfg["host"] = args.host
    if args.port:
        cfg["port"] = args.port
    logging.basicConfig(level=getattr(logging, str(cfg.get("log_level", "INFO")).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(message)s")
    bridge = Bridge(cfg)
    if args.check:
        print(json.dumps(bridge.status({}), ensure_ascii=False, indent=2))
        return 0
    server = ThreadingHTTPServer((cfg["host"], int(cfg["port"])), make_handler(bridge, cfg["token"]))
    print(f"[bridge] v{VERSION} 监听 http://{cfg['host']}:{cfg['port']}")
    print(f"[bridge] Token: {cfg['token']}  （填入插件配置 cn.bridge_token）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

