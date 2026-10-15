import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .catalog import Catalog
from .config import Config
from .dex import TOTAL_SPECIES, scope

LOG = logging.getLogger(__name__)
BG = "#F2F5EF"
INK = "#203D36"
MUTED = "#6B7C74"
ACCENT = "#EAA351"
WHITE = "#FFFFFF"


def find_font(configured: str | None = None) -> str:
    candidates = [configured] if configured else []
    candidates += ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
                   "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
                   "/System/Library/Fonts/PingFang.ttc"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise FileNotFoundError("未找到中文字体，请在 config.local.json 中设置 font_path")


class Renderer:
    def __init__(self, catalog: Catalog, cache: Path, config: Config):
        self.catalog = catalog
        self.cache = cache
        self.config = config
        self.font_path = find_font(config.font_path)
        cache.mkdir(parents=True, exist_ok=True)

    def font(self, size):
        return ImageFont.truetype(self.font_path, size)

    def text(self, draw, xy, text, size=28, fill=INK, max_width=None):
        text = str(text).replace("\n", " ").replace("\r", " ")
        if max_width:
            while size > 18 and draw.textlength(text, font=self.font(size)) > max_width:
                size -= 1
            while text and draw.textlength(text, font=self.font(size)) > max_width:
                text = text[:-2] + "…" if len(text) > 2 else ""
        draw.text(xy, text, font=self.font(size), fill=fill)

    def sprite(self, canvas, pokemon_id, box, captured=True):
        x, y, w, h = box
        draw = ImageDraw.Draw(canvas)
        if captured:
            try:
                with Image.open(self.catalog.sprite(pokemon_id)) as source:
                    sprite = source.convert("RGBA")
                    bounds = sprite.getchannel("A").getbbox()
                    if bounds:
                        sprite = sprite.crop(bounds)
                    sprite = ImageOps.contain(sprite, (w, h), Image.Resampling.NEAREST)
                canvas.paste(sprite, (x + (w - sprite.width) // 2, y + (h - sprite.height) // 2), sprite)
                return
            except (OSError, ValueError):
                LOG.debug("Missing or invalid image for #%03d", pokemon_id)
        radius = min(w, h) // 3
        cx, cy = x + w // 2, y + h // 2
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill="#E1E6DF")
        self.text(draw, (cx - 13, cy - 27), "?", 44, MUTED)

    def render(self, kind: str, payload: dict) -> Path:
        # Include source state so a repaired sprite invalidates a placeholder cache.
        resource_state = [(p.name, p.stat().st_mtime_ns, p.stat().st_size)
                          for p in sorted((self.catalog.assets / "sprites").glob("*.png"))]
        key = hashlib.sha256(json.dumps([8, kind, payload, self.catalog.entries, resource_state, self.font_path],
                                       ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:32]
        target = self.cache / f"{kind}-{key}.png"
        if target.exists():
            os.utime(target, None)
            return target
        if kind == "dex":
            canvas = self.dex(payload)
        elif kind == "overview":
            canvas = self.overview(payload)
        elif kind == "profile":
            canvas = self.profile(payload)
        elif kind == "exploration":
            canvas = self.exploration(payload)
        elif kind in ("draw", "detail"):
            canvas = self.card(payload, kind)
        else:
            raise ValueError("未知图片类型")
        temporary = self.cache / f".{uuid.uuid4().hex}.tmp"
        try:
            canvas.save(temporary, format="PNG", optimize=True)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        return target

    def header(self, canvas, title, subtitle):
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((0, 0, canvas.width, 188), fill=INK)
        self.text(draw, (36, 22), "POKÉMON  /  NATIONAL 1025", 21, ACCENT)
        self.text(draw, (34, 57), title, 49, WHITE, canvas.width - 70)
        self.text(draw, (36, 129), subtitle, 25, "#CFDCD3", canvas.width - 72)
        return draw

    def dex(self, data):
        page = data["page"]
        generation = data.get("generation", 1)
        selected = scope(generation)
        ids = list(selected.ids(page))
        rows = (len(ids) + 4) // 5
        canvas = Image.new("RGB", (1200, 236 + rows * 224 + 80), BG)
        caught = data["collection"]
        total = sum(str(i) in caught for i in selected.ids())
        title = "第一世代图鉴" if generation == 1 else "全国图鉴" if generation == 0 else f"第 {generation} 世代 · {selected.name}"
        draw = self.header(canvas, f"{title} · 第 {page} / {selected.pages} 页",
                           f"{data['name']}  ·  已收集 {total}/{selected.count}  ·  {total / selected.count:.1%}")
        self.text(draw, (34, 202), f"本页 #{ids[0]:03}—#{ids[-1]:03}   /   彩色为已捕捉，灰色为待发现", 22, MUTED)
        for index, pokemon_id in enumerate(ids):
            x = 30 + index % 5 * 234
            y = 246 + index // 5 * 224
            entry = self.catalog.by_id[pokemon_id]
            owned = caught.get(str(pokemon_id))
            draw.rounded_rectangle((x, y, x + 216, y + 208), radius=16, fill=WHITE if owned else "#E9EDE6")
            self.text(draw, (x + 14, y + 10), f"#{pokemon_id:03}", 22, MUTED)
            self.sprite(canvas, pokemon_id, (x + 36, y + 40, 144, 105), bool(owned))
            self.text(draw, (x + 14, y + 149), entry["name"] if owned else "尚未捕捉", 27,
                      INK if owned else MUTED, 188)
            self.text(draw, (x + 14, y + 184), f"捕捉 {owned['count']} 次" if owned else "等待下一次相遇", 17, MUTED)
        command = "全国" if generation == 0 else str(generation)
        footer = f"下一页：@机器人 图鉴 {command} {page + 1}" if page < selected.pages else "这是最后一页 · @机器人 我的收集 查看统计"
        self.text(draw, (34, canvas.height - 49), footer, 24, MUTED)
        return canvas

    def card(self, data, kind):
        canvas = Image.new("RGB", (960, 800), BG)
        p = self.catalog.by_id[data["pokemon_id"]]
        draw = self.header(canvas, f"#{p['id']:03}  {p['name']}",
                           f"{data['name']}  ·  " + (data["day"] if kind == "draw" else f"第 {p['generation']} 世代 · 个人捕捉详情"))
        draw.rounded_rectangle((35, 213, 925, 558), radius=24, fill=WHITE)
        self.sprite(canvas, p["id"], (300, 221, 360, 310), True)
        for index, line in enumerate(data["lines"][:4]):
            self.text(draw, (44, 584 + 43 * index), line, 30 if index == 0 else 25, INK if index == 0 else MUTED, 870)
        if data.get("achievements"):
            draw.rounded_rectangle((35, 684, 925, 738), radius=12, fill="#FAE2B9")
            self.text(draw, (51, 694), "成就解锁 · " + " / ".join(data["achievements"]), 27, INK, 854)
        self.text(draw, (44, 758), "每日探索 · 允许重复 · 全国图鉴 1025 种", 21, MUTED)
        return canvas

    def profile(self, data):
        p = self.catalog.by_id[data["pokemon_id"]]
        font = self.font(28)
        lines, line = [], ""
        # Wrap Chinese at character boundaries without shortening the game text.
        for char in data["description"]:
            if line and font.getlength(line + char) > 904:
                lines.append(line)
                line = ""
            line += char
        if line:
            lines.append(line)
        canvas = Image.new("RGB", (1000, max(940, 806 + len(lines) * 42)), BG)
        draw = self.header(canvas, f"#{p['id']:03}  {p['name']}",
                           f"{p['english_name']}  ·  第 {p['generation']} 世代资料图鉴")
        draw.rounded_rectangle((35, 213, 965, 558), radius=24, fill=WHITE)
        self.sprite(canvas, p["id"], (320, 221, 360, 310), True)
        self.text(draw, (44, 584), f"{data['category']}  ·  {' / '.join(data['types'])}", 30, INK, 912)
        self.text(draw, (44, 632), f"身高 {data['height_m']:.1f} 米   ·   体重 {data['weight_kg']:.1f} 千克", 27, MUTED, 912)
        self.text(draw, (44, 686), "图鉴介绍", 25, MUTED)
        for index, description_line in enumerate(lines):
            self.text(draw, (44, 729 + index * 42), description_line, 28)
        self.text(draw, (44, canvas.height - 48), "资料查询 · 不计入收藏 · 不消耗每日探索", 22, MUTED)
        return canvas

    def exploration(self, data):
        items = data["items"]
        rows = (len(items) + 4) // 5
        canvas = Image.new("RGB", (1200, 390 + rows * 240 + (60 if data.get("achievements") else 0)), BG)
        title = "升级补抽十连" if data.get("upgrade_bonus") else "今日探索"
        draw = self.header(canvas, title + (" · 已保存的结果" if data["repeated"] else " · 新的相遇"),
                           f"{data['name']}  ·  {data['day']}（北京时间）")
        state = "今天已探索，图鉴无新增，捕捉次数不增加" if data["repeated"] else f"捕捉 {len(items)} 只 · 新发现 {sum(p['is_new'] for p in items)} 种"
        self.text(draw, (34, 205), state, 28, INK, 1130)
        self.text(draw, (34, 247), "每群独立 · 同日结果固定 · 标记为新发现的宝可梦已加入收藏", 22, MUTED, 1130)
        for index, item in enumerate(items):
            x, y = 30 + index % 5 * 234, 294 + index // 5 * 240
            draw.rounded_rectangle((x, y, x + 216, y + 222), radius=16, fill="#FAE2B9" if item["is_new"] else WHITE)
            self.text(draw, (x + 14, y + 10), f"#{item['id']:03}" + (" · 新发现" if item["is_new"] else ""), 21, INK, 190)
            self.sprite(canvas, item["id"], (x + 36, y + 45, 144, 112), True)
            self.text(draw, (x + 14, y + 163), self.catalog.by_id[item["id"]]["name"], 26, INK, 188)
            self.text(draw, (x + 14, y + 196), f"当次累计捕捉 {item['count']} 次", 17, MUTED, 188)
        if data.get("achievements"):
            self.text(draw, (34, 310 + rows * 240), "成就解锁：" + " / ".join(data["achievements"]), 26, INK, 1130)
        footer = ("升级补抽仅限一次 · 原单抽记录保留 · 额外十只已计入收藏" if data.get("upgrade_bonus") else
                  "升级前单只结果保留" if data.get("legacy") else
                  "当次探索启用未收集保底" if data["guaranteed"] else "每日探索 10 只 · 整批重复且抽池未集齐时保证一种新发现")
        self.text(draw, (34, canvas.height - 48), footer, 22, MUTED, 1130)
        return canvas

    def overview(self, data):
        canvas = Image.new("RGB", (1000, 1100), BG)
        count = data["count"]
        draw = self.header(canvas, "我的全国图鉴", data["name"] + "  ·  全国与各世代收集总览")
        draw.rounded_rectangle((36, 224, 964, 480), radius=24, fill=WHITE)
        self.text(draw, (65, 245), f"{count} / {TOTAL_SPECIES}", 76, INK)
        self.text(draw, (67, 345), f"收集完成度 {count / TOTAL_SPECIES:.1%}   ·   累计捕捉 {data['total']} 次", 29, MUTED, 860)
        draw.rounded_rectangle((65, 422, 935, 452), radius=15, fill="#E7ECE4")
        if count:
            draw.rounded_rectangle((65, 422, 65 + max(30, int(870 * count / TOTAL_SPECIES)), 452), radius=15, fill=ACCENT)
        for index, g in enumerate(data.get("progress", [])):
            self.text(draw, (44, 510 + index * 52), f"第 {g['generation']} 世代 · {g['name']}", 26, INK, 520)
            self.text(draw, (600, 510 + index * 52), f"{g['count']}/{g['total']}  ·  {g['count'] / g['total']:.1%}", 26, MUTED, 350)
        self.text(draw, (42, 1002), "按世代查看：@机器人 图鉴 1 1", 27)
        self.text(draw, (42, 1050), "全国分页：@机器人 图鉴 全国 1（共 35 页）", 24, MUTED)
        return canvas

    def cleanup(self):
        cutoff = time.time() - self.config.cache_days * 86400
        files = []
        for path in self.cache.glob("*.png"):
            try:
                stat = path.stat()
                if stat.st_mtime < cutoff:
                    path.unlink(missing_ok=True)
                else:
                    files.append((stat.st_mtime, stat.st_size, path))
            except FileNotFoundError:
                pass
        total = sum(size for _, size, _ in files)
        for _, size, path in sorted(files):
            if total <= self.config.cache_max_mb * 1024 * 1024:
                break
            path.unlink(missing_ok=True)
            total -= size
