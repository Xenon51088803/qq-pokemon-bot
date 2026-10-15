import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .achievements import evaluate
from .dex import TOTAL_SPECIES
from .models import Draw, Exploration


# One-time compensation for members who used the single-draw version on launch day.
UPGRADE_BONUS_DAY = "2026-10-15"


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4):
                raise ValueError("数据库版本高于当前程序支持的版本，请更新程序")
            if version in (1, 2):
                before_upgrade = path.parent / "backups" / "before-national-v3.sqlite3"
                if not before_upgrade.exists():
                    self.backup(before_upgrade)
            elif version == 3:
                before_upgrade = path.parent / "backups" / "before-upgrade-bonus-v4.sqlite3"
                if not before_upgrade.exists():
                    self.backup(before_upgrade)
            conn.execute("PRAGMA journal_mode=WAL")
            self._migrate(conn)

    def _migrate(self, conn):
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version in (3, 4):
                if version == 3:
                    self._migrate_bonus(conn)
                conn.commit()
                return
            if version in (1, 2):
                conn.execute("ALTER TABLE collection RENAME TO collection_v2")
            conn.execute(f"""
                CREATE TABLE collection (
                    group_id TEXT NOT NULL, member_id TEXT NOT NULL,
                    pokemon_id INTEGER NOT NULL CHECK(pokemon_id BETWEEN 1 AND {TOTAL_SPECIES}),
                    first_day TEXT NOT NULL, last_day TEXT NOT NULL,
                    count INTEGER NOT NULL CHECK(count > 0),
                    PRIMARY KEY(group_id, member_id, pokemon_id)
                )
            """)
            if version in (1, 2):
                conn.execute("INSERT INTO collection SELECT * FROM collection_v2")
                conn.execute("DROP TABLE collection_v2")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS processed_events (
                    group_id TEXT NOT NULL, message_id TEXT NOT NULL, processed_at REAL NOT NULL,
                    PRIMARY KEY(group_id, message_id)
                )
            """)
            conn.execute("""
                CREATE TABLE explorations (
                    group_id TEXT NOT NULL, member_id TEXT NOT NULL, day TEXT NOT NULL,
                    guaranteed INTEGER NOT NULL CHECK(guaranteed IN (0,1)),
                    legacy INTEGER NOT NULL CHECK(legacy IN (0,1)),
                    PRIMARY KEY(group_id, member_id, day)
                )
            """)
            conn.execute(f"""
                CREATE TABLE exploration_items (
                    group_id TEXT NOT NULL, member_id TEXT NOT NULL, day TEXT NOT NULL,
                    slot INTEGER NOT NULL CHECK(slot BETWEEN 1 AND 10),
                    pokemon_id INTEGER NOT NULL CHECK(pokemon_id BETWEEN 1 AND {TOTAL_SPECIES}),
                    is_new INTEGER NOT NULL CHECK(is_new IN (0,1)),
                    count INTEGER NOT NULL CHECK(count > 0),
                    PRIMARY KEY(group_id, member_id, day, slot),
                    FOREIGN KEY(group_id, member_id, day) REFERENCES explorations(group_id, member_id, day)
                )
            """)
            if version in (1, 2):
                conn.execute("INSERT INTO explorations SELECT group_id, member_id, day, 0, 1 FROM draws")
                conn.execute("""
                    INSERT INTO exploration_items
                    SELECT d.group_id, d.member_id, d.day, 1, d.pokemon_id,
                           CASE WHEN c.first_day=d.day THEN 1 ELSE 0 END,
                           COUNT(*) OVER (PARTITION BY d.group_id, d.member_id, d.pokemon_id ORDER BY d.day)
                    FROM draws d JOIN collection c ON c.group_id=d.group_id
                        AND c.member_id=d.member_id AND c.pokemon_id=d.pokemon_id
                """)
                if conn.execute("SELECT count(*) FROM exploration_items").fetchone()[0] != conn.execute("SELECT count(*) FROM draws").fetchone()[0]:
                    raise ValueError("旧抽取与收藏记录不一致，升级已回滚，请从备份检查")
                conn.execute("DROP TABLE draws")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS achievements (
                    group_id TEXT NOT NULL, member_id TEXT NOT NULL,
                    achievement_id TEXT NOT NULL, unlocked_day TEXT NOT NULL,
                    PRIMARY KEY(group_id, member_id, achievement_id)
                )
            """)
            trainers = conn.execute("SELECT DISTINCT group_id, member_id FROM collection").fetchall()
            for trainer in trainers:
                self._award_achievements(conn, trainer["group_id"], trainer["member_id"])
            self._migrate_bonus(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    @staticmethod
    def _migrate_bonus(conn):
        # Keep the original daily exploration and its item unchanged. The extra
        # batch has its own ledger, committed atomically with captures/achievements.
        conn.execute("""
            CREATE TABLE upgrade_explorations (
                group_id TEXT NOT NULL, member_id TEXT NOT NULL, day TEXT NOT NULL,
                guaranteed INTEGER NOT NULL CHECK(guaranteed IN (0,1)),
                legacy INTEGER NOT NULL CHECK(legacy=0),
                PRIMARY KEY(group_id, member_id, day)
            )
        """)
        conn.execute(f"""
            CREATE TABLE upgrade_exploration_items (
                group_id TEXT NOT NULL, member_id TEXT NOT NULL, day TEXT NOT NULL,
                slot INTEGER NOT NULL CHECK(slot BETWEEN 1 AND 10),
                pokemon_id INTEGER NOT NULL CHECK(pokemon_id BETWEEN 1 AND {TOTAL_SPECIES}),
                is_new INTEGER NOT NULL CHECK(is_new IN (0,1)),
                count INTEGER NOT NULL CHECK(count>0),
                PRIMARY KEY(group_id, member_id, day, slot),
                FOREIGN KEY(group_id, member_id, day)
                    REFERENCES upgrade_explorations(group_id, member_id, day),
                FOREIGN KEY(group_id, member_id, day)
                    REFERENCES explorations(group_id, member_id, day)
            )
        """)
        conn.execute("PRAGMA user_version=4")

    @staticmethod
    def _award_achievements(conn, group, member):
        captures = conn.execute("SELECT pokemon_id, first_day FROM collection WHERE group_id=? AND member_id=?",
                                (group, member)).fetchall()
        unlocked = []
        for achievement, _, day in evaluate(captures):
            if day:
                inserted = conn.execute("INSERT OR IGNORE INTO achievements VALUES (?, ?, ?, ?)",
                                        (group, member, achievement.id, day))
                if inserted.rowcount:
                    unlocked.append(achievement.id)
        return tuple(unlocked)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    def draw(self, group: str, member: str, day: str, low: int, high: int) -> Draw:
        """Single-item compatibility entry for fixtures and legacy integrations."""
        result = self.explore(group, member, day, low, high, size=1, guarantee=False)
        item = result.items[0]
        return Draw(item.pokemon_id, day, item.count, item.is_new and not result.repeated_today,
                    result.repeated_today, result.unlocked_achievements)

    def explore(self, group: str, member: str, day: str, low=1, high=TOTAL_SPECIES,
                *, size=10, guarantee=True) -> Exploration:
        if not 1 <= low <= high <= TOTAL_SPECIES or not 1 <= size <= 10:
            raise ValueError("探索范围或数量无效")
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                old = conn.execute(
                    "SELECT guaranteed, legacy FROM explorations WHERE group_id=? AND member_id=? AND day=?",
                    (group, member, day),
                ).fetchone()
                upgrade_bonus = bool(old and old["legacy"] and day == UPGRADE_BONUS_DAY and size == 10)
                batch_table = "upgrade_explorations" if upgrade_bonus else "explorations"
                item_table = "upgrade_exploration_items" if upgrade_bonus else "exploration_items"
                if upgrade_bonus:
                    old = conn.execute(
                        "SELECT guaranteed, legacy FROM upgrade_explorations WHERE group_id=? AND member_id=? AND day=?",
                        (group, member, day),
                    ).fetchone()
                if old:
                    items = conn.execute(f"""SELECT pokemon_id, count, is_new FROM {item_table}
                        WHERE group_id=? AND member_id=? AND day=? ORDER BY slot""", (group, member, day)).fetchall()
                    result = Exploration(day, tuple(Draw(r["pokemon_id"], day, r["count"], bool(r["is_new"]), True)
                                                    for r in items), True, bool(old["guaranteed"]), bool(old["legacy"]),
                                         upgrade_bonus=upgrade_bonus)
                else:
                    owned = {r[0] for r in conn.execute("SELECT pokemon_id FROM collection WHERE group_id=? AND member_id=?", (group, member))}
                    species = [low + secrets.randbelow(high - low + 1) for _ in range(size)]
                    forced = False
                    if guarantee and all(p in owned for p in species):
                        missing = [p for p in range(low, high + 1) if p not in owned]
                        if missing:
                            species[-1] = missing[secrets.randbelow(len(missing))]
                            forced = True
                    conn.execute(f"INSERT INTO {batch_table} VALUES (?, ?, ?, ?, 0)", (group, member, day, int(forced)))
                    items = []
                    for slot, pokemon_id in enumerate(species, 1):
                        is_new = pokemon_id not in owned
                        owned.add(pokemon_id)
                        conn.execute("""
                            INSERT INTO collection VALUES (?, ?, ?, ?, ?, 1)
                            ON CONFLICT(group_id, member_id, pokemon_id) DO UPDATE SET
                            first_day=MIN(first_day, excluded.first_day),
                            last_day=MAX(last_day, excluded.last_day), count=count+1
                        """, (group, member, pokemon_id, day, day))
                        count = conn.execute("SELECT count FROM collection WHERE group_id=? AND member_id=? AND pokemon_id=?",
                                             (group, member, pokemon_id)).fetchone()["count"]
                        conn.execute(f"INSERT INTO {item_table} VALUES (?, ?, ?, ?, ?, ?, ?)",
                                     (group, member, day, slot, pokemon_id, int(is_new), count))
                        items.append(Draw(pokemon_id, day, count, is_new, False))
                    unlocked = self._award_achievements(conn, group, member)
                    result = Exploration(day, tuple(items), False, forced, False, unlocked, upgrade_bonus)
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise

    def snapshot(self, group: str, member: str) -> dict[int, dict]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT pokemon_id, first_day, last_day, count FROM collection WHERE group_id=? AND member_id=? ORDER BY pokemon_id",
                (group, member),
            ).fetchall()
        return {r["pokemon_id"]: dict(r) for r in rows}

    def seen(self, group: str, message_id: str) -> bool:
        with self.connection() as conn:
            return conn.execute("SELECT 1 FROM processed_events WHERE group_id=? AND message_id=?",
                                (group, message_id)).fetchone() is not None

    def achievement_status(self, group: str, member: str) -> list[dict]:
        with self.connection() as conn:
            conn.execute("BEGIN")
            captures = conn.execute("SELECT pokemon_id, first_day FROM collection WHERE group_id=? AND member_id=?",
                                    (group, member)).fetchall()
            unlocked = {r["achievement_id"]: r["unlocked_day"] for r in conn.execute(
                "SELECT achievement_id, unlocked_day FROM achievements WHERE group_id=? AND member_id=?",
                (group, member))}
            conn.commit()
        return [{"id": a.id, "title": a.title, "description": a.description,
                 "progress": progress, "target": a.target, "unlocked_day": unlocked.get(a.id)}
                for a, progress, _ in evaluate(captures)]

    def mark_seen(self, group: str, message_id: str, now: float):
        with self.connection() as conn:
            conn.execute("INSERT OR IGNORE INTO processed_events VALUES (?, ?, ?)", (group, message_id, now))

    def clean_events(self, cutoff: float):
        with self.connection() as conn:
            conn.execute("DELETE FROM processed_events WHERE processed_at < ?", (cutoff,))

    def backup(self, target: Path):
        target.parent.mkdir(parents=True, exist_ok=True)
        temp = target.with_suffix(".tmp")
        try:
            with self.connection() as source:
                dest = sqlite3.connect(temp)
                try:
                    source.backup(dest)
                    if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("备份完整性校验失败")
                finally:
                    dest.close()
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
