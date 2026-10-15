import argparse
import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import uuid

from .config import Config, ROOT
from .maintenance import Backups, InstanceLock, restore_backup
from .models import CST, Message
from .service import BotService
from .store import Store
from datetime import datetime

RUNTIME = ROOT / "runtime" / "simulator"


def setup_logging():
    logdir = RUNTIME / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(logdir / "bot.log", maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(logging.INFO)


async def simulate(args):
    service = BotService(ROOT / "assets", RUNTIME, Config.load())
    group, member = args.group, args.member
    last_buttons = ()
    try:
        await service.maintain()

        async def send(line, is_group=True):
            nonlocal last_buttons
            mentioned = "@机器人" in line
            text = line.replace("@机器人", " ").strip()
            reply = await service.handle(Message(group, member, uuid.uuid4().hex, datetime.now(CST), text,
                                                 mentioned=mentioned, is_group=is_group, display_name=member))
            if reply:
                # Keep the displayed page available when a rapid click is rate limited.
                if reply.buttons:
                    last_buttons = reply.buttons
                print(f"\n[机器人 → @{reply.mention_member_id}]\n{reply.text}", flush=True)
                if reply.image_path:
                    print(f"图片：{reply.image_path.resolve()}\n", flush=True)
                if reply.buttons:
                    print("翻页按钮：" + "  ".join(f"[{i}] {b.label}" for i, b in enumerate(reply.buttons, 1)), flush=True)
                    print("输入 /按钮 编号 操作，例如 /按钮 1。", flush=True)
            else:
                print("（未回复：未 @、私聊、重复消息或连续触发限流）", flush=True)

        if args.once is not None:
            await send(args.once)
            return
        print("\n宝可梦每日探索 · 本地模拟器\n全国 1025 种 / 每日探索 10 只 / 每群独立 / 北京时间零点换日")
        print("输入 @机器人 开始；输入 @机器人 帮助 查看玩法。")
        print("模拟器操作：/群 名称  /成员 名称  /按钮 编号  /私聊 消息  /备份  /状态  /退出")
        print("生成的图片路径会显示在回复下方，可用文件管理器打开。")
        print(f"数据目录：{RUNTIME}\n")
        while True:
            try:
                line = await asyncio.to_thread(input, f"[{group} / {member}] > ")
            except EOFError:
                break
            line = line.strip()
            if line in ("/退出", "/exit"):
                break
            if not line:
                continue
            if line.startswith("/群 ") or line.startswith("/成员 "):
                command, value = line.split(maxsplit=1)
                if not value or len(value) > 60:
                    print("名称须为 1～60 个字符。")
                elif command == "/群":
                    group = value
                    last_buttons = ()
                else:
                    member = value
                    last_buttons = ()
                continue
            if line == "/按钮" or line.startswith("/按钮 "):
                value = line.removeprefix("/按钮").strip()
                if value not in tuple(str(i) for i in range(1, len(last_buttons) + 1)):
                    print("按钮编号无效，请先查询图鉴页面，再选择显示的编号。")
                else:
                    await send("@机器人 " + last_buttons[int(value) - 1].command)
                continue
            if line == "/状态":
                print(f"当前群：{group}；当前成员：{member}；日期：{datetime.now(CST):%Y-%m-%d}（北京时间）")
                print("仅本地模拟，尚未连接 QQ；切换群或成员名称会切换对应的收藏记录。")
                continue
            if line == "/备份":
                path = await asyncio.to_thread(service.backups.manual)
                print(f"备份完成：{path}")
                continue
            if line.startswith("/私聊 "):
                await send(line[4:], is_group=False)
                continue
            await send(line)
    finally:
        await service.close()


def main():
    parser = argparse.ArgumentParser(description="全国图鉴宝可梦机器人本地模拟器")
    parser.add_argument("--once", help="模拟一条消息后退出，例如 --once '@机器人 图鉴'")
    parser.add_argument("--group", default="测试群A")
    parser.add_argument("--member", default="小智")
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument("--backup", action="store_true", help="关闭模拟器后创建手动备份")
    operations.add_argument("--restore", type=Path, help="关闭模拟器后恢复备份；覆盖前自动保存当前数据")
    args = parser.parse_args()
    if sys.version_info < (3, 12):
        parser.error("需要 Python 3.12 或更新版本")
    try:
        setup_logging()
        if args.restore:
            print(f"恢复完成：{restore_backup(args.restore, RUNTIME)}")
        else:
            with InstanceLock(RUNTIME):
                if args.backup:
                    print(f"备份完成：{Backups(Store(RUNTIME / 'collection.sqlite3'), RUNTIME / 'backups').manual()}")
                else:
                    asyncio.run(simulate(args))
        return 0
    except KeyboardInterrupt:
        print("\n模拟器已停止。收藏记录已保存。")
        return 0
    except Exception as exc:
        logging.exception("Application stopped")
        print(f"启动或操作失败：{exc}\n排障日志：{RUNTIME / 'logs' / 'bot.log'}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
