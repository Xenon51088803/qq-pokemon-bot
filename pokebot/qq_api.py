"""QQ official HTTPS API. Secrets are only sent to the fixed official origin."""
import asyncio
import base64
import hashlib
import time
from collections import OrderedDict
from urllib.parse import quote, urlsplit

import aiohttp

API_ORIGIN = "https://api.bot.qq.com"

ERROR_HINTS = {
    100007: "AppID 无效或机器人状态异常，请核对配置和后台状态",
    100016: "AppID 或 AppSecret 不正确，请重新运行凭证配置",
    10004: "机器人不存在，请核对 AppID",
    11281: "请求来源 IP 不在白名单，请核对后台服务器 IP 白名单",
    40034101: "机器人不在该群，请先把机器人添加进群",
    40054003: "机器人不在该群，请先把机器人添加进群",
    40054002: "机器人被禁言，请由群主解除禁言",
    40034105: "缺少群消息发送权限，请检查群内机器人的消息权限",
    40034005: "原消息超过回复有效期，请在群里重新 @ 机器人",
    40034128: "回复时间或次数超限，请在群里重新 @ 机器人",
    850018: "群或机器人被禁言，请检查群内发言权限",
    850031: "图片超过平台大小限制",
}


class QQAPIError(RuntimeError):
    def __init__(self, status: int, code: int = 0):
        self.status, self.code = status, code
        hint = ERROR_HINTS.get(code, "请检查网络、机器人开发设置或群消息权限")
        super().__init__(f"QQ 接口失败（HTTP {status}，代码 {code}）：{hint}")


class QQAPI:
    def __init__(self, session, settings):
        self.session, self.settings = session, settings
        self._token = None
        self._refresh_at = 0
        self._token_lock = asyncio.Lock()
        self.media = OrderedDict()

    async def _request(self, method, path, *, body=None, authenticated=True):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("QQ API 路径必须相对于固定官方域名")
        for attempt in range(4):
            headers = {"User-Agent": "PokemonCollectionBot/0.2"}
            if authenticated:
                headers["Authorization"] = "QQBot " + await self.token()
                headers["X-Union-Appid"] = self.settings.app_id
            try:
                async with self.session.request(method, API_ORIGIN + path, json=body, headers=headers,
                                                allow_redirects=False) as response:
                    status = response.status
                    try:
                        data = await response.json(content_type=None)
                    except (ValueError, UnicodeError):
                        data = {}
                    if not isinstance(data, dict):
                        data = {}
                    try:
                        code = int(data.get("code", 0))
                    except (ValueError, TypeError):
                        code = -1
                    # Stable msg_id + msg_seq makes a retry safe after an uncertain send.
                    if path.endswith("/messages") and code == 40054005:
                        return {"duplicate": True}
                    if 200 <= status < 300 and code == 0:
                        return data
                    if status == 401 and authenticated and attempt < 3:
                        self._refresh_at = 0
                        continue
                    retry = status == 429 or status >= 500 or code in (100001, 40034100, 50055001, 850027)
                    if not retry or attempt == 3:
                        raise QQAPIError(status, code)
                    try:
                        delay = min(20, max(1, float(response.headers.get("Retry-After", 2 ** attempt))))
                    except (ValueError, TypeError):
                        delay = 2 ** attempt
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt == 3:
                    # Do not echo HTTP bodies, headers, or request details containing credentials.
                    raise QQAPIError(0) from None
                delay = 2 ** attempt
            await asyncio.sleep(delay)
        raise QQAPIError(0)

    async def token(self):
        async with self._token_lock:
            if self._token and time.monotonic() < self._refresh_at:
                return self._token
            data = await self._request("POST", "/app/getAppAccessToken", authenticated=False,
                                       body={"appId": self.settings.app_id, "clientSecret": self.settings.app_secret})
            if not isinstance(data.get("access_token"), str) or not data["access_token"]:
                raise QQAPIError(200, -1)
            try:
                lifetime = float(data["expires_in"])
                if not 0 < lifetime <= 86400:
                    raise ValueError()
            except (KeyError, ValueError, TypeError):
                raise QQAPIError(200, -1) from None
            self._token = data["access_token"]
            self._refresh_at = time.monotonic() + lifetime - min(45, lifetime / 2)
            return self._token

    async def gateway(self):
        data = await self._request("GET", "/gateway")
        url = data.get("url", "")
        parts = urlsplit(url)
        if parts.scheme != "wss" or not parts.hostname or not parts.hostname.endswith(".qq.com") or parts.username:
            raise RuntimeError("QQ 网关返回了非官方 WSS 地址，已停止连接")
        return url

    async def upload(self, group, path):
        data = await asyncio.to_thread(path.read_bytes)
        if len(data) > 4 * 1024 * 1024:
            raise ValueError("本地图片超过 4 MB，暂不发送")
        key = (group, hashlib.sha256(data).hexdigest())
        old = self.media.get(key)
        if old and old[1] > time.monotonic():
            self.media.move_to_end(key)
            return old[0]
        response = await self._request("POST", f"/v2/groups/{quote(group, safe='')}/files", body={
            "file_type": 1, "file_name": path.name,
            "file_data": base64.b64encode(data).decode("ascii"), "srv_send_msg": False,
        })
        info = response.get("file_info")
        if not isinstance(info, str) or not info:
            raise QQAPIError(200, -1)
        ttl = float(response.get("ttl", 300))
        expires = time.monotonic() + (86400 if ttl == 0 else max(0, ttl - 10))
        self.media[key] = (info, expires)
        self.media.move_to_end(key)
        while len(self.media) > 128:
            self.media.popitem(last=False)
        return info

    async def send(self, group, body):
        return await self._request("POST", f"/v2/groups/{quote(group, safe='')}/messages", body=body)
