import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp

from pokebot.catalog import Catalog
from pokebot.config import ROOT
from pokebot.qq_api import QQAPI, QQAPIError
from pokebot.qq_panel import (COMMANDS, configure_panel, diagnose_panel, list_group_panels,
                             main, panel_config, response_shape)
from pokebot.qq_settings import QQSettings
from pokebot.service import parse
from test_qq import Response, Session


def record(**changes):
    return {"panel_id": "test-panel", "scope": "group", "target_type": "all",
            "version": 2, "panel": panel_config(), **changes}


def page(records, **changes):
    return {"records": records, "next_cursor": "", "is_end": True, **changes}


class PanelTests(unittest.IsolatedAsyncioTestCase):
    def api(self, *responses):
        return SimpleNamespace(_request=AsyncMock(side_effect=responses))

    async def test_diagnosis_only_queries_and_redacts_values_and_unknown_keys(self):
        api = self.api({"data": {"records": [{"panel_id": "private-panel-id",
                        "remark": "private-text", "group_openids": ["private-group-id"],
                        "private-dynamic-key": "private-secret", "scope": "group",
                        "target_type": "all"}], "is_end": True, "next_cursor": ""}})
        report = await diagnose_panel(api)
        self.assertNotIn("private-", report)
        self.assertIn('"data"', report)
        self.assertIn('"scope": "group"', report)
        self.assertIn('"is_end": true', report)
        api._request.assert_awaited_once_with("GET", "/v2/panels?scope=group&limit=50")

    async def test_create_verify_then_rerun_without_write(self):
        api = self.api(page([]), {"panel_id": "test-panel"}, page([record()]), page([record()]))
        self.assertIn("回读确认", await configure_panel(api))
        self.assertIn("无需修改", await configure_panel(api))
        writes = [c for c in api._request.call_args_list if c.args[0] != "GET"]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0].args, ("POST", "/v2/panels"))
        self.assertEqual(writes[0].kwargs["body"]["scope"], "group")
        self.assertFalse(writes[0].kwargs["retry"])

    async def test_update_owned_panel_preserves_scope_and_uses_version(self):
        old = panel_config()
        old["items"] = old["items"][:1]
        api = self.api(page([record(panel=old)]), {"version": 3}, page([record()]))
        await configure_panel(api)
        call = api._request.call_args_list[1]
        self.assertEqual(call.args, ("PUT", "/v2/panels/test-panel"))
        self.assertEqual(set(call.kwargs["body"]), {"panel"})
        self.assertEqual(call.kwargs["body"]["panel"]["version"], 2)

    async def test_foreign_global_or_duplicate_owned_panel_is_preserved(self):
        for records in ([record(panel={"remark": "another-tool"})], [record(), record()]):
            api = self.api(page(records))
            with self.assertRaisesRegex(RuntimeError, "保留原配置"):
                await configure_panel(api)
            self.assertEqual(api._request.call_count, 1)

    async def test_pagination_finds_existing_panel_and_defaults(self):
        existing = record()
        for item in existing["panel"]["items"]:
            del item["only_admin"]
        api = self.api(page([], next_cursor="cursor&+", is_end=False), page([existing]))
        self.assertIn("无需修改", await configure_panel(api))
        self.assertIn("cursor=cursor%26%2B", api._request.call_args.args[1])

    async def test_incomplete_or_malformed_list_never_creates(self):
        for response in ({}, page([{}]), page([], next_cursor=None, is_end=False)):
            api = self.api(response)
            with self.assertRaises(RuntimeError):
                await configure_panel(api)
            self.assertEqual(api._request.call_count, 1)
        api = self.api(page([], next_cursor="same", is_end=False),
                       page([], next_cursor="same", is_end=False))
        with self.assertRaises(RuntimeError):
            await list_group_panels(api)

    async def test_readback_must_confirm_configuration(self):
        api = self.api(page([]), {"panel_id": "test-panel"}, page([]))
        with self.assertRaisesRegex(RuntimeError, "未确认生效"):
            await configure_panel(api)

    async def test_specific_panels_are_not_modified(self):
        specific = record(target_type="specific", panel_id="test-specific")
        api = self.api(page([specific]), {"panel_id": "test-panel"}, page([specific, record()]))
        await configure_panel(api)
        self.assertEqual(api._request.call_args_list[1].args[0], "POST")

    async def test_mutation_failure_is_not_retried_by_http_client(self):
        for status in (401, 429, 500):
            session = Session([Response({"access_token": "fake-token", "expires_in": 7200}),
                               Response({"code": 100001}, status)])
            api = QQAPI(session, QQSettings("123456789", "dummy-test-secret"))
            with patch("pokebot.qq_api.asyncio.sleep", new=AsyncMock()) as sleep:
                with self.assertRaises(QQAPIError):
                    await api._request("POST", "/v2/panels", body={}, retry=False)
                sleep.assert_not_awaited()
            self.assertEqual(len(session.calls), 2)
        session = Session([Response({"access_token": "fake-token", "expires_in": 7200})])
        api = QQAPI(session, QQSettings("123456789", "dummy-test-secret"))
        await api.token()
        with patch.object(session, "request", side_effect=aiohttp.ClientError()) as request:
            with self.assertRaises(QQAPIError):
                await api._request("POST", "/v2/panels", body={}, retry=False)
            self.assertEqual(request.call_count, 1)


class PanelCommandTests(unittest.TestCase):
    def test_diagnostic_cli_does_not_claim_configuration_success(self):
        with patch("pokebot.qq_panel.apply_panel", new=AsyncMock(return_value="脱敏结构")) as run, \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["--diagnose"]), 0)
        run.assert_awaited_once_with(diagnose=True)
        self.assertIn("仅查询面板", output.getvalue())
        self.assertNotIn("请在 QQ 群输入框", output.getvalue())

    def test_diagnostic_distinguishes_missing_null_and_empty_records(self):
        self.assertEqual(response_shape({}), {})
        self.assertEqual(response_shape({"records": None}), {"records": None})
        self.assertEqual(response_shape({"records": []}), {"records": {"长度": 0, "前两项结构": []}})

    def test_preview_never_loads_credentials_or_connects(self):
        with patch("pokebot.qq_panel.QQSettings.load") as load, \
                patch("pokebot.qq_panel.aiohttp.ClientSession") as session, \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main([]), 0)
        load.assert_not_called()
        session.assert_not_called()
        self.assertIn("探索", output.getvalue())

    def test_all_menu_commands_route_to_expected_existing_features(self):
        catalog = Catalog(ROOT / "assets")
        expected = ["draw", "stats", "overview", "dex", "dex", "achievements", "profile", "help"]
        for (name, description), kind in zip(COMMANDS, expected, strict=True):
            with self.subTest(command=name):
                self.assertLessEqual(len(name.encode("utf-16-le")) // 2, 14)
                self.assertLessEqual(len(description.encode("utf-16-le")) // 2, 30)
                self.assertEqual(parse(name, catalog)[0], kind)
                self.assertEqual(parse("/" + name, catalog)[0], kind)
