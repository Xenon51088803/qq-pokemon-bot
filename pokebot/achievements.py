"""Collection-only milestones. Dates derive from the original capture records."""
from dataclasses import dataclass
from .dex import GENERATIONS, TOTAL_SPECIES


@dataclass(frozen=True)
class Achievement:
    id: str
    title: str
    description: str
    target: int
    species: frozenset[int] = frozenset()


ACHIEVEMENTS = (
    Achievement("first_capture", "初次相遇", "首次捕捉一只宝可梦", 1),
    Achievement("species_10", "小有收藏", "收集 10 种不同的宝可梦", 10),
    Achievement("species_50", "收藏达人", "收集 50 种不同的宝可梦", 50),
    Achievement("species_100", "关都专家", "收集 100 种不同的第一世代宝可梦", 100, frozenset(range(1, 152))),
    Achievement("species_151", "关都大师", "集齐第一世代 151 种宝可梦", 151, frozenset(range(1, 152))),
    Achievement("kanto_starters", "御三家集结", "集齐妙蛙种子、小火龙和杰尼龟", 3, frozenset((1, 4, 7))),
) + tuple(Achievement(f"generation_{g.number}", f"{g.name}大师", f"集齐第 {g.number} 世代 {g.count} 种宝可梦",
                      g.count, frozenset(g.ids())) for g in GENERATIONS.values() if g.number != 1) + (
    Achievement("national_master", "全国图鉴大师", f"集齐全国 {TOTAL_SPECIES} 种宝可梦", TOTAL_SPECIES),
)
BY_ID = {a.id: a for a in ACHIEVEMENTS}


def evaluate(captures):
    """Return (definition, capped progress, historical unlock day or None)."""
    first_days = {r["pokemon_id"]: r["first_day"] for r in captures}
    results = []
    for achievement in ACHIEVEMENTS:
        days = sorted(day for species, day in first_days.items()
                      if not achievement.species or species in achievement.species)
        reached = days[achievement.target - 1] if len(days) >= achievement.target else None
        results.append((achievement, min(len(days), achievement.target), reached))
    return results
