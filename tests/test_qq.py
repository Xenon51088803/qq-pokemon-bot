import base64
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp

from pokebot.config import ROOT
from pokebot.models import CST, Reply
from pokebot.qq import Gateway, deliver, normalize_event
from pokebot.qq_api import API_ORIGIN, QQAPI, QQAPIError
from pokebot.qq_settings import QQSettings
from pokebot.service import page_buttons


def event(**overrides):
    return {"op": 0, "s": 3, "t": "GROUP_AT_MESSAGE_CREATE", "d": {
        "group_openid": "group-A", "id": "message-1", "timestamp": datetime.now(CST).isoformat(),
        "author": {"member_openid": "member-1", "username": "小智"},
        "content": "<@!bot> <@other> 图鉴 1 2",
        "message_scene": {"ext": ["msg_idx=REF_123"]}, **overrides}}


class Response:
    def __init__(self, data, status=200, headers=None):
        self.data, self.status, self.headers = data, status, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self, **kwargs):
        return self.data


class Session:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


class SettingsAndEventsTests(unittest.TestCase):
    def test_secret_saved_locally_and_not_in_repr(self):
        folder = ROOT / "runtime" / "tests"
        folder.mkdir(parents=True, exist_ok=True)
        settings = QQSettings("123456789", "dummy-test-secret")
        self.assertNotIn(settings.app_secret, repr(settings))
        with tempfile.TemporaryDirectory(dir=folder) as directory:
            path = Path(directory) / "qq.local.json"
            settings.save(path)
            self.assertEqual(QQSettings.load(path).app_secret, settings.app_secret)
            self.assertFalse(path.with_suffix(".tmp").exists())

    def test_rejects_share_link_and_blank_secret(self):
        for app_id, secret in (("https://bot.q.qq.com/s/example", "test"), ("123", ""), ("123", "a\nb")):
            with self.assertRaises(ValueError):
                QQSettings(app_id, secret)

    def test_normalizes_author_and_mentions(self):
        msg = normalize_event(event())
        self.assertEqual((msg.group_id, msg.member_id, msg.text), ("group-A", "member-1", "图鉴 1 2"))
        self.assertTrue(msg.mentioned)

    def test_ignores_private_nonmention_bot_disallowed_and_malformed(self):
        for kind in ("C2C_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE", "READY"):
            self.assertIsNone(normalize_event({**event(), "t": kind}))
        self.assertIsNone(normalize_event(event(author={"member_openid": "bot", "bot": True})))
        self.assertIsNone(normalize_event(event(), ("another-group",)))
        self.assertIsNone(normalize_event(event(timestamp="invalid")))
        self.assertIsNone(normalize_event(event(author={"id": "not-member-openid"})))


class APITests(unittest.IsolatedAsyncioTestCase):
    def api(self, responses):
        return QQAPI(Session(responses), QQSettings("123456789", "dummy-test-secret"))

    async def test_token_cache_and_fixed_origin(self):
        api = self.api([Response({"access_token": "fake-token", "expires_in": 7200}),
                        Response({"url": "wss://api.bot.qq.com/websocket/"})])
        self.assertEqual(await api.token(), "fake-token")
        self.assertEqual(await api.token(), "fake-token")
        self.assertEqual(await api.gateway(), "wss://api.bot.qq.com/websocket/")
        calls = api.session.calls
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][1], API_ORIGIN + "/app/getAppAccessToken")
        self.assertFalse(calls[0][2]["allow_redirects"])
        self.assertNotIn("Authorization", calls[0][2]["headers"])
        self.assertEqual(calls[1][2]["headers"]["Authorization"], "QQBot fake-token")

    async def test_http_200_business_error_not_echoed(self):
        api = self.api([Response({"code": 100016, "message": "never echo dummy-secret"})])
        with self.assertRaises(QQAPIError) as context:
            await api.token()
        self.assertNotIn("dummy-secret", str(context.exception))
        self.assertIn("AppSecret", str(context.exception))

    async def test_expired_token_refreshes(self):
        api = self.api([Response({"access_token": "old", "expires_in": 7200}),
                        Response({"access_token": "new", "expires_in": 7200})])
        await api.token()
        api._refresh_at = 0
        self.assertEqual(await api.token(), "new")

    async def test_rate_limit_retries_same_sequence(self):
        api = self.api([Response({"access_token": "t", "expires_in": 7200}),
                        Response({"code": 40034100}, 429), Response({"id": "sent"})])
        body = {"msg_id": "m", "msg_seq": 2, "msg_type": 0, "content": "test"}
        with patch("pokebot.qq_api.asyncio.sleep", new=AsyncMock()):
            await api.send("g", body)
        self.assertEqual(api.session.calls[1][2]["json"], api.session.calls[2][2]["json"])

    async def test_platform_dedup_is_success(self):
        api = self.api([Response({"access_token": "t", "expires_in": 7200}),
                        Response({"code": 40054005}, 400)])
        self.assertEqual(await api.send("g", {"msg_id": "m", "msg_seq": 1}), {"duplicate": True})

    async def test_gateway_rejects_nonofficial_host(self):
        api = self.api([Response({"access_token": "t", "expires_in": 7200}),
                        Response({"url": "wss://qq.com.attacker.example/"})])
        with self.assertRaises(RuntimeError):
            await api.gateway()

    async def test_upload_base64_and_cache_group_isolation(self):
        folder = ROOT / "runtime" / "tests"
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=folder) as directory:
            path = Path(directory) / "image.png"
            path.write_bytes(b"test-image")
            api = self.api([Response({"access_token": "t", "expires_in": 7200}),
                            Response({"file_info": "f1", "ttl": 300}), Response({"file_info": "f2", "ttl": 300})])
            self.assertEqual(await api.upload("g1", path), "f1")
            self.assertEqual(await api.upload("g1", path), "f1")
            self.assertEqual(await api.upload("g2", path), "f2")
            body = api.session.calls[1][2]["json"]
            self.assertFalse(body["srv_send_msg"])
            self.assertEqual(base64.b64decode(body["file_data"]), b"test-image")
            self.assertEqual(len(api.session.calls), 3)

    async def test_delivery_passive_reference_and_sequences(self):
        api = SimpleNamespace(send=AsyncMock(), upload=AsyncMock(return_value="media"))
        raw = event()
        msg = normalize_event(raw)
        await deliver(api, msg, Reply("group-A", "member-1", "结果", Path("sample.png")), raw)
        bodies = [call.args[1] for call in api.send.call_args_list]
        self.assertEqual([b["msg_seq"] for b in bodies], [1, 2])
        self.assertTrue(all(b["msg_id"] == msg.message_id for b in bodies))
        self.assertEqual(bodies[0]["message_reference"]["message_id"], "REF_123")
        self.assertIn("<@member-1>", bodies[0]["content"])

    async def test_upload_failure_text_fallback(self):
        api = SimpleNamespace(send=AsyncMock(), upload=AsyncMock(side_effect=QQAPIError(400, 850031)))
        raw = event()
        with self.assertLogs("pokebot.qq", level="WARNING"), patch("builtins.print"):
            await deliver(api, normalize_event(raw), Reply("group-A", "member-1", "结果", Path("sample.png")), raw)
        self.assertEqual(api.send.call_args_list[-1].args[1]["msg_seq"], 3)
        self.assertIn("收藏记录不受影响", api.send.call_args_list[-1].args[1]["content"])

    async def test_navigation_is_command_keyboard_bound_to_requester(self):
        api = SimpleNamespace(send=AsyncMock(), upload=AsyncMock(return_value="media"))
        raw = event()
        reply = Reply("group-A", "member-1", "第一世代图鉴 · 第 2/6 页\n已收集 2/151（1.3%）", Path("sample.png"), page_buttons(2))
        await deliver(api, normalize_event(raw), reply, raw)
        bodies = [c.args[1] for c in api.send.call_args_list]
        self.assertEqual([b["msg_seq"] for b in bodies], [1, 2])
        self.assertEqual([b["msg_type"] for b in bodies], [2, 7])
        self.assertTrue(all(b["msg_id"] == "message-1" for b in bodies))
        self.assertEqual(bodies[0]["message_reference"], {"message_id": "REF_123"})
        self.assertEqual(bodies[0]["markdown"]["content"], "<@member-1>\n" + reply.text)
        self.assertNotIn("下一页：", bodies[0]["markdown"]["content"])
        self.assertNotIn("上一页：", bodies[0]["markdown"]["content"])
        buttons = bodies[0]["keyboard"]["content"]["rows"][0]["buttons"]
        self.assertEqual([b["action"]["data"] for b in buttons], ["/图鉴 1 1", "/图鉴 1 3"])
        for button in buttons:
            action = button["action"]
            self.assertEqual(action["type"], 2)
            self.assertEqual(action["permission"], {"type": 0, "specify_user_ids": ["member-1"]})
            self.assertFalse(action["enter"])

    async def test_navigation_rejection_falls_back_with_bounded_sequences(self):
        for image_fails in (False, True):
            for has_image in (False, True):
                async def send(group, body):
                    if body["msg_type"] == 2:
                        raise QQAPIError(403, 40034127)
                upload = AsyncMock(side_effect=QQAPIError(400, 850031)) if image_fails else AsyncMock(return_value="media")
                api = SimpleNamespace(send=AsyncMock(side_effect=send), upload=upload)
                raw = event()
                reply = Reply("group-A", "member-1", "末页", Path("sample.png") if has_image else None, page_buttons(6))
                with self.assertLogs("pokebot.qq", level="WARNING"), patch("builtins.print"):
                    await deliver(api, normalize_event(raw), reply, raw)
                bodies = [c.args[1] for c in api.send.call_args_list]
                seqs = [b["msg_seq"] for b in bodies]
                self.assertEqual(seqs, sorted(set(seqs)))
                self.assertLessEqual(max(seqs), 4)
                self.assertEqual(seqs[-1], 4 if has_image and image_fails else 3 if has_image else 2)
                self.assertEqual(bodies[0]["msg_type"], 2)
                self.assertEqual(bodies[1]["msg_type"], 0)
                self.assertIn("<@member-1>\n末页\n上一页：@机器人 图鉴 1 5", bodies[1]["content"])
                self.assertNotIn("下一页", bodies[1]["content"])
                self.assertTrue(all(b["msg_id"] == "message-1" for b in bodies))
                if has_image and not image_fails:
                    self.assertEqual(bodies[2]["msg_type"], 7)

    async def test_navigation_network_failure_does_not_create_extra_reply(self):
        api = SimpleNamespace(send=AsyncMock(side_effect=QQAPIError(503, 0)))
        raw = event()
        with self.assertRaises(QQAPIError):
            await deliver(api, normalize_event(raw), Reply("group-A", "member-1", "结果", buttons=page_buttons(1)), raw)
        self.assertEqual(api.send.await_count, 1)

    async def test_navigation_without_image_is_one_message_at_page_boundaries(self):
        for page, label, command in ((1, "下一页", "/图鉴 1 2"), (6, "上一页", "/图鉴 1 5")):
            api = SimpleNamespace(send=AsyncMock(), upload=AsyncMock())
            raw = event()
            await deliver(api, normalize_event(raw), Reply("group-A", "member-1", "图鉴", buttons=page_buttons(page)), raw)
            self.assertEqual(api.send.await_count, 1)
            api.upload.assert_not_awaited()
            body = api.send.call_args.args[1]
            self.assertEqual(body["msg_seq"], 1)
            buttons = body["keyboard"]["content"]["rows"][0]["buttons"]
            self.assertEqual(len(buttons), 1)
            self.assertEqual(buttons[0]["render_data"]["label"], label)
            self.assertEqual(buttons[0]["action"]["data"], command)

    async def test_navigation_image_failure_keeps_first_message_buttons(self):
        api = SimpleNamespace(send=AsyncMock(), upload=AsyncMock(side_effect=OSError("test")))
        raw = event()
        reply = Reply("group-A", "member-1", "图鉴", Path("sample.png"), page_buttons(1))
        with self.assertLogs("pokebot.qq", level="WARNING"):
            await deliver(api, normalize_event(raw), reply, raw)
        bodies = [c.args[1] for c in api.send.call_args_list]
        self.assertEqual([b["msg_seq"] for b in bodies], [1, 3])
        self.assertIn("keyboard", bodies[0])
        self.assertIn("图片暂时发送失败", bodies[1]["content"])


class WebSocket:
    def __init__(self, events, close_code=1000):
        self.events, self.close_code, self.sent = list(events), close_code, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def receive_json(self):
        return {"op": 10, "d": {"heartbeat_interval": 45000}}

    async def send_json(self, data):
        self.sent.append(data)

    async def close(self):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.events:
            raise StopAsyncIteration
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(self.events.pop(0)))


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def gateway(self, ws):
        api = SimpleNamespace(gateway=AsyncMock(return_value="wss://api.bot.qq.com/websocket/"),
                              token=AsyncMock(return_value="fake"),
                              session=SimpleNamespace(ws_connect=lambda *args, **kwargs: ws))
        return Gateway(api, status=lambda *args: None)

    async def test_check_requires_ready_and_sends_no_group_messages(self):
        ws = WebSocket([{"op": 0, "t": "READY", "s": 1, "d": {"session_id": "session"}}])
        gateway = self.gateway(ws)
        self.assertTrue(await gateway.connect(check_only=True))
        self.assertEqual(ws.sent[0]["op"], 2)
        self.assertEqual(ws.sent[0]["d"]["intents"], 1 << 25)
        self.assertTrue(gateway.queue.empty())

    async def test_invalid_session_check_fails(self):
        with self.assertRaises(PermissionError):
            await self.gateway(WebSocket([{"op": 9, "d": False}])).connect(check_only=True)

    async def test_resume_and_only_group_at_queued(self):
        ws = WebSocket([{"op": 0, "t": "RESUMED", "s": 2, "d": {}}, event(),
                        {"op": 0, "t": "C2C_MESSAGE_CREATE", "s": 4, "d": {}}])
        gateway = self.gateway(ws)
        gateway.session_id, gateway.seq = "old-session", 1
        await gateway.connect()
        self.assertEqual(ws.sent[0]["op"], 6)
        self.assertEqual(gateway.queue.qsize(), 1)
        self.assertEqual(gateway.seq, 4)

    async def test_permission_close_is_fatal(self):
        with self.assertRaises(PermissionError):
            await self.gateway(WebSocket([], close_code=4014)).connect()


if __name__ == "__main__":
    unittest.main()
