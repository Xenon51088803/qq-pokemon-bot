import json
from dataclasses import dataclass, fields
from pathlib import Path
from .dex import TOTAL_SPECIES

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    pool_min: int = 1
    pool_max: int = TOTAL_SPECIES
    draw_cooldown_seconds: float = 3
    dex_cooldown_seconds: float = 15
    group_images_per_minute: int = 10
    cache_max_mb: int = 500
    cache_days: int = 7
    render_workers: int = 2
    render_queue_limit: int = 16
    font_path: str | None = None

    def __post_init__(self):
        integers = ("pool_min", "pool_max", "group_images_per_minute", "cache_max_mb",
                    "cache_days", "render_workers", "render_queue_limit")
        for name in integers:
            if type(getattr(self, name)) is not int:
                raise ValueError(f"{name} 必须是整数")
        if not 1 <= self.pool_min <= self.pool_max <= TOTAL_SPECIES:
            raise ValueError(f"抽取范围必须满足 1 ≤ pool_min ≤ pool_max ≤ {TOTAL_SPECIES}")
        for name in ("draw_cooldown_seconds", "dex_cooldown_seconds"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not 0 <= value <= 86400:
                raise ValueError(f"{name} 必须为 0～86400 的数字")
        for name in integers[2:]:
            if getattr(self, name) < 1:
                raise ValueError(f"{name} 必须大于零")
        if self.render_workers > 8 or self.render_queue_limit > 128:
            raise ValueError("图片工作线程最多 8 个，排队上限最多 128 个")
        if self.font_path is not None and not Path(self.font_path).is_file():
            raise ValueError("font_path 指定的字体不存在")

    @classmethod
    def load(cls, path: Path | None = None):
        path = path or ROOT / "config.local.json"
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("配置必须是 JSON 对象")
        unknown = set(data) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"未知配置字段：{', '.join(sorted(unknown))}")
        return cls(**data)
