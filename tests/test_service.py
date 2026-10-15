import asyncio
import tempfile
import threading
import unittest
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pokebot.config import Config, ROOT
from pokebot.models import CST, Message
from pokebot.service import BotService, parse


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = ROOT / "runtime" / "tests"
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.now = datetime(2026, 10, 14, 15, 59, 59, tzinfo=timezone.utc)
        self.bot = BotService(ROOT / "assets", Path(self.temp.name),
                              Config(pool_min=25, pool_max=25, draw_cooldown_seconds=0,
                                     dex_cooldown_seconds=0, group_images_per_minute=100),
                              clock=lambda: self.now)

    async def asyncTearDown(self):
        await self.bot.close()
        self.temp.cleanup()

    def message(self, text="", **changes):
        return replace(Message("group", "member", uuid.uuid4().hex, self.now, text,
                               display_name="小智"), **changes)

    async def test_draw_then_repeat_has_image_and_one_capture(self):
        reply = await self.bot.handle(self.message())
        self.assertIn("皮卡丘", reply.text)
        self.assertIn("新捕捉", reply.text)
        self.assertEqual(reply.mention_member_id, "member")
        self.assertTrue(reply.image_path.is_file())
        self.assertIn("成就解锁：初次相遇", reply.text)
        repeat = await self.bot.handle(self.message())
        self.assertIn("图鉴无新增", repeat.text)
        self.assertNotIn("成就解锁", repeat.text)
        self.assertEqual(self.bot.store.snapshot("group", "member")[25]["count"], 10)

    async def test_beijing_midnight_independent_of_host_timezone(self):
        await self.bot.handle(self.message())
        self.now += timedelta(seconds=2)
        await self.bot.handle(self.message())
        entry = self.bot.store.snapshot("group", "member")[25]
        self.assertEqual((entry["first_day"], entry["last_day"], entry["count"]),
                         ("2026-10-14", "2026-10-15", 20))

    async def test_upgrade_bonus_reply_and_replay_preserve_original(self):
        from legacy_database import make_legacy
        from pokebot.store import Store
        path = Path(self.temp.name) / "legacy" / "collection.sqlite3"
        make_legacy(path, rows=[("group", "member", "2026-10-15", 25)])
        self.bot.store = Store(path)
        self.now += timedelta(seconds=2)
        with patch.object(self.bot.renderer, "render", wraps=self.bot.renderer.render) as render:
            first = await self.bot.handle(self.message("探索"))
            self.assertTrue(render.call_args.args[1]["upgrade_bonus"])
        self.assertIn("升级补抽十连", first.text)
        self.assertIn("已额外捕捉 10 只", first.text)
        self.assertIn("已捕捉 10 只，新捕捉 0 种", first.text)
        self.assertNotIn("图鉴无新增", first.text)
        self.assertTrue(first.image_path.is_file())
        again = await self.bot.handle(self.message("探索"))
        self.assertIn("升级补抽十连已领取", again.text)
        self.assertIn("捕捉次数不增加", again.text)
        self.assertNotIn("成就解锁", again.text)
        self.assertEqual(self.bot.store.snapshot("group", "member")[25]["count"], 11)

    async def test_replayed_message_suppressed_across_restart(self):
        message = self.message()
        await self.bot.handle(message)
        self.assertIsNone(await self.bot.handle(message))
        await self.bot.close()
        self.bot = BotService(ROOT / "assets", Path(self.temp.name), self.bot.config, clock=lambda: self.now)
        self.assertIsNone(await self.bot.handle(message))
        self.assertEqual(self.bot.store.snapshot("group", "member")[25]["count"], 10)

    async def test_concurrent_duplicate_event_produces_one_reply(self):
        message = self.message()
        replies = await asyncio.gather(*(self.bot.handle(message) for _ in range(10)))
        self.assertEqual(sum(r is not None for r in replies), 1)

    async def test_concurrent_distinct_messages_capture_once(self):
        await asyncio.gather(*(self.bot.handle(self.message()) for _ in range(8)))
        self.assertEqual(self.bot.store.snapshot("group", "member")[25]["count"], 10)

    async def test_unmentioned_private_expired_and_future_messages_ignored(self):
        messages = [self.message(mentioned=False), self.message(is_group=False),
                    self.message(timestamp=self.now - timedelta(minutes=6)),
                    self.message(timestamp=self.now + timedelta(minutes=2))]
        for message in messages:
            self.assertIsNone(await self.bot.handle(message))
        self.assertEqual(self.bot.store.snapshot("group", "member"), {})

    async def test_queries_never_capture_and_invalid_commands_explain(self):
        for text in ("图鉴", "图鉴 1", "图鉴 1 6", "图鉴 皮卡丘", "我的收集", "帮助",
                     "图鉴 2", "图鉴 1 7", "图鉴 1 abc", "图鉴 超梦梦", "我的收集 abc",
                     "成就", "我的成就", "成就 abc", "皮卡丘", "梦幻", "利欧路", "皮卡丘丘"):
            reply = await self.bot.handle(self.message(text))
            self.assertTrue(reply.text)
            self.assertNotIn("处理暂时失败", reply.text)
        self.assertEqual(self.bot.store.snapshot("group", "member"), {})

    async def test_reference_query_does_not_use_draw_or_collection_and_keeps_daily_draw(self):
        with patch.object(self.bot.store, "explore", wraps=self.bot.store.explore) as draw, \
                patch.object(self.bot.store, "snapshot", wraps=self.bot.store.snapshot) as snapshot:
            reply = await self.bot.handle(self.message("皮卡丘"))
            draw.assert_not_called()
            snapshot.assert_not_called()
        self.assertIn("#025 皮卡丘", reply.text)
        self.assertIn("属性：电", reply.text)
        self.assertIn("身高：0.4 米 · 体重：6.0 千克", reply.text)
        self.assertIn(self.bot.catalog.profiles[25]["description"], reply.text)
        self.assertTrue(reply.image_path.is_file())
        self.assertEqual(reply.buttons, ())
        with self.bot.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM explorations").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM achievements").fetchone()[0], 0)
        first_draw = await self.bot.handle(self.message())
        self.assertIn("新捕捉", first_draw.text)
        self.assertIn("成就解锁：初次相遇", first_draw.text)

    async def test_reference_query_preserves_collection_pages_and_personal_detail(self):
        await self.bot.handle(self.message())
        before = self.bot.store.snapshot("group", "member")
        achievements = self.bot.store.achievement_status("group", "member")
        personal_before = await self.bot.handle(self.message("图鉴 皮卡丘"))
        page_before = await self.bot.handle(self.message("图鉴 1 1"))
        reference = await self.bot.handle(self.message("梦幻"))
        self.assertIn("#151 梦幻", reference.text)
        self.assertNotIn("累计捕捉", reference.text)
        self.assertEqual(self.bot.store.snapshot("group", "member"), before)
        self.assertEqual(self.bot.store.achievement_status("group", "member"), achievements)
        self.assertEqual(await self.bot.handle(self.message("图鉴 皮卡丘")), personal_before)
        self.assertEqual(await self.bot.handle(self.message("图鉴 1 1")), page_before)

    async def test_all_1025_names_resolve_to_reference_and_have_descriptions(self):
        with patch.object(self.bot.renderer, "render", return_value=Path(self.temp.name) / "sample.png"), \
                patch.object(self.bot.limiter, "image", return_value=True):
            for p in self.bot.catalog.entries:
                self.assertEqual(parse(p["name"], self.bot.catalog), ("profile", p["id"]))
                for alias in p.get("aliases", []):
                    self.assertEqual(parse(alias, self.bot.catalog), ("profile", p["id"]))
                reply = await self.bot.handle(self.message(p["name"]))
                self.assertIn(self.bot.catalog.profiles[p["id"]]["description"], reply.text)
                self.assertNotIn("今日宝可梦是", reply.text)
        self.assertEqual(self.bot.store.snapshot("group", "member"), {})

    async def test_reference_rate_limit_uses_dex_interval(self):
        self.bot.limiter.config = replace(self.bot.config, dex_cooldown_seconds=15)
        await self.bot.handle(self.message("皮卡丘"))
        response = await self.bot.handle(self.message("梦幻"))
        self.assertIn("操作太快", response.text)
        first_draw = await self.bot.handle(self.message())
        self.assertIn("新捕捉", first_draw.text)

    async def test_reference_missing_data_or_image_is_clear_and_does_not_capture(self):
        with patch.dict(self.bot.catalog.profiles, clear=True):
            reply = await self.bot.handle(self.message("皮卡丘"))
            self.assertIn("更新资源.cmd", reply.text)
            self.assertIsNone(reply.image_path)
        with self.assertLogs("pokebot.service", level="ERROR"), \
                patch.object(self.bot.renderer, "render", side_effect=OSError("test")):
            reply = await self.bot.handle(self.message("皮卡丘"))
        self.assertIn(self.bot.catalog.profiles[25]["description"], reply.text)
        self.assertIsNone(reply.image_path)
        self.assertEqual(self.bot.store.snapshot("group", "member"), {})

    async def test_page_buttons_follow_all_boundaries_without_capturing(self):
        for page in range(1, 7):
            reply = await self.bot.handle(self.message(f"图鉴 1 {page}"))
            self.assertNotIn("下一页：", reply.text)
            self.assertNotIn("上一页：", reply.text)
            targets = ([page - 1] if page > 1 else []) + ([page + 1] if page < 6 else [])
            self.assertEqual([b.command for b in reply.buttons], [f"图鉴 1 {n}" for n in targets])
            for button, target in zip(reply.buttons, targets):
                response = await self.bot.handle(self.message("/" + button.command))
                self.assertIn(f"第 {target}/6 页", response.text)
        self.assertEqual(self.bot.store.snapshot("group", "member"), {})

    async def test_button_requests_keep_cooldown_and_render_failure_keeps_navigation(self):
        with self.assertLogs("pokebot.service", level="ERROR"), patch.object(self.bot.renderer, "render", side_effect=OSError("test")):
            reply = await self.bot.handle(self.message("图鉴 1 1"))
        self.assertTrue(reply.buttons)
        self.assertIsNone(reply.image_path)
        self.bot.limiter.config = replace(self.bot.config, dex_cooldown_seconds=15)
        response = await self.bot.handle(self.message(reply.buttons[0].command))
        self.assertIn("操作太快", response.text)
        self.assertEqual(self.bot.store.snapshot("group", "member"), {})

    async def test_achievement_query_reports_progress_without_capture(self):
        empty = await self.bot.handle(self.message("成就"))
        self.assertIn("已解锁 0/15 项", empty.text)
        self.assertIsNone(empty.image_path)
        await self.bot.handle(self.message())
        reply = await self.bot.handle(self.message("我的成就"))
        self.assertIn("已解锁 1/15 项", reply.text)
        self.assertIn("[已解锁] 初次相遇 · 2026-10-14", reply.text)
        self.assertIn("小有收藏 · 1/10", reply.text)
        other = await self.bot.handle(self.message("成就", member_id="other"))
        self.assertIn("已解锁 0/15 项", other.text)
        self.assertEqual(self.bot.store.snapshot("group", "member")[25]["count"], 10)

    async def test_multi_mention_still_captures_for_sender(self):
        await self.bot.handle(self.message("@小霞"))
        self.assertEqual(self.bot.store.snapshot("group", "小霞"), {})
        self.assertEqual(self.bot.store.snapshot("group", "member")[25]["count"], 10)

    async def test_command_with_other_mentions_takes_priority(self):
        reply = await self.bot.handle(self.message("@小霞 帮我看看 图鉴 1 2 @小刚"))
        self.assertIn("第 2/6 页", reply.text)
        self.assertEqual(self.bot.store.snapshot("group", "member"), {})

    async def test_missing_render_returns_text_and_retains_capture(self):
        with self.assertLogs("pokebot.service", level="ERROR"), \
                patch.object(self.bot.renderer, "render", side_effect=OSError("disk failure")):
            reply = await self.bot.handle(self.message())
        self.assertIn("图片暂时不可用", reply.text)
        self.assertIsNone(reply.image_path)
        self.assertEqual(self.bot.store.snapshot("group", "member")[25]["count"], 10)

    async def test_slow_image_does_not_block_other_message(self):
        started = threading.Event()
        release = threading.Event()
        original = self.bot.renderer.render

        def slow(*args):
            started.set()
            release.wait(5)
            return original(*args)

        with patch.object(self.bot.renderer, "render", side_effect=slow):
            task = asyncio.create_task(self.bot.handle(self.message("图鉴 1")))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                response = await asyncio.wait_for(self.bot.handle(self.message("帮助", member_id="other")), 1)
                self.assertIn("每日探索", response.text)
            finally:
                release.set()
                await task

    async def test_member_limit_and_notice_suppression(self):
        self.bot.limiter.config = replace(self.bot.config, draw_cooldown_seconds=3)
        await self.bot.handle(self.message())
        reply = await self.bot.handle(self.message())
        self.assertIn("操作太快", reply.text)
        self.assertIsNone(await self.bot.handle(self.message()))
        self.assertEqual(self.bot.store.snapshot("group", "member")[25]["count"], 10)

    async def test_group_image_limit_keeps_text_and_capture(self):
        self.bot.limiter.config = replace(self.bot.config, group_images_per_minute=1)
        await self.bot.handle(self.message())
        reply = await self.bot.handle(self.message(member_id="other"))
        self.assertIn("本次先返回文字", reply.text)
        self.assertIsNone(reply.image_path)
        self.assertEqual(self.bot.store.snapshot("group", "other")[25]["count"], 10)

    async def test_full_image_queue_returns_text(self):
        self.bot.pending = self.bot.config.render_queue_limit
        reply = await self.bot.handle(self.message())
        self.bot.pending = 0
        self.assertIn("图片生成繁忙", reply.text)
        self.assertEqual(self.bot.store.snapshot("group", "member")[25]["count"], 10)

    def test_parser_bounds_for_national_and_generations(self):
        for text in ("图鉴 10", "图鉴 0", "图鉴 1 0", "图鉴 1 7", "图鉴 1 -1", "图鉴 1 1 extra",
                     "图鉴 全国 36", "图鉴 9 5", "图鉴 00", "探索 2",
                     "图鉴 " + "9" * 5000, "图鉴 1 " + "9" * 5000):
            self.assertEqual(parse(text, self.bot.catalog)[0], "error")
        self.assertEqual(parse("  /图鉴   1   6  ", self.bot.catalog), ("dex", (1, 6)))
        self.assertEqual(parse("图鉴 全国 35", self.bot.catalog), ("dex", (0, 35)))
        self.assertEqual(parse("图鉴 9", self.bot.catalog), ("dex", (9, 1)))

    async def test_all_generation_and_national_pagination_keep_scope(self):
        from pokebot.dex import GENERATIONS, NATIONAL
        for generation in [*GENERATIONS.values(), NATIONAL]:
            label = "全国" if generation.number == 0 else str(generation.number)
            for page in {1, generation.pages}:
                reply = await self.bot.handle(self.message(f"图鉴 {label} {page}"))
                self.assertIn(f"第 {page}/{generation.pages} 页", reply.text)
                self.assertIn(f"已收集 0/{generation.count}", reply.text)
                for button in reply.buttons:
                    kind, target = parse(button.command, self.bot.catalog)
                    self.assertEqual(kind, "dex")
                    self.assertEqual(target[0], generation.number)
                    self.assertTrue(1 <= target[1] <= generation.pages)
        self.assertEqual(self.bot.store.snapshot("group", "member"), {})

    async def test_new_generation_collection_does_not_dilute_kanto_progress(self):
        self.bot.store.draw("group", "member", "2026-10-13", 906, 906)
        kanto = await self.bot.handle(self.message("图鉴 1 1"))
        self.assertIn("已收集 0/151", kanto.text)
        paldea = await self.bot.handle(self.message("图鉴 9 1"))
        self.assertIn("已收集 1/120", paldea.text)
        national = await self.bot.handle(self.message("图鉴 全国 1"))
        self.assertIn("已收集 1/1025", national.text)
        overview = await self.bot.handle(self.message("图鉴"))
        self.assertIn("全国图鉴：1/1025", overview.text)
        self.assertIn("第 1 世代 · 关都：0/151", overview.text)
        self.assertIn("第 9 世代 · 帕底亚：1/120", overview.text)

    async def test_explore_alias_uses_same_daily_batch_and_one_summary_image(self):
        first = await self.bot.handle(self.message("探索"))
        self.assertIn("已捕捉 10 只，新捕捉 1 种", first.text)
        self.assertEqual(first.text.count("#025 皮卡丘"), 10)
        self.assertTrue(first.image_path.name.startswith("exploration-"))
        again = await self.bot.handle(self.message())
        self.assertIn("图鉴无新增", again.text)
        self.assertEqual(self.bot.store.snapshot("group", "member")[25]["count"], 10)


if __name__ == "__main__":
    unittest.main()
