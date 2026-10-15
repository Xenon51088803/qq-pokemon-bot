import os
import sqlite3
from datetime import datetime
from pathlib import Path

from .models import CST
from .store import Store


class InstanceLock:
    """Prevent two simulator processes (or a restore) touching one runtime."""
    def __init__(self, runtime: Path):
        self.path = runtime / "instance.lock"
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        try:
            if os.fstat(self.file.fileno()).st_size == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise RuntimeError("模拟器正在运行，请先关闭它再启动另一实例或恢复备份") from None
        return self

    def __exit__(self, *args):
        if self.file:
            self.file.close()


class Backups:
    def __init__(self, store: Store, folder: Path):
        self.store = store
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)

    def maintain(self, now: datetime):
        local = now.astimezone(CST)
        daily = self.folder / f"daily-{local:%Y-%m-%d}.sqlite3"
        if not daily.exists():
            self.store.backup(daily)
        year, week, _ = local.isocalendar()
        weekly = self.folder / f"weekly-{year}-{week:02}.sqlite3"
        if not weekly.exists():
            self.store.backup(weekly)
        for prefix, count in (("daily", 7), ("weekly", 4)):
            for old in sorted(self.folder.glob(f"{prefix}-*.sqlite3"), reverse=True)[count:]:
                old.unlink()

    def manual(self):
        target = self.folder / f"manual-{datetime.now(CST):%Y%m%d-%H%M%S-%f}.sqlite3"
        self.store.backup(target)
        return target


def restore_backup(source: Path, runtime: Path):
    source = source.resolve(strict=True)
    target = runtime / "collection.sqlite3"
    if source == target.resolve():
        raise ValueError("不能把正在使用的数据库作为恢复源")
    with InstanceLock(runtime):
        check = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("备份文件完整性校验失败")
            version = check.execute("PRAGMA user_version").fetchone()[0]
            if version not in (1, 2, 3, 4):
                raise ValueError("备份版本不匹配")
            tables = {
                "draws": {"group_id", "member_id", "day", "pokemon_id"},
                "collection": {"group_id", "member_id", "pokemon_id", "first_day", "last_day", "count"},
                "processed_events": {"group_id", "message_id", "processed_at"},
            }
            if version >= 2:
                tables["achievements"] = {"group_id", "member_id", "achievement_id", "unlocked_day"}
            if version >= 3:
                del tables["draws"]
                tables["explorations"] = {"group_id", "member_id", "day", "guaranteed", "legacy"}
                tables["exploration_items"] = {"group_id", "member_id", "day", "slot", "pokemon_id", "is_new", "count"}
            if version >= 4:
                tables["upgrade_explorations"] = tables["explorations"]
                tables["upgrade_exploration_items"] = tables["exploration_items"]
            for table, columns in tables.items():
                found = {r[1] for r in check.execute(f"PRAGMA table_info({table})")}
                if not columns <= found:
                    raise ValueError("文件不是本程序的完整数据库备份")
            if target.exists():
                Backups(Store(target), runtime / "backups").manual()
            temp = runtime / "restore.tmp"
            dest = sqlite3.connect(temp)
            try:
                check.backup(dest)
                dest.execute("PRAGMA journal_mode=DELETE")
            finally:
                dest.close()
        finally:
            check.close()
        # No active process may hold these files: InstanceLock is acquired above.
        for suffix in ("-wal", "-shm"):
            Path(str(target) + suffix).unlink(missing_ok=True)
        temp.replace(target)
        # Old rendered pages may reflect collection state before restoration.
        for image in (runtime / "images").glob("*.png"):
            image.unlink()
    return target
