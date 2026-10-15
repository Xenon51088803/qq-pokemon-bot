"""National species scope; alternate forms share their national species number."""
from dataclasses import dataclass

TOTAL_SPECIES = 1025
PAGE_SIZE = 30


@dataclass(frozen=True)
class Generation:
    number: int
    name: str
    first: int
    last: int

    @property
    def count(self):
        return self.last - self.first + 1

    @property
    def pages(self):
        return (self.count + PAGE_SIZE - 1) // PAGE_SIZE

    def ids(self, page=None):
        if page is None:
            return range(self.first, self.last + 1)
        if not 1 <= page <= self.pages:
            raise ValueError(f"有效页码为 1～{self.pages}")
        start = self.first + (page - 1) * PAGE_SIZE
        return range(start, min(start + PAGE_SIZE, self.last + 1))


GENERATIONS = {
    1: Generation(1, "关都", 1, 151),
    2: Generation(2, "城都", 152, 251),
    3: Generation(3, "丰缘", 252, 386),
    4: Generation(4, "神奥", 387, 493),
    5: Generation(5, "合众", 494, 649),
    6: Generation(6, "卡洛斯", 650, 721),
    7: Generation(7, "阿罗拉", 722, 809),
    8: Generation(8, "伽勒尔／洗翠", 810, 905),
    9: Generation(9, "帕底亚", 906, 1025),
}
NATIONAL = Generation(0, "全国", 1, TOTAL_SPECIES)


def scope(number):
    return NATIONAL if number == 0 else GENERATIONS[number]


def generation_of(species):
    for generation in GENERATIONS.values():
        if generation.first <= species <= generation.last:
            return generation.number
    raise ValueError("编号超出全国图鉴范围")
