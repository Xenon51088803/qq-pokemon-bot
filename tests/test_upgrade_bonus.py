import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from legacy_database import make_legacy
from pokebot.config import ROOT
from pokebot.maintenance import restore_backup
from pokebot.store import Store, UPGRADE_BONUS_DAY


class UpgradeBonusTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / "runtime" / "tests"
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.runtime = Path(self.temp.name)
        self.path = self.runtime / "collection.sqlite3"
        make_legacy(self.path, rows=[("g", "u", "2026-10-14", 25),
                                     ("g", "u", UPGRADE_BONUS_DAY, 25)])
        self.store = Store(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def bonus(self, **kwargs):
        return self.store.explore("g", "u", UPGRADE_BONUS_DAY, **kwargs)

    def total(self):
        return sum(p["count"] for p in self.store.snapshot("g", "u").values())

    def test_adds_ten_preserves_original_and_replays_after_restart(self):
        first = self.bonus(low=25, high=25)
        self.assertTrue(first.upgrade_bonus)
        self.assertFalse(first.legacy)
        self.assertFalse(first.repeated_today)
        self.assertEqual([p.count for p in first.items], list(range(3, 13)))
        self.assertEqual(first.unlocked_achievements, ())
        self.assertEqual(self.store.snapshot("g", "u")[25], {
            "pokemon_id": 25, "first_day": "2026-10-14", "last_day": UPGRADE_BONUS_DAY, "count": 12})
        with self.store.connection() as conn:
            original = conn.execute("SELECT pokemon_id,count FROM exploration_items WHERE day=?",
                                    (UPGRADE_BONUS_DAY,)).fetchall()
            self.assertEqual([tuple(r) for r in original], [(25, 2)])
        self.store = Store(self.path)
        again = self.bonus(low=1, high=1)
        self.assertEqual(again.items, tuple(type(p)(p.pokemon_id, p.day, p.count, p.is_new, True) for p in first.items))
        self.assertTrue(again.repeated_today)
        self.assertTrue(again.upgrade_bonus)
        self.assertEqual(self.total(), 12)

    def test_concurrent_requests_award_exactly_once(self):
        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(lambda _: self.bonus(), range(30)))
        self.assertEqual(sum(not r.repeated_today for r in results), 1)
        self.assertTrue(all(r.upgrade_bonus for r in results))
        expected = [p.pokemon_id for p in results[0].items]
        self.assertTrue(all([p.pokemon_id for p in r.items] == expected for r in results))
        self.assertEqual(self.total(), 12)

    def test_failure_rolls_back_bonus_and_keeps_eligibility(self):
        with self.store.connection() as conn:
            conn.execute("CREATE TRIGGER fail_bonus BEFORE INSERT ON upgrade_exploration_items WHEN NEW.slot=5 BEGIN SELECT RAISE(ABORT,'test'); END")
        before = self.store.snapshot("g", "u")
        before_achievements = self.store.achievement_status("g", "u")
        with self.assertRaises(sqlite3.IntegrityError):
            self.bonus()
        self.assertEqual(self.store.snapshot("g", "u"), before)
        self.assertEqual(self.store.achievement_status("g", "u"), before_achievements)
        with self.store.connection() as conn:
            for table in ("upgrade_explorations", "upgrade_exploration_items"):
                self.assertEqual(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM exploration_items").fetchone()[0], 2)
            conn.execute("DROP TRIGGER fail_bonus")
        self.assertFalse(self.bonus().repeated_today)
        self.assertEqual(self.total(), 12)

    def test_guarantee_accounts_for_original_single_capture(self):
        with patch("pokebot.store.secrets.randbelow", return_value=0):
            result = self.bonus(low=25, high=26)
        self.assertTrue(result.guaranteed)
        self.assertEqual([p.pokemon_id for p in result.items], [25] * 9 + [26])
        self.assertEqual(sum(p.is_new for p in result.items), 1)
        self.assertEqual(self.total(), 12)

    def test_only_launch_day_legacy_members_are_eligible(self):
        historical = self.store.explore("g", "u", "2026-10-14")
        self.assertTrue(historical.legacy)
        self.assertFalse(historical.upgrade_bonus)
        self.assertEqual(len(historical.items), 1)
        for group, member in (("g", "other"), ("other", "u")):
            first = self.store.explore(group, member, UPGRADE_BONUS_DAY)
            again = self.store.explore(group, member, UPGRADE_BONUS_DAY)
            self.assertFalse(first.upgrade_bonus)
            self.assertTrue(again.repeated_today)
            self.assertEqual(sum(p["count"] for p in self.store.snapshot(group, member).values()), 10)
        # The fixture compatibility draw is not an old-version daily result.
        self.store.draw("g", "fixture", UPGRADE_BONUS_DAY, 25, 25)
        self.assertFalse(self.store.explore("g", "fixture", UPGRADE_BONUS_DAY).upgrade_bonus)
        self.assertEqual(self.total(), 2)
        self.bonus()
        tomorrow = self.store.explore("g", "u", "2026-10-16")
        self.assertFalse(tomorrow.upgrade_bonus)
        self.assertEqual(self.total(), 22)

    def test_backup_restore_keeps_bonus_and_original(self):
        first = self.bonus()
        backup = self.runtime / "snapshot.sqlite3"
        self.store.backup(backup)
        self.store.explore("g", "u", "2026-10-16")
        restore_backup(backup, self.runtime)
        self.store = Store(self.path)
        again = self.bonus()
        self.assertTrue(again.upgrade_bonus)
        self.assertTrue(again.repeated_today)
        self.assertEqual([p.pokemon_id for p in first.items], [p.pokemon_id for p in again.items])
        self.assertEqual(self.total(), 12)
        self.assertTrue(self.store.explore("g", "u", "2026-10-14").legacy)

    def test_v3_migration_backs_up_then_supports_claim_and_old_restore(self):
        path = self.runtime / "v3" / "collection.sqlite3"
        make_legacy(path, rows=[("g", "u", UPGRADE_BONUS_DAY, 25)])
        # Run the genuine national migration, stopping before the new v4 step.
        with patch.object(Store, "_migrate_bonus", side_effect=lambda conn: conn.execute("PRAGMA user_version=3")):
            Store(path)
        store = Store(path)
        backup = path.parent / "backups" / "before-upgrade-bonus-v4.sqlite3"
        with closing(sqlite3.connect(backup)) as conn:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='upgrade_explorations'").fetchone())
        self.assertTrue(store.explore("g", "u", UPGRADE_BONUS_DAY).upgrade_bonus)
        restore_backup(backup, path.parent)
        store = Store(path)
        self.assertFalse(store.explore("g", "u", UPGRADE_BONUS_DAY).repeated_today)
        self.assertEqual(sum(p["count"] for p in store.snapshot("g", "u").values()), 11)


if __name__ == "__main__":
    unittest.main()
