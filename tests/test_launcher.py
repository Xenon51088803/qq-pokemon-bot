import os
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pokebot.config import ROOT


@unittest.skipUnless(os.name == "nt", "Windows batch launcher")
class LauncherTests(unittest.TestCase):
    def test_actual_batch_launcher_and_interactive_commands(self):
        commands = ("/状态\n@机器人 帮助\n@机器人 皮卡丘\n@机器人 图鉴 1 1\n/按钮 1\n@机器人 成就\n"
                    "/群 启动器验收群\n/按钮 1\n@机器人 图鉴 1 6\n"
                    "/成员 启动器验收员\n/按钮 1\n/状态\n/退出\n\n")
        test_root = ROOT / "runtime" / "tests"
        test_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_root) as folder:
            clone = Path(folder)
            shutil.copytree(ROOT / "pokebot", clone / "pokebot", ignore=shutil.ignore_patterns("__pycache__"))
            (clone / "scripts").mkdir()
            (clone / "assets").mkdir()
            shutil.copy2(ROOT / "scripts" / "start.ps1", clone / "scripts" / "start.ps1")
            shutil.copy2(ROOT / "assets" / "pokemon.json", clone / "assets" / "pokemon.json")
            shutil.copy2(ROOT / "assets" / "profiles.json", clone / "assets" / "profiles.json")
            shutil.copy2(ROOT / "启动模拟器.cmd", clone / "启动模拟器.cmd")
            (clone / "config.local.json").write_text(json.dumps({"dex_cooldown_seconds": 0, "draw_cooldown_seconds": 0}), encoding="utf-8")
            result = subprocess.run(
                ["cmd.exe", "/d", "/c", str(clone / "启动模拟器.cmd")],
                input=commands, capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=clone, timeout=30,
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("本地模拟器", result.stdout)
        self.assertIn("启动器验收群", result.stdout)
        self.assertIn("启动器验收员", result.stdout)
        self.assertIn("仅本地模拟，尚未连接 QQ", result.stdout)
        self.assertIn("图鉴", result.stdout)
        self.assertIn("分类：鼠宝可梦", result.stdout)
        self.assertIn("属性：电", result.stdout)
        self.assertIn("第 2/6 页", result.stdout)
        self.assertIn("[1] 下一页", result.stdout)
        self.assertIn("[1] 上一页", result.stdout)
        self.assertIn("已解锁 0/15 项", result.stdout)
        self.assertEqual(result.stdout.count("按钮编号无效"), 2)
        self.assertNotIn("今日宝可梦是", result.stdout)
        self.assertNotIn("启动或操作失败", result.stdout)


if __name__ == "__main__":
    unittest.main()
