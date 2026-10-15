import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from legacy_database import make_legacy
from pokebot.config import ROOT
from pokebot.dex import GENERATIONS, NATIONAL, TOTAL_SPECIES
from pokebot.maintenance import restore_backup
from pokebot.store import Store


class NationalTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / "runtime" / "tests"
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.runtime = Path(self.temp.name)
        self.store = Store(self.runtime / "collection.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def batch(self, day="2026-10-15", group="g", member="u", low=1, high=1025):
        return self.store.explore(group, member, day, low, high)

    def seed(self, species):
        with self.store.connection() as conn:
            conn.executemany("INSERT INTO collection VALUES ('g','u',?,'2026-10-14','2026-10-14',1)", [(p,) for p in species])

    def test_ten_items_repeat_restart_and_pool_change_keep_batch(self):
        first = self.batch(low=1025, high=1025)
        self.assertEqual(len(first.items), 10)
        self.assertEqual(sum(p.is_new for p in first.items), 1)
        self.assertEqual([p.count for p in first.items], list(range(1, 11)))
        self.assertFalse(first.guaranteed)
        self.store = Store(self.store.path)
        again = self.batch(low=1, high=1)
        self.assertEqual([(p.pokemon_id, p.count, p.is_new) for p in first.items],
                         [(p.pokemon_id, p.count, p.is_new) for p in again.items])
        self.assertTrue(again.repeated_today)
        self.assertEqual(again.unlocked_achievements, ())
        self.assertEqual(self.store.snapshot("g", "u")[1025]["count"], 10)

    def test_guarantee_replaces_one_duplicate_and_is_saved(self):
        self.seed([1])
        with patch("pokebot.store.secrets.randbelow", return_value=0):
            batch = self.batch(low=1, high=3)
        self.assertTrue(batch.guaranteed)
        self.assertEqual([p.pokemon_id for p in batch.items], [1] * 9 + [2])
        self.assertEqual(sum(p.is_new for p in batch.items), 1)
        self.assertEqual(self.store.snapshot("g", "u")[1]["count"], 10)
        self.assertEqual(self.store.snapshot("g", "u")[2]["count"], 1)
        self.assertTrue(self.batch().guaranteed)
        self.assertNotIn(3, self.store.snapshot("g", "u"))

    def test_naturally_new_batch_is_not_modified_and_complete_pool_stops_guarantee(self):
        self.seed([1])
        with patch("pokebot.store.secrets.randbelow", side_effect=[0] * 9 + [1]) as random:
            batch = self.batch(low=1, high=2)
        self.assertEqual(random.call_count, 10)
        self.assertFalse(batch.guaranteed)
        self.assertEqual(batch.items[-1].pokemon_id, 2)
        with patch("pokebot.store.secrets.randbelow", return_value=0) as random:
            batch = self.batch(day="2026-10-16", low=1, high=2)
        self.assertEqual(random.call_count, 10)
        self.assertFalse(batch.guaranteed)
        self.assertEqual(sum(p.is_new for p in batch.items), 0)

    def test_national_complete_pool_can_still_explore(self):
        self.seed(range(1, 1026))
        batch = self.batch(low=1, high=1025)
        self.assertEqual(len(batch.items), 10)
        self.assertFalse(batch.guaranteed)
        self.assertEqual(sum(p.is_new for p in batch.items), 0)
        statuses = self.store.achievement_status("g", "u")
        self.assertTrue(all(p["unlocked_day"] for p in statuses))
        self.assertEqual(len(self.store.snapshot("g", "u")), 1025)

    def test_concurrent_batches_capture_only_ten_and_award_once(self):
        with ThreadPoolExecutor(max_workers=10) as pool:
            batches = list(pool.map(lambda _: self.batch(), range(30)))
        expected = tuple(p.pokemon_id for p in batches[0].items)
        self.assertTrue(all(tuple(p.pokemon_id for p in b.items) == expected for b in batches))
        self.assertEqual(sum(not b.repeated_today for b in batches), 1)
        self.assertEqual(sum(p["count"] for p in self.store.snapshot("g", "u").values()), 10)
        self.assertEqual(sum("first_capture" in b.unlocked_achievements for b in batches), 1)

    def test_partial_batch_failure_rolls_back_all_tables(self):
        with self.store.connection() as conn:
            conn.execute("CREATE TRIGGER fail_fifth BEFORE INSERT ON exploration_items WHEN NEW.slot=5 BEGIN SELECT RAISE(ABORT,'test'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.batch()
        for table in ("explorations", "exploration_items", "collection", "achievements"):
            with self.store.connection() as conn:
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)

    def test_guarantee_isolated_by_member_group_and_next_day(self):
        self.seed([1])
        with patch("pokebot.store.secrets.randbelow", return_value=0):
            self.assertTrue(self.batch(low=1, high=3).guaranteed)
            self.assertFalse(self.batch(member="other", low=1, high=3).guaranteed)
            self.assertFalse(self.batch(group="other", low=1, high=3).guaranteed)
            self.assertEqual(self.batch(day="2026-10-16", low=1, high=3).items[-1].pokemon_id, 3)

    def test_generation_pages_cover_all_species_exactly_and_kanto_stays_151(self):
        all_species = []
        for generation in GENERATIONS.values():
            ids = [p for page in range(1, generation.pages + 1) for p in generation.ids(page)]
            self.assertEqual(ids, list(generation.ids()))
            all_species += ids
        self.assertEqual(all_species, list(range(1, 1026)))
        self.assertEqual(list(GENERATIONS[1].ids(6)), [151])
        self.assertEqual(NATIONAL.pages, 35)
        self.assertEqual(list(NATIONAL.ids(35)), list(range(1021, 1026)))

    def test_new_generations_do_not_award_kanto_master(self):
        self.seed(range(152, 303))
        self.batch(low=152, high=152)
        statuses = {a["id"]: a for a in self.store.achievement_status("g", "u")}
        self.assertIsNone(statuses["species_151"]["unlocked_day"])
        self.assertIsNone(statuses["species_100"]["unlocked_day"])
        self.assertEqual(statuses["species_151"]["progress"], 0)
        self.assertTrue(statuses["generation_2"]["unlocked_day"])

    def test_real_v1_v2_migrations_preserve_legacy_day_and_allow_next_day_batch(self):
        for version in (1, 2):
            runtime = self.runtime / f"legacy-{version}"
            path = runtime / "collection.sqlite3"
            make_legacy(path, version)
            store = Store(path)
            old = store.explore("g", "u", "2026-01-04")
            self.assertTrue(old.legacy)
            self.assertTrue(old.repeated_today)
            self.assertEqual([(p.pokemon_id, p.count) for p in old.items], [(7, 2)])
            self.assertTrue(store.seen("g", "old-event"))
            self.assertEqual(store.snapshot("g", "u")[7], {"pokemon_id":7,"first_day":"2026-01-03","last_day":"2026-01-04","count":2})
            statuses = {a["id"]: a["unlocked_day"] for a in store.achievement_status("g", "u")}
            self.assertEqual(statuses["first_capture"], "2026-01-01")
            self.assertEqual(statuses["kanto_starters"], "2026-01-03")
            with closing(sqlite3.connect(runtime / "backups" / "before-national-v3.sqlite3")) as conn:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], version)
            fresh = Store(path).explore("g", "u", "2026-01-05", 1025, 1025)
            self.assertEqual(len(fresh.items), 10)
            self.assertNotIn("first_capture", fresh.unlocked_achievements)
            self.assertEqual(store.snapshot("g", "u")[1025]["count"], 10)

    def test_failed_migration_rolls_back_schema_and_retains_backup(self):
        runtime = self.runtime / "broken-legacy"
        path = runtime / "collection.sqlite3"
        make_legacy(path, 2)
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("DELETE FROM collection WHERE pokemon_id=7")
            conn.commit()
        with self.assertRaises(ValueError):
            Store(path)
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT count(*) FROM draws").fetchone()[0], 4)
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='explorations'").fetchone())
        self.assertTrue((runtime / "backups" / "before-national-v3.sqlite3").is_file())

    def test_v3_backup_restores_exact_batch_and_guarantee(self):
        self.seed([1])
        with patch("pokebot.store.secrets.randbelow", return_value=0):
            saved = self.batch(low=1, high=2)
        backup = self.runtime / "snapshot.sqlite3"
        self.store.backup(backup)
        self.batch(day="2026-10-16")
        restore_backup(backup, self.runtime)
        restored = Store(self.store.path).explore("g", "u", "2026-10-15")
        self.assertTrue(restored.guaranteed)
        self.assertEqual([(p.pokemon_id,p.count,p.is_new) for p in restored.items],
                         [(p.pokemon_id,p.count,p.is_new) for p in saved.items])
        self.assertEqual(sum(p["count"] for p in self.store.snapshot("g", "u").values()), 11)
