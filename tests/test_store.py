import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

from pokebot.config import ROOT
from pokebot.maintenance import Backups, InstanceLock, restore_backup
from pokebot.models import CST
from pokebot.store import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / "runtime" / "tests"
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.runtime = Path(self.temp.name)
        self.store = Store(self.runtime / "collection.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_repeat_restart_and_changed_pool_keep_result(self):
        first = self.store.draw("g", "u", "2026-10-14", 25, 25)
        self.assertTrue(first.is_new)
        restarted = Store(self.store.path)
        again = restarted.draw("g", "u", "2026-10-14", 1, 1)
        self.assertEqual((again.pokemon_id, again.count, again.repeated_today), (25, 1, True))

    def test_next_day_same_species_increments_only_count(self):
        self.store.draw("g", "u", "2026-10-14", 25, 25)
        result = self.store.draw("g", "u", "2026-10-15", 25, 25)
        self.assertEqual((result.count, result.is_new), (2, False))
        self.assertEqual(self.store.snapshot("g", "u")[25],
                         {"pokemon_id": 25, "first_day": "2026-10-14", "last_day": "2026-10-15", "count": 2})

    def test_concurrent_requests_are_atomic(self):
        def draw(_):
            return self.store.draw("g", "u", "2026-10-14", 1, 151)
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(draw, range(40)))
        self.assertEqual(len({r.pokemon_id for r in results}), 1)
        self.assertEqual(sum(not r.repeated_today for r in results), 1)
        self.assertEqual(sum(p["count"] for p in self.store.snapshot("g", "u").values()), 1)

    def test_groups_and_members_are_independent(self):
        for group, user, species in (("a", "u", 1), ("b", "u", 2), ("a", "v", 3)):
            self.store.draw(group, user, "2026-10-14", species, species)
            self.assertEqual(set(self.store.snapshot(group, user)), {species})

    def test_failed_collection_write_rolls_back_draw(self):
        with self.store.connection() as conn:
            conn.execute("CREATE TRIGGER fail_capture BEFORE INSERT ON collection BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.draw("g", "u", "2026-10-14", 1, 1)
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM explorations").fetchone()[0], 0)
            conn.execute("DROP TRIGGER fail_capture")
        self.assertTrue(self.store.draw("g", "u", "2026-10-14", 1, 1).is_new)

    def test_backup_restore_preserves_record_and_saves_current(self):
        self.store.draw("g", "u", "2026-10-14", 25, 25)
        backup = self.runtime / "backups" / "snapshot.sqlite3"
        self.store.backup(backup)
        self.store.draw("g", "u", "2026-10-15", 25, 25)
        restore_backup(backup, self.runtime)
        restored = Store(self.store.path)
        self.assertEqual(restored.snapshot("g", "u")[25]["count"], 1)
        self.assertTrue(list((self.runtime / "backups").glob("manual-*.sqlite3")))
        self.assertTrue(restored.draw("g", "u", "2026-10-14", 1, 1).repeated_today)

    def test_bad_backup_leaves_current_database_unchanged(self):
        self.store.draw("g", "u", "2026-10-14", 25, 25)
        invalid = self.runtime / "invalid.sqlite3"
        invalid.write_text("not a database", encoding="utf-8")
        with self.assertRaises(sqlite3.DatabaseError):
            restore_backup(invalid, self.runtime)
        self.assertEqual(self.store.snapshot("g", "u")[25]["count"], 1)

    def test_lock_prevents_restore_and_second_instance(self):
        with InstanceLock(self.runtime):
            with self.assertRaises(RuntimeError):
                with InstanceLock(self.runtime):
                    pass
        with InstanceLock(self.runtime):
            pass

    def test_automatic_backup_retention(self):
        backups = Backups(self.store, self.runtime / "backups")
        start = datetime(2026, 8, 1, tzinfo=CST)
        for day in range(40):
            backups.maintain(start + timedelta(days=day))
        self.assertEqual(len(list(backups.folder.glob("daily-*.sqlite3"))), 7)
        self.assertEqual(len(list(backups.folder.glob("weekly-*.sqlite3"))), 4)

    def test_processed_event_retention(self):
        self.store.mark_seen("g", "old", 1)
        self.store.mark_seen("g", "new", 100)
        self.store.clean_events(50)
        self.assertFalse(self.store.seen("g", "old"))
        self.assertTrue(self.store.seen("g", "new"))


if __name__ == "__main__":
    unittest.main()
