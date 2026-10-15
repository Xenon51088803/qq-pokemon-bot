"""Generate labeled UI samples without changing any trainer's collection."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pokebot.catalog import Catalog
from pokebot.config import Config
from pokebot.render import Renderer
from pokebot.dex import GENERATIONS


def main():
    renderer = Renderer(Catalog(ROOT / "assets"), ROOT / "runtime" / "previews", Config())
    collection = {str(i): {"count": i % 4 + 1} for i in (1, 4, 7, 10, 12, 16, 19, 21, 25, 26, 29, 906, 1000, 1025)}
    progress = [{"generation": g.number, "name": g.name, "count": sum(str(i) in collection for i in g.ids()),
                 "total": g.count} for g in GENERATIONS.values()]
    batch = {"name": "小智（展示样例）", "day": "2026-10-15", "repeated": False, "guaranteed": True,
             "legacy": False, "items": [{"id": i, "count": 1 if i == 1025 else 3, "is_new": i == 1025}
                                        for i in (1, 25, 152, 252, 448, 658, 722, 810, 906, 1025)]}
    samples = {
        "图鉴示例": ("dex", {"name": "小智（展示样例）", "page": 1, "collection": collection}),
        "末页示例": ("dex", {"name": "小智（展示样例）", "page": 6, "collection": {"151": {"count": 1}}}),
        "全国末页示例": ("dex", {"name": "小智（展示样例）", "generation": 0, "page": 35, "collection": collection}),
        "第九世代示例": ("dex", {"name": "小智（展示样例）", "generation": 9, "page": 1, "collection": collection}),
        "探索示例": ("exploration", batch),
        "升级补抽示例": ("exploration", {**batch, "upgrade_bonus": True}),
        "成就解锁示例": ("exploration", {**batch, "achievements": ["全国图鉴大师"]}),
        "旧版当日结果示例": ("exploration", {**batch, "repeated": True, "guaranteed": False, "legacy": True,
                                              "items": [{"id": 25, "count": 1, "is_new": False}]}),
        "总览示例": ("overview", {"name": "小智（展示样例）", "count": len(collection), "total": 30, "progress": progress}),
        "资料图鉴示例": ("profile", {"pokemon_id": 1025, **renderer.catalog.profiles[1025]}),
    }
    for name, (kind, data) in samples.items():
        generated = renderer.render(kind, data)
        target = renderer.cache / f"{name}.png"
        target.write_bytes(generated.read_bytes())
        print(target)


if __name__ == "__main__":
    main()
