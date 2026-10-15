"""Prepare default-form species #001–#1025 and shared sprites; safe to rerun."""
import argparse
import csv
import hashlib
import io
import json
import re
import sys
import time
import unicodedata
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from pokebot.dex import TOTAL_SPECIES, generation_of
NAMES_URL = "https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv/pokemon_species_names.csv"
SPRITE_URL = "https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/{id}.png"
NAMES_MIRROR = "https://fastly.jsdelivr.net/gh/PokeAPI/pokeapi@master/data/v2/csv/pokemon_species_names.csv"
SPRITE_MIRROR = "https://fastly.jsdelivr.net/gh/PokeAPI/sprites@master/sprites/pokemon/{id}.png"
SHOWDOWN_URL = "https://play.pokemonshowdown.com/sprites/gen5/{slug}.png"
OFFICIAL_INDEX = "https://www.pokemon.cn/play/pokedex/api/v1"


def fetch(url, attempts=3, timeout=15):
    error = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "PokemonLocalBot/0.1 (resource preparation)"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except Exception as exc:
            error = exc
            if attempt < attempts - 1:
                time.sleep(attempt + 1)
    raise RuntimeError(f"下载失败：{url}: {error}")


def fetch_table(name, direct=False, refresh=False):
    cache = ROOT / "runtime" / "resource-cache"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (name + ".csv")
    base = ("https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv/" if direct else
            "https://fastly.jsdelivr.net/gh/PokeAPI/pokeapi@master/data/v2/csv/")
    url = base + name + ".csv"
    if target.exists() and not refresh:
        return target.read_bytes(), url
    raw = fetch(url, timeout=90)
    temp = target.with_suffix(".tmp")
    temp.write_bytes(raw)
    temp.replace(target)
    return raw, url


def official_names(refresh=False):
    cache = ROOT / "runtime" / "resource-cache" / "official-pokedex.json"
    if not cache.exists() or refresh:
        raw = fetch(OFFICIAL_INDEX, attempts=2, timeout=30)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(raw)
    data = json.loads(cache.read_text(encoding="utf-8"))
    names = {int(p["zukan_id"]): p["pokemon_name"] for p in data["pokemons"] if p["zukan_sub_id"] == 0}
    if not set(range(1, TOTAL_SPECIES + 1)) <= names.keys():
        raise ValueError("官网名称未覆盖全国图鉴，未覆盖本地资料")
    return names


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-names", action="store_true")
    parser.add_argument("--refresh-profiles", action="store_true", help="重新下载中文介绍及属性资料")
    parser.add_argument("--direct", action="store_true", help="直接使用 GitHub 源站；默认中文资料用镜像、图片使用 Pokemon Showdown")
    args = parser.parse_args()
    assets = ROOT / "assets"
    sprites = assets / "sprites"
    sprites.mkdir(parents=True, exist_ok=True)
    catalog = assets / "pokemon.json"
    existing = json.loads(catalog.read_text(encoding="utf-8")) if catalog.exists() else []
    raw, _ = fetch_table("pokemon_species_names", args.direct, args.refresh_names)
    rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    names = {int(r["pokemon_species_id"]): r["name"] for r in rows
             if r["local_language_id"] == "12" and 1 <= int(r["pokemon_species_id"]) <= TOTAL_SPECIES}
    if set(names) != set(range(1, TOTAL_SPECIES + 1)):
        raise RuntimeError("上游简体中文名称不完整，未覆盖本地资料")
    english = {int(r["pokemon_species_id"]): r["name"] for r in rows
               if r["local_language_id"] == "9" and 1 <= int(r["pokemon_species_id"]) <= TOTAL_SPECIES}
    authoritative = official_names(args.refresh_names)
    old_by_id = {p["id"]: p for p in existing}
    entries = [{"id": i, "name": names[i], "english_name": english[i], "generation": generation_of(i)}
               for i in range(1, TOTAL_SPECIES + 1)]
    for p in entries:
        p["name"] = authoritative[p["id"]]
        aliases = set(old_by_id.get(p["id"], {}).get("aliases", []))
        aliases.update((names[p["id"]], old_by_id.get(p["id"], {}).get("name", p["name"])))
        if p["id"] in (29, 32):
            aliases.add("尼多兰" if p["id"] == 29 else "尼多朗")
        aliases.discard(p["name"])
        if aliases:
            p["aliases"] = sorted(aliases)
    temp = catalog.with_suffix(".tmp")
    temp.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(catalog)
    entries = json.loads(catalog.read_text(encoding="utf-8"))
    english = {p["id"]: p["english_name"] for p in entries}
    previous_path = assets / "sources.json"
    previous = json.loads(previous_path.read_text(encoding="utf-8")) if previous_path.exists() else {}
    source_by_file = previous.get("source_by_file", {})

    def prepare(pokemon_id):
        target = sprites / f"{pokemon_id:03}.png"
        ascii_name = unicodedata.normalize("NFKD", english[pokemon_id]).encode("ascii", "ignore").decode().lower()
        slug = {29: "nidoranf", 32: "nidoranm"}.get(pokemon_id, re.sub("[^a-z0-9]", "", ascii_name))
        if target.exists():
            try:
                with Image.open(target) as im:
                    im.verify()
                source = source_by_file.get(f"sprites/{pokemon_id:03}.png")
                if not source:
                    source = previous.get("download_images_url", SPRITE_MIRROR).format(id=pokemon_id, slug=slug)
                return pokemon_id, source
            except Exception:
                pass
        url = SPRITE_URL.format(id=pokemon_id) if args.direct else SHOWDOWN_URL.format(slug=slug)
        data = fetch(url, attempts=2, timeout=25)
        with Image.open(io.BytesIO(data)) as im:
            if im.format != "PNG":
                raise ValueError("素材不是 PNG")
            im.verify()
        temp = target.with_suffix(".tmp")
        temp.write_bytes(data)
        temp.replace(target)
        return pokemon_id, url

    failures = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(prepare, i): i for i in range(1, TOTAL_SPECIES + 1)}
        for index, future in enumerate(as_completed(futures), 1):
            try:
                pokemon_id, source = future.result()
                source_by_file[f"sprites/{pokemon_id:03}.png"] = source
            except Exception as exc:
                failures.append({"id": futures[future], "error": str(exc)})
                print(f"暂缺 #{futures[future]:03}: {exc}", flush=True)
            if index % 50 == 0 or index == TOTAL_SPECIES:
                print(f"资源检查：{index}/{TOTAL_SPECIES}", flush=True)
    manifest = {
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "names_source": NAMES_URL,
        "official_names_source": OFFICIAL_INDEX,
        "images_source": SPRITE_URL,
        "download_names_url": NAMES_URL if args.direct else NAMES_MIRROR,
        "download_images_url": SPRITE_URL if args.direct else SHOWDOWN_URL,
        "source_by_file": source_by_file,
        "notice": "Pokemon names and artwork belong to their respective owners. Public availability is not a license to reuse the artwork.",
        "files": {str(p.relative_to(assets)).replace('\\', '/'): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in sorted(assets.rglob('*.png'))},
        "names_sha256": hashlib.sha256(catalog.read_bytes()).hexdigest(),
        "failures": failures,
    }
    (assets / "sources.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"完成：{TOTAL_SPECIES - len(failures)}/{TOTAL_SPECIES} 张图片；资料：{catalog}")
    profile_path = assets / "profiles.json"
    if args.refresh_profiles or not profile_path.exists() or len(json.loads(profile_path.read_text(encoding="utf-8"))) != TOTAL_SPECIES:
        from prepare_profiles import prepare_profiles
        prepare_profiles(direct=args.direct, refresh=args.refresh_profiles)
    if failures:
        print("缺失图片运行时使用占位；再次执行本脚本可补齐。")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"资源整理失败：{exc}", file=sys.stderr)
        sys.exit(1)
