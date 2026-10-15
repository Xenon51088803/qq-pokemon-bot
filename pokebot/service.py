import asyncio
import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from .achievements import ACHIEVEMENTS, BY_ID
from .catalog import Catalog
from .config import Config
from .dex import GENERATIONS, TOTAL_SPECIES, scope
from .maintenance import Backups
from .models import CST, CommandButton, Message, Reply
from .render import Renderer
from .store import Store

LOG = logging.getLogger(__name__)
HELP = """全国图鉴 1025 种 · 每日探索（北京时间零点换日）
@机器人 或 @机器人 探索 → 一次获得 10 只，自动捕捉
当天整批重复时，替换其中一只为未拥有的（当前抽池集齐后停用）。
@机器人 皮卡丘 → 图片、属性和介绍（独立资料查询）
@机器人 图鉴 → 全国与各世代完成度总览
@机器人 图鉴 1 [页码] → 第一世代图鉴，第 1～6 页
@机器人 图鉴 9 1 → 第九世代第 1 页
@机器人 图鉴 全国 [页码] → 全国图鉴，第 1～35 页
@机器人 图鉴 皮卡丘 → 单只资料与本人捕捉记录
@机器人 我的收集 → 种类、次数和最近捕捉
@机器人 成就 → 已解锁成就、解锁日期和收集进度
@机器人 帮助 → 本说明
图鉴页支持翻页按钮，群聊点击后需发送填入的命令。
每群独立；默认形态；允许重复；同日再次探索不增加次数。"""


def parse(text: str, catalog: Catalog):
    # The QQ adapter will remove structured mention elements. The local simulator
    # also accepts simple @name tokens for multi-mention scenarios.
    parts = [part for part in text.strip().split() if not part.startswith("@")]
    for index, part in enumerate(parts):
        if part.removeprefix("/") in ("帮助", "我的收集", "图鉴", "成就", "我的成就", "探索"):
            parts = parts[index:]
            break
    if not parts:
        return "draw", None
    # Match whole command tokens; never execute user text as code.
    command = parts[0].removeprefix("/")
    if command == "探索":
        return ("draw", None) if len(parts) == 1 else ("error", "用法：@机器人 探索")
    if command in ("帮助", "我的收集", "成就", "我的成就"):
        if len(parts) != 1:
            return "error", f"用法：@机器人 {command}"
        return {"帮助": "help", "我的收集": "stats", "成就": "achievements", "我的成就": "achievements"}[command], None
    if command == "图鉴":
        if len(parts) == 1:
            return "overview", None
        if parts[1] == "全国" or (parts[1].isascii() and parts[1].isdecimal()):
            # Length bounds avoid unbounded integer conversion on untrusted input.
            generation = 0 if parts[1] == "全国" else int(parts[1]) if len(parts[1]) <= 2 else -1
            if parts[1] != "全国" and generation not in GENERATIONS:
                return "error", "世代须为 1～9 或“全国”，用法：@机器人 图鉴 9 1"
            selected = scope(generation)
            if len(parts) > 3 or (len(parts) == 3 and
                                  (not parts[2].isascii() or not parts[2].isdecimal() or len(parts[2]) > 2)):
                return "error", f"页码须为 1～{selected.pages}，用法：@机器人 图鉴 {parts[1]} 1"
            page = int(parts[2]) if len(parts) == 3 else 1
            if not 1 <= page <= selected.pages:
                return "error", f"有效页码为 1～{selected.pages}。"
            return "dex", (generation, page)
        pokemon = catalog.lookup(" ".join(parts[1:]))
        if pokemon:
            return "detail", pokemon["id"]
        return "error", "未找到全国图鉴宝可梦。请使用中文名，例如：@机器人 图鉴 皮卡丘"
    pokemon = catalog.lookup(" ".join(parts).removeprefix("/"))
    if pokemon:
        return "profile", pokemon["id"]
    return "error", "未找到宝可梦或指令。资料查询示例：@机器人 皮卡丘；@机器人 探索 可每日获得 10 只。"


def page_buttons(page: int, generation: int = 1) -> tuple[CommandButton, ...]:
    buttons = []
    label = "全国" if generation == 0 else str(generation)
    if page > 1:
        buttons.append(CommandButton("上一页", f"图鉴 {label} {page - 1}"))
    if page < scope(generation).pages:
        buttons.append(CommandButton("下一页", f"图鉴 {label} {page + 1}"))
    return tuple(buttons)


class Limiter:
    def __init__(self, config):
        self.config = config
        self.last = {}
        self.notices = {}
        self.images = {}
        self.next_cleanup = 0

    def allow(self, group, member, kind):
        now = time.monotonic()
        if now >= self.next_cleanup:
            self.last = {k: v for k, v in self.last.items() if now - v < 86401}
            self.notices = {k: v for k, v in self.notices.items() if now - v < 60}
            self.images = {k: v for k, v in self.images.items() if v and now - v[-1] < 60}
            self.next_cleanup = now + 60
        category = "dex" if kind in ("dex", "detail", "overview", "profile") else "draw"
        key = (group, member, category)
        wait = self.config.dex_cooldown_seconds if category == "dex" else self.config.draw_cooldown_seconds
        remaining = wait - (now - self.last.get(key, -1e20))
        if remaining > 0:
            notice_key = (group, member)
            if now - self.notices.get(notice_key, -1e20) >= 15:
                self.notices[notice_key] = now
                return False, f"操作太快，请约 {int(remaining) + 1} 秒后再试。"
            return False, None
        self.last[key] = now
        return True, None

    def image(self, group):
        now = time.monotonic()
        recent = self.images.setdefault(group, deque())
        while recent and now - recent[0] >= 60:
            recent.popleft()
        if len(recent) >= self.config.group_images_per_minute:
            return False
        recent.append(now)
        return True


class BotService:
    def __init__(self, assets: Path, runtime: Path, config: Config | None = None, clock=None):
        self.config = config or Config()
        self.clock = clock or (lambda: datetime.now(CST))
        self.catalog = Catalog(assets)
        self.store = Store(runtime / "collection.sqlite3")
        self.renderer = Renderer(self.catalog, runtime / "images", self.config)
        self.backups = Backups(self.store, runtime / "backups")
        self.limiter = Limiter(self.config)
        self.executor = ThreadPoolExecutor(max_workers=self.config.render_workers, thread_name_prefix="dex-image")
        self.pending = 0
        self.inflight = set()
        self.maintenance_task = None
        self.next_maintenance = 0

    async def maintain(self):
        now = self.clock()
        try:
            await asyncio.to_thread(self.backups.maintain, now)
            await asyncio.to_thread(self.renderer.cleanup)
            await asyncio.to_thread(self.store.clean_events, now.timestamp() - 86400)
        except Exception:
            LOG.exception("Maintenance failed; collection is preserved")

    async def close(self):
        if self.maintenance_task:
            await self.maintenance_task
        await asyncio.to_thread(self.executor.shutdown, wait=True)

    async def handle(self, message: Message) -> Reply | None:
        if not message.is_group or not message.mentioned:
            return None
        if not all((message.group_id, message.member_id, message.message_id)):
            raise ValueError("群、成员和消息标识不能为空")
        if message.timestamp.tzinfo is None:
            raise ValueError("消息时间必须携带时区")
        now = self.clock()
        age = (now - message.timestamp).total_seconds()
        if age > 300 or age < -30:
            return None
        if len(message.text) > 2000:
            return None
        key = (message.group_id, message.message_id)
        if key in self.inflight:
            return None
        self.inflight.add(key)
        try:
            if await asyncio.to_thread(self.store.seen, *key):
                return None
            if time.monotonic() >= self.next_maintenance and (
                    self.maintenance_task is None or self.maintenance_task.done()):
                self.next_maintenance = time.monotonic() + 60
                self.maintenance_task = asyncio.create_task(self.maintain())
            kind, arg = parse(message.text, self.catalog)
            allowed, notice = self.limiter.allow(message.group_id, message.member_id, kind)
            if not allowed:
                reply = Reply(message.group_id, message.member_id, notice) if notice else None
            else:
                reply = await self.execute(message, kind, arg)
            await asyncio.to_thread(self.store.mark_seen, *key, now.timestamp())
            return reply
        except Exception:
            LOG.exception("Message handling failed")
            return Reply(message.group_id, message.member_id,
                         "处理暂时失败，请稍后重新发送。若抽取已保存，再试仍是同一只，不会多计次数。")
        finally:
            self.inflight.discard(key)

    async def execute(self, msg, kind, arg):
        buttons = page_buttons(arg[1], arg[0]) if kind == "dex" else ()
        def reply(text, image=None):
            return Reply(msg.group_id, msg.member_id, text, image, buttons)
        if kind == "help":
            return reply(HELP)
        if kind == "error":
            return reply(arg)
        if kind == "achievements":
            statuses = await asyncio.to_thread(self.store.achievement_status, msg.group_id, msg.member_id)
            unlocked_count = sum(s["unlocked_day"] is not None for s in statuses)
            lines = [f"收集成就：已解锁 {unlocked_count}/{len(ACHIEVEMENTS)} 项"]
            for status in statuses:
                if status["unlocked_day"]:
                    lines.append(f"[已解锁] {status['title']} · {status['unlocked_day']}\n  {status['description']}")
                else:
                    lines.append(f"[未解锁] {status['title']} · {status['progress']}/{status['target']}\n  {status['description']}")
            lines.append("成就按本群收藏计算，不增加额外抽取次数。")
            return reply("\n".join(lines))
        if kind == "profile":
            p = self.catalog.by_id[arg]
            profile = self.catalog.profiles.get(arg)
            if profile is None:
                return reply("资料图鉴尚未准备完成，请让运行者双击“更新资源.cmd”后重启机器人。")
            text = (f"#{arg:03} {p['name']} · 第 {p['generation']} 世代\n"
                    f"分类：{profile['category']}\n属性：{' / '.join(profile['types'])}\n"
                    f"身高：{profile['height_m']:.1f} 米 · 体重：{profile['weight_kg']:.1f} 千克\n"
                    f"图鉴介绍：{profile['description']}\n资料查询不会增加收藏或消耗每日探索。")
            # Reference pages are shared resources, independent of trainer collection state.
            payload = {"pokemon_id": arg, **profile}
        else:
            name = (msg.display_name or msg.member_id)[:40]
            payload = {"name": name, "group": msg.group_id, "member": msg.member_id}
        if kind == "draw":
            day = msg.timestamp.astimezone(CST).date().isoformat()
            result = await asyncio.to_thread(self.store.explore, msg.group_id, msg.member_id, day,
                                             self.config.pool_min, self.config.pool_max)
            if result.repeated_today:
                state = "今天已经探索过，结果不变，图鉴无新增，捕捉次数不增加。"
            else:
                state = f"已捕捉 {len(result.items)} 只，新捕捉 {sum(item.is_new for item in result.items)} 种。"
            lines = [f"#{item.pokemon_id:03} {self.catalog.by_id[item.pokemon_id]['name']}"
                     + (" [新发现]" if item.is_new and not result.repeated_today else "") for item in result.items]
            text = f"今日探索 · {day}（北京时间）\n{state}\n" + "\n".join(lines)
            if result.upgrade_bonus:
                text += ("\n升级补抽十连：原单抽记录保留，已额外捕捉 10 只，每人每群仅限一次。"
                         if not result.repeated_today else "\n升级补抽十连已领取，本次展示已保存的补抽结果。")
            if result.legacy:
                text += "\n这是升级前保存的单只结果，原记录保留。"
            elif result.guaranteed:
                text += "\n本次已启用未收集保底。" if not result.repeated_today else "\n当次探索已启用未收集保底。"
            unlocked = [BY_ID[achievement_id].title for achievement_id in result.unlocked_achievements]
            if unlocked:
                text += "\n成就解锁：" + "、".join(unlocked) + "\n@机器人 成就 查看详情。"
            kind = "exploration"
            payload.update(day=day, repeated=result.repeated_today, guaranteed=result.guaranteed,
                           legacy=result.legacy, upgrade_bonus=result.upgrade_bonus,
                           items=[{"id": item.pokemon_id, "count": item.count,
                               "is_new": item.is_new and not result.repeated_today} for item in result.items])
            if unlocked:
                payload["achievements"] = unlocked
        elif kind != "profile":
            caught = await asyncio.to_thread(self.store.snapshot, msg.group_id, msg.member_id)
            count = len(caught)
            total = sum(p["count"] for p in caught.values())
            if kind == "stats":
                recent = sorted(caught.values(), key=lambda p: (p["last_day"], p["pokemon_id"]), reverse=True)[:5]
                lines = [f"#{p['pokemon_id']:03} {self.catalog.by_id[p['pokemon_id']]['name']}（{p['last_day']}，共 {p['count']} 次）" for p in recent]
                return reply(f"全国已收集：{count}/{TOTAL_SPECIES} 种\n完成度：{count / TOTAL_SPECIES:.1%}\n累计捕捉：{total} 次\n最近捕捉：\n" +
                             ("\n".join(lines) if lines else "还没有记录，@机器人 开始第一次抽取吧。"))
            if kind == "overview":
                progress = [{"generation": g.number, "name": g.name, "count": sum(i in caught for i in g.ids()),
                             "total": g.count} for g in GENERATIONS.values()]
                text = f"全国图鉴：{count}/{TOTAL_SPECIES}，完成度 {count / TOTAL_SPECIES:.1%}\n累计捕捉 {total} 次。\n"
                text += "\n".join(f"第 {g['generation']} 世代 · {g['name']}：{g['count']}/{g['total']}" for g in progress)
                text += "\n按世代查看：@机器人 图鉴 1 1\n全国分页：@机器人 图鉴 全国 1"
                payload.update(count=count, total=total, progress=progress)
            elif kind == "dex":
                generation, page = arg
                selected = scope(generation)
                scope_count = sum(i in caught for i in selected.ids())
                title = "第一世代图鉴" if generation == 1 else "全国图鉴" if generation == 0 else f"第 {generation} 世代 · {selected.name}图鉴"
                text = f"{title} · 第 {page}/{selected.pages} 页\n已收集 {scope_count}/{selected.count}（{scope_count / selected.count:.1%}）"
                payload.update(page=page, generation=generation, collection={str(k): v for k, v in caught.items()})
            else:
                p = self.catalog.by_id[arg]
                owned = caught.get(arg)
                lines = ([f"累计捕捉 {owned['count']} 次", f"首次捕捉：{owned['first_day']}", f"最近捕捉：{owned['last_day']}"]
                         if owned else ["尚未捕捉到这只宝可梦", "查询资料不会增加捕捉记录"])
                text = f"#{arg:03} {p['name']} · 第 {p['generation']} 世代\n" + "\n".join(lines)
                payload.update(pokemon_id=arg, lines=lines)
        if self.pending >= self.config.render_queue_limit:
            return reply(text + "\n图片生成繁忙，稍后重新查询即可。")
        if not self.limiter.image(msg.group_id):
            return reply(text + "\n本群图片请求较多，本次先返回文字，请一分钟后再查看图片。")
        self.pending += 1
        try:
            image = await asyncio.get_running_loop().run_in_executor(self.executor, self.renderer.render, kind, payload)
            return reply(text, image)
        except Exception:
            LOG.exception("Image rendering failed")
            return reply(text + "\n图片暂时不可用，捕捉记录不受影响。")
        finally:
            self.pending -= 1
