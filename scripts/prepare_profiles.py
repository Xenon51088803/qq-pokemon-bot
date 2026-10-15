"""Prepare an offline reference dex from PokeAPI's Chinese game data."""
import argparse
import csv
import hashlib
import io
import json
import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from prepare_assets import fetch_table, fetch
from pokebot.dex import TOTAL_SPECIES

ROOT = Path(__file__).resolve().parent.parent
TABLES = ("pokemon", "pokemon_types", "type_names", "pokemon_species_names", "pokemon_species_flavor_text")


def official_profile(species, refresh=False):
    url = f"https://www.pokemon.cn/play/pokedex/{species:04}"
    target = ROOT / "runtime" / "resource-cache" / "official-profiles" / f"{species:04}.html"
    if target.exists() and not refresh:
        raw = target.read_bytes()
    else:
        raw = fetch(url, attempts=2, timeout=25)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    page = raw.decode("utf-8")
    story = re.search(r'<p class="pokemon-story__body[^"\n]*"[^>]*>(.*?)</p>', page, re.S)
    category = re.search(r'pokemon-info__category.*?pokemon-info__value[^>]*>(.*?)</div>', page, re.S)
    clean = lambda value: "".join(html.unescape(re.sub(r"<[^>]+>", "", value)).split())
    if not story or not category or not clean(story[1]):
        raise ValueError(f"官网 #{species} 中文介绍解析失败，未覆盖现有资料")
    return {"flavor_text": clean(story[1]), "version_id": None, "source": url,
            "category": clean(category[1]), "source_sha256": hashlib.sha256(raw).hexdigest()}


def build_profiles(tables, refresh=False):
    names = {int(r["pokemon_species_id"]): r for r in tables["pokemon_species_names"]
             if r["local_language_id"] == "12" and 1 <= int(r["pokemon_species_id"]) <= TOTAL_SPECIES}
    types = {int(r["type_id"]): r["name"] for r in tables["type_names"] if r["local_language_id"] == "12"}
    slots = {}
    for row in tables["pokemon_types"]:
        species = int(row["pokemon_id"])
        if 1 <= species <= TOTAL_SPECIES:
            slots.setdefault(species, []).append((int(row["slot"]), types[int(row["type_id"])]))
    sizes = {int(r["id"]): r for r in tables["pokemon"] if 1 <= int(r["id"]) <= TOTAL_SPECIES}
    descriptions = {}
    for row in tables["pokemon_species_flavor_text"]:
        species = int(row["species_id"])
        if row["language_id"] == "12" and 1 <= species <= TOTAL_SPECIES:
            # Use the most recent available game's entry, with provenance per species.
            if species not in descriptions or int(row["version_id"]) > int(descriptions[species]["version_id"]):
                descriptions[species] = row
    profiles = []
    missing = [i for i in range(1, TOTAL_SPECIES + 1) if i not in descriptions or not names[i]["genus"]]
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(official_profile, i, refresh): i for i in missing}
        for index, future in enumerate(as_completed(futures), 1):
            species = futures[future]
            descriptions[species] = future.result()
            if index % 50 == 0 or index == len(missing):
                print(f"补齐官网中文介绍：{index}/{len(missing)}", flush=True)
    for species in range(1, TOTAL_SPECIES + 1):
        row = descriptions[species]
        profiles.append({
            "id": species, "category": row.get("category", names[species]["genus"]),
            "types": [name for _, name in sorted(slots[species])],
            "height_m": int(sizes[species]["height"]) / 10,
            "weight_kg": int(sizes[species]["weight"]) / 10,
            "description": "".join(row["flavor_text"].split()),
            "description_version_id": int(row["version_id"]) if row["version_id"] is not None else None,
            "description_source": row.get("source", "https://github.com/PokeAPI/pokeapi/blob/master/data/v2/csv/pokemon_species_flavor_text.csv"),
            **({"source_sha256": row["source_sha256"]} if "source_sha256" in row else {}),
        })
    if any(not p["description"] or not p["category"] or not p["types"] for p in profiles):
        raise ValueError("上游中文图鉴不完整，未覆盖本地资料")
    return profiles


def prepare_profiles(direct=False, refresh=False):
    tables, sources = {}, {}
    for name in TABLES:
        print(f"正在整理图鉴资料：{name}", flush=True)
        raw, url = fetch_table(name, direct, refresh)
        tables[name] = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")))
        sources[name] = {"url": url, "sha256": hashlib.sha256(raw).hexdigest()}
    profiles = build_profiles(tables, refresh)
    assets = ROOT / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(profiles, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    target = assets / "profiles.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.replace(target)
    manifest = {
        "prepared_at": datetime.now(timezone.utc).isoformat(), "sources": sources,
        "profiles_sha256": hashlib.sha256(data).hexdigest(), "language_id": 12,
        "description_selection": "Chinese language 12, highest available version_id; missing entries use Chinese official Pokedex default-form page, first description. Source URL recorded per species.",
        "type_basis": "Current default form types, including later type changes such as Fairy.",
        "notice": "Pokemon data and game text belong to their respective rights holders.",
    }
    (assets / "profiles.sources.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已准备全国 {TOTAL_SPECIES} 只中文介绍、分类、属性、身高和体重。", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--direct", action="store_true")
    prepare_profiles(parser.parse_args().direct)
