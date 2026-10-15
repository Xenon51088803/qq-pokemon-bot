import sqlite3
import tempfile
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

from pokebot.config import ROOT
from pokebot.maintenance import restore_backup
from pokebot.store import Store
from legacy_database import make_legacy


class AchievementTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / "runtime" / "tests"
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.runtime = Path(self.temp.name)
        self.store = Store(self.runtime / "collection.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def capture(self, species, day, group="g", member="u"):
        return self.store.draw(group, member, (date(2026, 1, 1) + timedelta(days=day)).isoformat(), species, species)

    def statuses(self, group="g", member="u"):
        return {a["id"]: a for a in self.store.achievement_status(group, member)}

    def test_first_capture_repeat_restart_and_duplicates(self):
        self.assertEqual(self.capture(25, 0).unlocked_achievements, ("first_capture",))
        self.assertEqual(self.capture(25, 0).unlocked_achievements, ())
        self.store = Store(self.store.path)
        for day in range(1, 12):
            self.assertEqual(self.capture(25, day).unlocked_achievements, ())
        self.assertEqual(self.statuses()["species_10"]["progress"], 1)
        self.assertEqual(self.statuses()["first_capture"]["unlocked_day"], "2026-01-01")

    def test_all_milestones_and_historical_dates(self):
        thresholds = {1: "first_capture", 10: "species_10", 50: "species_50", 100: "species_100", 151: "species_151"}
        for species in range(1, 152):
            result = self.capture(species, species - 1)
            expected = ([thresholds[species]] if species in thresholds else [])
            if species == 7:
                expected.append("kanto_starters")
            self.assertEqual(result.unlocked_achievements, tuple(expected))
        states = self.statuses()
        self.assertEqual(len(states), 15)
        self.assertTrue(all(s["unlocked_day"] and s["progress"] == s["target"] for s in list(states.values())[:6]))
        self.assertEqual(states["species_151"]["unlocked_day"], "2026-05-31")

    def test_starters_require_exact_species_and_isolate_trainers(self):
        for day, species in enumerate((2, 5, 8, 1, 4)):
            self.capture(species, day)
        self.capture(7, 0, group="other")
        self.capture(7, 0, member="other")
        self.assertEqual(self.statuses()["kanto_starters"]["progress"], 2)
        self.assertIsNone(self.statuses()["kanto_starters"]["unlocked_day"])
        self.assertEqual(self.capture(7, 5).unlocked_achievements, ("kanto_starters",))
        self.assertEqual(self.statuses("other")["kanto_starters"]["progress"], 1)
        self.assertEqual(self.statuses(member="other")["kanto_starters"]["progress"], 1)
        self.assertTrue(all(s["progress"] == 0 for s in self.statuses("empty").values()))

    def test_concurrency_awards_once(self):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.capture(1, 0), range(20)))
        self.assertEqual(sum("first_capture" in r.unlocked_achievements for r in results), 1)
        self.assertEqual(self.store.snapshot("g", "u")[1]["count"], 1)

    def test_failed_award_rolls_back_capture_and_draw(self):
        with self.store.connection() as conn:
            conn.execute("CREATE TRIGGER fail_award BEFORE INSERT ON achievements BEGIN SELECT RAISE(ABORT, 'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.capture(1, 0)
        self.assertEqual(self.store.snapshot("g", "u"), {})
        with self.store.connection() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM explorations").fetchone()[0], 0)
            conn.execute("DROP TRIGGER fail_award")
        self.assertEqual(self.capture(1, 0).unlocked_achievements, ("first_capture",))

    def test_v1_migration_backfills_preserves_data_and_restore(self):
        legacy_runtime = self.runtime / "legacy"
        make_legacy(legacy_runtime / "collection.sqlite3")
        self.store = Store(legacy_runtime / "collection.sqlite3")
        before = self.store.snapshot("g", "u")
        self.store = Store(self.store.path)
        self.assertEqual(self.store.snapshot("g", "u"), before)
        self.assertTrue(self.store.seen("g", "old-event"))
        self.assertEqual(self.statuses()["kanto_starters"]["unlocked_day"], "2026-01-03")
        self.assertEqual(self.statuses()["first_capture"]["unlocked_day"], "2026-01-01")
        self.assertEqual(self.capture(7, 3).unlocked_achievements, ())
        self.store = Store(self.store.path)
        self.assertEqual(self.capture(7, 4).unlocked_achievements, ())
        backup = legacy_runtime / "backups" / "before-national-v3.sqlite3"
        with closing(sqlite3.connect(backup)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT count FROM collection WHERE pokemon_id=7").fetchone()[0], 2)
        restore_backup(backup, legacy_runtime)
        self.store = Store(self.store.path)
        self.assertEqual(self.store.snapshot("g", "u"), before)
        self.assertEqual(self.statuses()["kanto_starters"]["unlocked_day"], "2026-01-03")

    def test_v2_backup_restores_awards_and_repeat_suppression(self):
        self.capture(1, 0)
        backup = self.runtime / "snapshot.sqlite3"
        self.store.backup(backup)
        self.capture(4, 1)
        self.capture(7, 2)
        restore_backup(backup, self.runtime)
        self.store = Store(self.store.path)
        self.assertIsNone(self.statuses()["kanto_starters"]["unlocked_day"])
        self.assertEqual(self.statuses()["first_capture"]["unlocked_day"], "2026-01-01")
        self.assertEqual(self.capture(1, 0).unlocked_achievements, ())
