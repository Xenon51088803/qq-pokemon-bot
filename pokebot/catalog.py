import json
from pathlib import Path
from .dex import TOTAL_SPECIES, generation_of


class Catalog:
    def __init__(self, assets: Path):
        self.assets = assets
        path = assets / "pokemon.json"
        if not path.exists():
            raise FileNotFoundError("缺少资料 assets/pokemon.json，请先运行 scripts/prepare_assets.py")
        entries = json.loads(path.read_text(encoding="utf-8"))
        if len(entries) != TOTAL_SPECIES or [p["id"] for p in entries] != list(range(1, TOTAL_SPECIES + 1)):
            raise ValueError(f"宝可梦资料必须完整包含 1～{TOTAL_SPECIES}，请先运行更新资源.cmd")
        if any(not p.get("name") or p.get("generation") != generation_of(p["id"]) for p in entries):
            raise ValueError("中文名或世代资料不完整")
        self.entries = entries
        self.by_id = {p["id"]: p for p in entries}
        self.by_name = {p["name"]: p for p in entries}
        for p in entries:
            for alias in p.get("aliases", []):
                self.by_name[alias] = p
        self.profiles = {}
        profile_path = assets / "profiles.json"
        if profile_path.exists():
            profiles = json.loads(profile_path.read_text(encoding="utf-8"))
            if [p.get("id") for p in profiles] != list(range(1, TOTAL_SPECIES + 1)):
                raise ValueError(f"介绍资料必须完整包含 1～{TOTAL_SPECIES}，请先运行更新资源.cmd")
            for profile in profiles:
                if (not isinstance(profile.get("description"), str) or not profile["description"].strip()
                        or not profile.get("category") or not profile.get("types")
                        or profile.get("height_m", 0) <= 0 or profile.get("weight_kg", 0) <= 0):
                    raise ValueError("宝可梦介绍、分类、属性或体型资料不完整")
            self.profiles = {p["id"]: p for p in profiles}

    def lookup(self, name: str):
        return self.by_name.get(name.strip())

    def sprite(self, pokemon_id: int) -> Path:
        return self.assets / "sprites" / f"{pokemon_id:03}.png"
