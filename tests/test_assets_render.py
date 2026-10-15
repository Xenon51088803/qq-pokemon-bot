import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from pokebot.catalog import Catalog
from pokebot.config import Config, ROOT
from pokebot.render import Renderer


class AssetRenderTests(unittest.TestCase):
    def setUp(self):
        folder = ROOT / "runtime" / "tests"
        folder.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=folder)
        self.catalog = Catalog(ROOT / "assets")
        self.renderer = Renderer(self.catalog, Path(self.temp.name), Config())

    def tearDown(self):
        self.temp.cleanup()

    def test_catalog_and_all_images_complete_and_manifest_matches(self):
        self.assertEqual(set(self.catalog.by_id), set(range(1, 1026)))
        self.assertEqual(self.catalog.by_id[1]["name"], "妙蛙种子")
        self.assertEqual(self.catalog.by_id[25]["name"], "皮卡丘")
        self.assertEqual(self.catalog.by_id[151]["name"], "梦幻")
        manifest = json.loads((ROOT / "assets" / "sources.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["failures"], [])
        self.assertEqual(hashlib.sha256((ROOT / "assets" / "pokemon.json").read_bytes()).hexdigest(), manifest["names_sha256"])
        for pokemon_id in range(1, 1026):
            path = self.catalog.sprite(pokemon_id)
            with Image.open(path) as im:
                self.assertEqual(im.format, "PNG")
                im.verify()
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), manifest["files"][f"sprites/{pokemon_id:03}.png"])

    def test_page_ranges_cover_151_without_overlap(self):
        pages = [list(range((page - 1) * 30 + 1, min(page * 30, 151) + 1)) for page in range(1, 7)]
        self.assertEqual([i for page in pages for i in page], list(range(1, 152)))
        self.assertEqual(pages[-1], [151])

    def test_reference_profiles_complete_and_source_manifest_matches(self):
        self.assertEqual(set(self.catalog.profiles), set(range(1, 1026)))
        path = ROOT / "assets" / "profiles.json"
        manifest = json.loads((ROOT / "assets" / "profiles.sources.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["language_id"], 12)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), manifest["profiles_sha256"])
        for profile in self.catalog.profiles.values():
            self.assertTrue(profile["description"].strip())
            self.assertTrue(profile["category"].strip())
            self.assertGreater(profile["height_m"], 0)
            self.assertGreater(profile["weight_kg"], 0)
            if profile["description_version_id"] is None:
                self.assertTrue(profile["description_source"].startswith("https://www.pokemon.cn/play/pokedex/"))
                self.assertEqual(len(profile["source_sha256"]), 64)
            else:
                self.assertGreater(profile["description_version_id"], 0)
            self.assertIn(len(profile["types"]), (1, 2))
        self.assertEqual(self.catalog.profiles[1]["types"], ["草", "毒"])
        self.assertEqual(self.catalog.profiles[25]["types"], ["电"])

    def test_reference_cards_render_with_wrapped_description_and_missing_image(self):
        longest = max(self.catalog.profiles.values(), key=lambda p: len(p["description"]))
        for species in (1, 25, 122, 150, 151, 448, 906, 1000, 1025, longest["id"]):
            data = {"pokemon_id": species, **self.catalog.profiles[species]}
            path = self.renderer.render("profile", data)
            with Image.open(path) as im:
                self.assertEqual(im.width, 1000)
                self.assertGreaterEqual(im.height, 940)
            self.assertLess(path.stat().st_size, 4 * 1024 * 1024)
        data = {"pokemon_id": 25, **self.catalog.profiles[25]}
        with patch.object(self.catalog, "sprite", return_value=Path(self.temp.name) / "missing.png"):
            self.renderer.profile(data).load()

    def test_empty_partial_full_and_last_page_render(self):
        for page, owned in ((1, []), (2, [31, 32, 50]), (5, list(range(1, 152))), (6, [151])):
            data = {"name": "很长的中文训练家名称用于验证截断且不溢出" * 3,
                    "page": page, "collection": {str(i): {"count": 123456789} for i in owned}}
            image = self.renderer.render("dex", data)
            with Image.open(image) as im:
                self.assertEqual(im.width, 1200)
                self.assertLessEqual(im.height, 1800)
            self.assertLess(image.stat().st_size, 2 * 1024 * 1024)

    def test_missing_and_corrupt_sprite_use_placeholder(self):
        missing = Path(self.temp.name) / "missing.png"
        data = {"name": "小智", "pokemon_id": 25, "lines": ["尚未捕捉"]}
        with patch.object(self.catalog, "sprite", return_value=missing):
            self.renderer.card(data, "detail").load()
        missing.write_bytes(b"broken image")
        with patch.object(self.catalog, "sprite", return_value=missing):
            self.renderer.card(data, "detail").load()

    def test_cache_invalidates_after_collection_change(self):
        base = {"name": "小智", "page": 1, "collection": {}}
        empty = self.renderer.render("dex", base)
        self.assertEqual(empty, self.renderer.render("dex", base))
        captured = self.renderer.render("dex", {**base, "collection": {"25": {"count": 1}}})
        self.assertNotEqual(empty, captured)

    def test_national_and_generation_last_pages_and_summary_cards_render(self):
        from pokebot.dex import GENERATIONS, NATIONAL
        for g in [*GENERATIONS.values(), NATIONAL]:
            path = self.renderer.render("dex", {"name": "小智", "generation": g.number, "page": g.pages,
                                                "collection": {str(g.last): {"count": 1}}})
            with Image.open(path) as im:
                self.assertEqual(im.width, 1200)
                self.assertLessEqual(im.height, 1800)
        for repeated, legacy in ((False, False), (True, False), (True, True)):
            path = self.renderer.render("exploration", {"name": "小智", "day": "2026-10-15",
                "items": [{"id": i, "count": 1, "is_new": not repeated} for i in ([25] if legacy else range(1016, 1026))],
                "repeated": repeated, "legacy": legacy, "guaranteed": not legacy, "achievements": [] if repeated else ["全国图鉴大师"]})
            with Image.open(path) as im:
                self.assertEqual(im.width, 1200)
                self.assertLessEqual(im.height, 1200)
            self.assertLess(path.stat().st_size, 4 * 1024 * 1024)

    def test_cleanup_removes_old_files_and_caps_size(self):
        folder = Path(self.temp.name)
        old = folder / "old.png"
        old.write_bytes(b"old")
        stamp = time.time() - 8 * 86400
        os.utime(old, (stamp, stamp))
        keep = folder / "keep.png"
        keep.write_bytes(b"x" * 600000)
        newer = folder / "newer.png"
        newer.write_bytes(b"y" * 600000)
        os.utime(keep, (time.time() - 10, time.time() - 10))
        self.renderer.config = Config(cache_max_mb=1)
        self.renderer.cleanup()
        self.assertFalse(old.exists())
        self.assertFalse(keep.exists())
        self.assertTrue(newer.exists())

    def test_invalid_config_fails_early(self):
        for values in ({"pool_min": 0}, {"pool_max": 1026}, {"pool_min": 50, "pool_max": 1},
                       {"pool_min": True}, {"render_workers": 0}, {"draw_cooldown_seconds": float("nan")}):
            with self.assertRaises(ValueError):
                Config(**values)


if __name__ == "__main__":
    unittest.main()
