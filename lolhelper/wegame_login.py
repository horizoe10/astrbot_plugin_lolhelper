"""WeGame QR-code login (QQ / WeChat).

Mirrors the wegame.com.cn web login: QQ ptlogin QR code (appid 1600001063, daid 733)
-> check_sig redirect gives p_uin / p_skey -> /api/middle/clientapi/auth/login_by_qq
exchanges them for the tgp_id / tgp_ticket cookies used by the WeGame battle API.
WeChat: open.weixin.qq.com qrconnect (appid wx911818d5d92affa8) -> long-poll gives wx_code
-> /api/middle/clientapi/auth/login_by_wechat.
Undocumented endpoints: may change without notice.
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp
from yarl import URL

from .log import default_logger
from .errors import BridgeError

PT_APPID = "1600001063"
PT_DAID = "733"
S_URL = "https://www.wegame.com.cn/login/callback.html?t=qq&l=zh_CN&c=0&a=0"
AUTH_API = "https://www.wegame.com.cn/api/middle/clientapi/auth/"
WX_APPID = "wx911818d5d92affa8"
WX_REDIRECT = "https://www.wegame.com.cn/login/callback.html?t=wx&l=zh_CN&c=0&a=0"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")
PT_REFERER = "https://xui.ptlogin2.qq.com/"

WAITING, SCANNED, EXPIRED, DONE, FAILED = "waiting", "scanned", "expired", "done", "failed"


def hash33(text: str) -> int:
    h = 0
    for ch in text:
        h += (h << 5) + ord(ch)
    return h & 0x7FFFFFFF


def parse_ptui(text: str) -> list[str]:
    """ptuiCB('0','0','url','0','登录成功！', 'nick') -> its string arguments."""
    m = re.search(r"ptuiCB\((.*)\)", text or "", re.S)
    return re.findall(r"'((?:[^'\\]|\\.)*)'", m.group(1)) if m else []


def ticket_expiry(resp: aiohttp.ClientResponse) -> float | None:
    """Expiry (unix time) WeGame sets on the tgp_ticket cookie, if any."""
    m = resp.cookies.get("tgp_ticket")
    if m is None:
        return None
    try:
        if m["max-age"]:
            return time.time() + int(m["max-age"])
        if m["expires"]:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(m["expires"]).timestamp()
    except (ValueError, TypeError):
        pass
    return None


def _cookies(resp: aiohttp.ClientResponse, into: dict[str, str]) -> None:
    for key, morsel in resp.cookies.items():
        if morsel.value:
            into[key] = morsel.value


class WeGameQrLogin:
    """One QR login attempt. Use: png = await start(); poll() until DONE; cookie = await finish()."""

    def __init__(self, timeout: float = 15, logger: Any = None):
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.logger = logger or default_logger
        self.session: aiohttp.ClientSession | None = None
        self.nick = ""
        self.info: dict = {}
        self._redirect = ""
        self._login_sig = ""
        self._qrsig = ""
        self._started = 0.0

    async def __aenter__(self) -> "WeGameQrLogin":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    def _jar_value(self, name: str) -> str:
        for c in self.session.cookie_jar:
            if c.key == name and c.value:
                return c.value
        return ""

    async def start(self) -> bytes:
        """Open a ptlogin session and return the QR code PNG."""
        await self.close()
        self.session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(), headers={"User-Agent": UA},
                                             timeout=self.timeout)
        q = {"appid": PT_APPID, "daid": PT_DAID, "style": 20, "s_url": S_URL, "target": "self",
             "hide_close_icon": 1, "hide_border": 1, "pt_no_auth": 0}
        try:
            async with self.session.get("https://xui.ptlogin2.qq.com/cgi-bin/xlogin?" + urlencode(q)) as resp:
                await resp.read()
            self._login_sig = self._jar_value("pt_login_sig")
            q = {"appid": PT_APPID, "e": 2, "l": "M", "s": 3, "d": 72, "v": 4, "t": random.random(),
                 "daid": PT_DAID, "pt_3rd_aid": 0}
            async with self.session.get("https://ssl.ptlogin2.qq.com/ptqrshow?" + urlencode(q),
                                        headers={"Referer": PT_REFERER}) as resp:
                png = await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise BridgeError(f"获取登录二维码失败：{type(exc).__name__}") from exc
        self._qrsig = self._jar_value("qrsig")
        if not self._qrsig or not png.startswith(b"\x89PNG"):
            raise BridgeError("获取登录二维码失败：QQ 登录服务没有返回二维码")
        self._started = time.time()
        return png

    async def poll(self) -> tuple[str, str]:
        """Check the QR state once: (WAITING|SCANNED|EXPIRED|DONE|FAILED, message)."""
        q = {"u1": S_URL, "ptqrtoken": hash33(self._qrsig), "ptredirect": 0, "h": 1, "t": 1, "g": 1,
             "from_ui": 1, "ptlang": 2052, "action": f"0-0-{int(time.time() * 1000)}", "js_ver": 25100115,
             "js_type": 1, "login_sig": self._login_sig, "pt_uistyle": 40, "aid": PT_APPID, "daid": PT_DAID,
             "o1vId": "", "pt_js_version": "v1.0"}
        try:
            async with self.session.get("https://ssl.ptlogin2.qq.com/ptqrlogin?" + urlencode(q),
                                        headers={"Referer": PT_REFERER}) as resp:
                args = parse_ptui(await resp.text())
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return WAITING, f"网络波动：{type(exc).__name__}"
        code = args[0] if args else ""
        msg = args[4] if len(args) > 4 else ""
        if code == "0":
            self._redirect = args[2] if len(args) > 2 else ""
            self.nick = args[5] if len(args) > 5 else ""
            return DONE, msg
        if code == "66":
            return WAITING, msg
        if code == "67":
            return SCANNED, msg
        if code == "65":
            return EXPIRED, msg or "二维码已失效"
        return FAILED, msg or f"QQ 登录返回 {code or '未知结果'}"

    async def finish(self) -> str:
        """After DONE: follow check_sig, exchange p_skey for WeGame tgp_id / tgp_ticket."""
        if not self._redirect:
            raise BridgeError("扫码登录未完成")
        got: dict[str, str] = {}
        url = self._redirect
        try:
            for _ in range(6):  # check_sig -> ... -> wegame callback page
                async with self.session.get(url, allow_redirects=False) as resp:
                    _cookies(resp, got)
                    nxt = resp.headers.get("Location")
                if not nxt or "wegame.com.cn/login/callback" in nxt:
                    break
                url = str(resp.url.join(URL(nxt)))
            for c in self.session.cookie_jar:  # cookies the jar accepted along the way
                if c.key in ("p_uin", "p_skey", "uin") and c.value and not got.get(c.key):
                    got[c.key] = c.value
            uin = re.sub(r"^o0*", "", got.get("p_uin") or got.get("uin") or "")
            skey = got.get("p_skey") or ""
            if not uin or not skey:
                raise BridgeError("QQ 登录成功，但没有拿到 WeGame 登录态（p_skey）")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise BridgeError(f"换取 WeGame 凭证失败：{type(exc).__name__}") from exc
        return await exchange("login_by_qq", {"qq_info_type": 6, "uin": uin, "sig": skey}, self.timeout, self.logger,
                              cookie=f"p_uin={got.get('p_uin', '')}; p_skey={skey}", info=self.info)


async def exchange(method: str, login_info: dict, timeout: aiohttp.ClientTimeout, logger: Any,
                   cookie: str = "", referer: str = S_URL, info: dict | None = None) -> str:
    """Call WeGame login_by_qq / login_by_wechat and return "tgp_id=…; tgp_ticket=…"."""
    body = {"clienttype": "1000005", "mappid": "10001", "mcode": "", "config_params": {"lang_type": 0},
            "login_info": login_info}
    headers = {"Referer": referer, "Origin": "https://www.wegame.com.cn"}
    if cookie:
        headers["Cookie"] = cookie
    got: dict[str, str] = {}
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), headers={"User-Agent": UA},
                                         timeout=timeout) as s:
            async with s.post(AUTH_API + method, json=body, headers=headers) as resp:
                _cookies(resp, got)
                if info is not None:
                    info["expires_at"] = ticket_expiry(resp)
                data = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        raise BridgeError(f"换取 WeGame 凭证失败：{type(exc).__name__}") from exc
    inner = (data or {}).get("data") or {}
    if (data or {}).get("code") not in (0, None) or inner.get("result") not in (0, None):
        msg = inner.get("errmsg") or (data or {}).get("msg") or "未知错误"
        raise BridgeError(f"WeGame 登录失败：{msg}")
    tgp_id = got.get("tgp_id") or str(inner.get("tgp_id") or inner.get("tgpid") or "")
    ticket = got.get("tgp_ticket") or str(inner.get("tgp_ticket") or inner.get("ticket") or "")
    if not tgp_id or not ticket:
        (logger or default_logger).warning(f"[lolhelper] WeGame 登录返回缺少凭证字段：{sorted(inner)}")
        raise BridgeError("WeGame 登录成功，但没有返回 tgp_id / tgp_ticket")
    return f"tgp_id={tgp_id}; tgp_ticket={ticket}"


class WeChatQrLogin:
    """WeChat variant with the same interface as WeGameQrLogin."""

    def __init__(self, timeout: float = 15, logger: Any = None):
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.logger = logger or default_logger
        self.session: aiohttp.ClientSession | None = None
        self.nick = ""
        self.info: dict = {}
        self._uuid = ""
        self._last = ""
        self._code = ""

    async def __aenter__(self) -> "WeChatQrLogin":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def start(self) -> bytes:
        await self.close()
        self.session = aiohttp.ClientSession(headers={"User-Agent": UA}, timeout=self.timeout)
        q = {"appid": WX_APPID, "scope": "snsapi_login", "redirect_uri": WX_REDIRECT, "login_type": "jssdk",
             "self_redirect": "true", "state": format(random.getrandbits(32), "x"), "style": "black"}
        try:
            async with self.session.get("https://open.weixin.qq.com/connect/qrconnect?" + urlencode(q)) as resp:
                html = await resp.text()
            m = re.search(r"/connect/qrcode/([A-Za-z0-9_-]+)", html)
            if not m:
                raise BridgeError("获取微信登录二维码失败：微信开放平台没有返回二维码")
            self._uuid = m.group(1)
            async with self.session.get(f"https://open.weixin.qq.com/connect/qrcode/{self._uuid}") as resp:
                img = await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise BridgeError(f"获取微信登录二维码失败：{type(exc).__name__}") from exc
        if len(img) < 200:
            raise BridgeError("获取微信登录二维码失败：二维码图片为空")
        return img

    async def poll(self) -> tuple[str, str]:
        """Long-poll (about 25 s per call) the WeChat QR state."""
        url = f"https://lp.open.weixin.qq.com/connect/l/qrconnect?uuid={self._uuid}"
        if self._last:
            url += f"&last={self._last}"
        url += f"&_={int(time.time() * 1000)}"
        try:
            async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=40)) as resp:
                text = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            return WAITING, f"网络波动：{type(exc).__name__}"
        m = re.search(r"wx_errcode=(\d+)", text)
        code = m.group(1) if m else ""
        if code == "405":
            c = re.search(r"wx_code='([^']*)'", text)
            self._code = c.group(1) if c else ""
            return (DONE, "") if self._code else (FAILED, "微信没有返回登录凭据")
        if code == "404":
            self._last = "404"
            return SCANNED, "已扫码"
        if code == "408":
            return WAITING, ""
        if code == "403":
            return FAILED, "已在手机上取消登录"
        if code in ("402", "500"):
            return EXPIRED, "二维码已失效"
        return FAILED, f"微信登录返回 {code or '未知结果'}"

    async def finish(self) -> str:
        if not self._code:
            raise BridgeError("扫码登录未完成")
        return await exchange("login_by_wechat", {"wx_info_type": 1, "appid": WX_APPID, "code": self._code},
                              self.timeout, self.logger, referer=WX_REDIRECT, info=self.info)

