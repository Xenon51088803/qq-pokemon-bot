"""Official QQ gateway adapter. Runs only when explicitly started by the user."""
import argparse
import asyncio
import json
import logging
import random
import re
from contextlib import suppress
from datetime import datetime
from logging.handlers import RotatingFileHandler

import aiohttp

from .config import Config, ROOT
from .maintenance import InstanceLock
from .models import CST, Message
from .qq_api import QQAPI, QQAPIError
from .qq_settings import QQSettings
from .service import BotService

LOG = logging.getLogger(__name__)
RUNTIME = ROOT / "runtime" / "qq"
FATAL_CLOSE = {4001, 4002, 4010, 4011, 4012, 4013, 4014, 4914, 4915}
KEYBOARD_REJECTIONS = {305007, 40034029, 40034127, 40034124, 40034011, 304036}


def normalize_event(event: dict, allowed_groups=()) -> Message | None:
    if event.get("t") != "GROUP_AT_MESSAGE_CREATE":
        return None
    data = event.get("d")
    if not isinstance(data, dict):
        return None
    author = data.get("author") or {}
    if not isinstance(author, dict) or author.get("bot"):
        return None
    group, member, msg_id = data.get("group_openid"), author.get("member_openid"), data.get("id")
    if any(not isinstance(v, str) or not v for v in (group, member, msg_id)):
        return None
    if allowed_groups and group not in allowed_groups:
        return None
    try:
        timestamp = datetime.fromisoformat(data.get("timestamp", "").replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None
    if timestamp.tzinfo is None:
        return None
    content = data.get("content", "")
    if not isinstance(content, str):
        return None
    # Platform content usually removes the bot prefix. Remove remaining encoded mentions.
    content = re.sub(r"<@!?[^>\s]+>|<qqbot-at-user\b[^>]*?/?>", " ", content).strip()
    display = author.get("username") or "训练家"
    if not isinstance(display, str):
        display = "训练家"
    return Message(group, member, msg_id, timestamp, content, display_name=display[:40])


def reply_context(raw):
    scene = raw.get("d", {}).get("message_scene") or {}
    if isinstance(scene, dict):
        for item in scene.get("ext", ()) or ():
            if isinstance(item, str) and item.startswith("msg_idx="):
                value = item.split("=", 1)[1]
                if value:
                    return {"message_reference": {"message_id": value}}
    return {}


def navigation(reply):
    buttons = [{
        "id": f"dex-page-{button.command.split()[-1]}",
        "render_data": {"label": button.label, "visited_label": button.label, "style": 1},
        "action": {
            "type": 2,
            "permission": {"type": 0, "specify_user_ids": [reply.mention_member_id]},
            "data": "/" + button.command, "enter": False, "reply": False,
            "unsupport_tips": "请手动 @ 机器人发送图鉴命令",
        },
    } for button in reply.buttons]
    return {"content": {"rows": [{"buttons": buttons}]}}


async def deliver(api, msg, reply, raw):
    # Always respond to the original message; do not consume proactive-message quota.
    if (datetime.now(CST) - msg.timestamp).total_seconds() >= 295:
        return
    base = {"msg_id": msg.message_id, **reply_context(raw)}
    content = f"<@{reply.mention_member_id}>\n{reply.text}"
    seq = 1
    if reply.buttons:
        try:
            await api.send(msg.group_id, {**base, "msg_type": 2, "msg_seq": seq,
                                        "markdown": {"content": content}, "keyboard": navigation(reply)})
        except QQAPIError as exc:
            if exc.code not in KEYBOARD_REJECTIONS:
                raise
            LOG.warning("翻页按钮被平台拒绝，使用文字命令：code=%s", exc.code)
            if (datetime.now(CST) - msg.timestamp).total_seconds() >= 295:
                return
            fallback = content + "\n" + "\n".join(
                f"{b.label}：@机器人 {b.command}" for b in reply.buttons)
            seq += 1
            await api.send(msg.group_id, {**base, "msg_type": 0, "msg_seq": seq, "content": fallback})
    else:
        await api.send(msg.group_id, {**base, "msg_type": 0, "msg_seq": seq, "content": content})
    if reply.image_path:
        image_seq = seq + 1
        try:
            media = await api.upload(msg.group_id, reply.image_path)
            if (datetime.now(CST) - msg.timestamp).total_seconds() >= 295:
                return
            await api.send(msg.group_id, {**base, "msg_type": 7, "msg_seq": image_seq,
                                        "media": {"file_info": media}})
        except (QQAPIError, ValueError, OSError) as exc:
            LOG.warning("图片发送未完成：%s", type(exc).__name__)
            if isinstance(exc, QQAPIError):
                print(f"图片发送未完成：{exc}", flush=True)
            if (datetime.now(CST) - msg.timestamp).total_seconds() >= 295:
                return
            await api.send(msg.group_id, {**base, "msg_type": 0, "msg_seq": image_seq + 1,
                                        "content": "图片暂时发送失败，收藏记录不受影响。请稍后重新 @ 我查询。"})


class Gateway:
    def __init__(self, api, service=None, status=print):
        self.api, self.service, self.status = api, service, status
        self.session_id = None
        self.seq = None
        self.queue = asyncio.Queue(maxsize=128)
        self.ready = False

    async def worker(self):
        while True:
            event = await self.queue.get()
            try:
                msg = normalize_event(event, self.api.settings.allowed_groups)
                if msg:
                    self.status("收到群内 @，正在处理……")
                    async with asyncio.timeout(240):
                        reply = await self.service.handle(msg)
                        if reply:
                            await deliver(self.api, msg, reply, event)
                            self.status("群消息处理完成。")
            except QQAPIError as exc:
                self.status(str(exc))
                LOG.warning("Reply failed: HTTP=%s code=%s", exc.status, exc.code)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Do not log event bodies, credentials, user content or HTTP payloads.
                self.status(f"消息处理未完成（{type(exc).__name__}），请稍后重新 @。")
                LOG.warning("Reply failed: %s", type(exc).__name__)
            finally:
                self.queue.task_done()

    async def refresh(self):
        while True:
            await asyncio.sleep(30)
            try:
                await self.api.token()
            except QQAPIError as exc:
                LOG.warning("Token refresh failed: HTTP=%s code=%s", exc.status, exc.code)

    async def connect(self, check_only=False):
        url = await self.api.gateway()
        token = await self.api.token()
        async with self.api.session.ws_connect(url, max_msg_size=1024 * 1024,
                                                timeout=aiohttp.ClientWSTimeout(ws_receive=75, ws_close=5)) as ws:
            hello = await asyncio.wait_for(ws.receive_json(), 20)
            if hello.get("op") != 10:
                raise RuntimeError("QQ 网关未返回 Hello")
            interval = float(hello.get("d", {}).get("heartbeat_interval", 45000)) / 1000
            if not 1 <= interval <= 120:
                raise RuntimeError("QQ 网关心跳间隔无效")
            if self.session_id and self.seq is not None and not check_only:
                await ws.send_json({"op": 6, "d": {"token": "QQBot " + token,
                                                    "session_id": self.session_id, "seq": self.seq}})
            else:
                await ws.send_json({"op": 2, "d": {"token": "QQBot " + token,
                                                    "intents": 1 << 25, "shard": [0, 1],
                                                    "properties": {"$os": "windows", "$browser": "pokebot", "$device": "pokebot"}}})
            acknowledged = True

            async def heartbeat():
                nonlocal acknowledged
                while True:
                    await asyncio.sleep(interval)
                    if not acknowledged:
                        await ws.close()
                        return
                    acknowledged = False
                    await ws.send_json({"op": 1, "d": self.seq})

            heartbeat_task = asyncio.create_task(heartbeat())
            try:
                async for incoming in ws:
                    if incoming.type != aiohttp.WSMsgType.TEXT:
                        if incoming.type == aiohttp.WSMsgType.ERROR:
                            raise RuntimeError("QQ WebSocket 网络异常")
                        continue
                    event = json.loads(incoming.data)
                    op = event.get("op")
                    if op == 11:
                        acknowledged = True
                    elif op == 1:
                        await ws.send_json({"op": 1, "d": self.seq})
                    elif op == 7:
                        if check_only:
                            raise RuntimeError("QQ 请求重新连接，请再次运行检查")
                        return
                    elif op == 9:
                        self.session_id, self.seq = None, None
                        if check_only:
                            raise PermissionError("QQ 拒绝了登录会话，请核对凭证及 WebSocket 设置")
                        return
                    elif op == 0:
                        if event.get("t") == "READY":
                            self.session_id = event.get("d", {}).get("session_id")
                            self.ready = True
                            self.status("QQ 连接成功（READY）。可在测试群里 @ 机器人。")
                            if check_only:
                                return True
                        elif event.get("t") == "RESUMED":
                            self.ready = True
                            self.status("QQ 连接已恢复。")
                        elif not check_only and event.get("t") == "GROUP_AT_MESSAGE_CREATE":
                            # Advance sequence only after the event enters the bounded queue.
                            await asyncio.wait_for(self.queue.put(event), 10)
                        if isinstance(event.get("s"), int):
                            self.seq = event["s"]
                code = ws.close_code
                if code in FATAL_CLOSE:
                    raise PermissionError(f"QQ 网关拒绝连接（代码 {code}）。请检查 AppID、机器人状态及事件权限；连接方式须为 WebSocket。")
                if code in (4006, 4007):
                    self.session_id, self.seq = None, None
            finally:
                heartbeat_task.cancel()
                with suppress(asyncio.CancelledError, aiohttp.ClientError, RuntimeError):
                    await heartbeat_task
        if check_only:
            raise RuntimeError("QQ 在确认登录前断开连接，请检查 WebSocket 设置和权限")

    async def run(self):
        workers = [asyncio.create_task(self.worker()) for _ in range(4)]
        refresher = asyncio.create_task(self.refresh())
        delay = 2
        try:
            while True:
                self.ready = False
                try:
                    await self.connect()
                except PermissionError:
                    raise
                except QQAPIError as exc:
                    if exc.status in (401, 403) or exc.code in (100007, 100016, 10004):
                        raise
                    self.status(str(exc))
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, RuntimeError) as exc:
                    self.status(f"连接中断（{type(exc).__name__}），准备重连。")
                if self.ready:
                    delay = 2
                self.status(f"约 {delay} 秒后自动重连；请保持电脑联网。")
                await asyncio.sleep(delay + random.random())
                delay = min(60, delay * 2)
        finally:
            refresher.cancel()
            # Finish work already accepted by this process, bounded by per-message timeout.
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.queue.join(), 15)
            for task in workers:
                task.cancel()
            await asyncio.gather(refresher, *workers, return_exceptions=True)


async def run(check_only=False):
    settings = QQSettings.load()
    timeout = aiohttp.ClientTimeout(total=25, connect=10)
    async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
        api = QQAPI(session, settings)
        if check_only:
            async with asyncio.timeout(60):
                if await Gateway(api).connect(check_only=True) is not True:
                    raise RuntimeError("QQ 尚未确认登录成功")
            print("凭证和 WebSocket 检查通过。检查程序现在退出，不会持续在线，也未发送群消息。")
            print("下一步：双击 启动QQ机器人.cmd 并保持窗口打开。")
        else:
            service = BotService(ROOT / "assets", RUNTIME, Config.load())
            try:
                await service.maintain()
                print("正在连接 QQ 官方网关……按 Ctrl+C 可停止。密钥不会显示在日志中。")
                print("真实群数据单独保存在 runtime/qq；本程序只响应群内 @。")
                await Gateway(api, service).run()
            finally:
                await service.close()


def main():
    parser = argparse.ArgumentParser(description="全国图鉴宝可梦机器人 · QQ 官方接入")
    parser.add_argument("--check", action="store_true", help="只验证凭证和 WebSocket，不发群消息，检查后退出")
    args = parser.parse_args()
    try:
        with InstanceLock(RUNTIME):
            logdir = RUNTIME / "logs"
            logdir.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(logdir / "qq.log", maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            logging.getLogger().addHandler(handler)
            logging.getLogger().setLevel(logging.INFO)
            asyncio.run(run(args.check))
        return 0
    except KeyboardInterrupt:
        print("\nQQ 机器人已停止，已捕捉记录保留。")
        return 0
    except (ValueError, PermissionError, QQAPIError) as exc:
        print(f"未能连接：{exc}")
        return 1
    except Exception as exc:
        print(f"连接失败（{type(exc).__name__}）。请检查网络、WebSocket 设置和本机系统时间。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
