"""Draw a plan's steps and a pointing grid on a battle frame, for a person to look at.

Pure: a frame and what to mark go in, a PNG comes out. Where a plan lands is
`ui.attack.planned_drops`'s and where the grid sits `parsers.boundary`'s; this
only paints them, with labels in Traditional Chinese since the picture is for
the user. Nothing is painted over the card row, which the picture is read
against, and the notes go on a strip added under the frame.
"""

from __future__ import annotations

import io
import math
from typing import TYPE_CHECKING
import logging
import itertools

from PIL import Image, ImageDraw, ImageFont

from ai_coc.parsers.frame import open_frame
from ai_coc.parsers.boundary import MAP_SLOPE, PLAYFIELD

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ai_coc.models import CardRow, BaseGrid, HeroKind, GridCorner, PlannedDrop

logger = logging.getLogger(__name__)

# Where the card row's strip begins, which is where `boundary.UI_PANELS` end.
CARD_ROW_TOP = 712
# A font with Traditional Chinese glyphs, by file name so Pillow looks in the
# system's own font folders: Windows, macOS, then the two common on Linux.
FONTS = ("msjhbd.ttc", "msjh.ttc", "PingFang.ttc", "NotoSansCJK-Bold.ttc", "wqy-zenhei.ttc")

# One tile's edge on the home village at the far zoom, in pixels across.
# Neighbouring parallel runs of the stroke sit 19.2 px apart along
# `y - 0.75x` (gaps of 19, 59, 96 and 19 on `battle_flat_reference.png`,
# which are 1, 3, 5 and 1 tiles), and that distance is 1.5 edges.
TILE = 12.8
# A spell's radius in tiles, from the game's own `spells.csv` (`Radius` 500 and
# 350, game version 19.32.4).
SPELL_TILES = {"rage": 5.0, "freeze": 3.5}

Colour = tuple[int, int, int]
WHITE: Colour = (255, 255, 255)
SIDE_COLOUR: Colour = (255, 220, 60)
CORNER_COLOUR: Colour = (90, 230, 255)
COLOURS: dict[str, Colour] = {
    "troops": (60, 140, 255),
    "hero": (255, 210, 60),
    "siege": (255, 80, 80),
    "machine": (255, 80, 80),
    "rage": (200, 60, 255),
    "freeze": (90, 220, 255),
}
ACT_NAMES = {
    "troops": "部隊",
    "hero": "英雄",
    "ability": "開技能",
    "siege": "攻城機具",
    "machine": "戰爭機器",
    "rage": "狂暴",
    "freeze": "冰凍",
}
HERO_NAMES: dict[HeroKind, str] = {
    "king": "野蠻人之王",
    "queen": "弓箭女皇",
    "warden": "大守護者",
    "champion": "神盾勇者",
    "minion_prince": "亡靈王子",
    "duke": "飛龍公爵",
    "unknown": "其他英雄",
}
# Each side by the two corners it joins, and which way its label is moved off it.
SIDES: tuple[tuple[str, GridCorner, GridCorner, tuple[int, int]], ...] = (
    ("A 左上邊", "top", "left", (-1, -1)),
    ("B 右上邊", "top", "right", (1, -1)),
    ("C 左下邊", "left", "bottom", (-1, 1)),
    ("D 右下邊", "right", "bottom", (1, 1)),
)
CORNERS: tuple[tuple[str, GridCorner, tuple[int, int]], ...] = (
    ("上角", "top", (0, 28)),
    ("右角", "right", (62, 0)),
    ("下角", "bottom", (0, -30)),
    ("左角", "left", (-62, 0)),
)
# What marks the plan another is held against (`--against`).
GHOST = "對照"
TIMES = "\u00d7"
NOTE_LINE = 34
NOTE_PAD = 14


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in FONTS:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    logger.warning("No font with Chinese glyphs among %s; the labels will not read", FONTS)
    return ImageFont.load_default(size)


def _tag(  # noqa: PLR0913 - one label: where, what, how it looks, how low it may sit
    draw: ImageDraw.ImageDraw,
    point: tuple[float, float],
    text: str,
    colour: Colour,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    *,
    lowest: int = PLAYFIELD[3] - 3,
) -> None:
    """A label on a dark plate, centred on `point` and kept on the playfield.

    A grid's lower corner lies under the card row, so its labels are lifted
    onto the ground above it, each kind to a height of its own (`lowest`) so
    they do not land on one another.
    """
    left, top, right, _bottom = PLAYFIELD
    at = (min(max(point[0], left + 50), right - 50), min(max(point[1], top + 16), lowest))
    box = draw.textbbox(at, text, font=font, anchor="mm")
    draw.rectangle((box[0] - 5, box[1] - 3, box[2] + 5, box[3] + 3), fill=(0, 0, 0, 200))
    draw.text(at, text, font=font, fill=(*colour, 255), anchor="mm")


def _footprint(act: str) -> tuple[float, float]:
    """Half of a spell's footprint on screen, across and down."""
    across = SPELL_TILES[act] * TILE * math.sqrt(2)
    return across, across * MAP_SLOPE


def _ring(
    draw: ImageDraw.ImageDraw,
    centre: tuple[int, int],
    half: tuple[float, float],
    colour: Colour,
    *,
    dashed: bool,
) -> None:
    """An ellipse about `centre`: filled faintly, or its outline alone in dashes."""
    box = (centre[0] - half[0], centre[1] - half[1], centre[0] + half[0], centre[1] + half[1])
    if not dashed:
        draw.ellipse(box, fill=(*colour, 70), outline=(*colour, 255), width=3)
        return
    for start in range(0, 360, 30):
        draw.arc(box, start, start + 18, fill=(*colour, 255), width=4)


def _grid_lines(draw: ImageDraw.ImageDraw, grid: BaseGrid, *, alone: bool) -> None:
    """The diamond cut into three by three."""
    shade = 200 if alone else 130
    for k in range(4):
        draw.line([grid.at(k / 3, 0), grid.at(k / 3, 1)], fill=(*WHITE, shade), width=3)
        draw.line([grid.at(0, k / 3), grid.at(1, k / 3)], fill=(*WHITE, shade), width=3)


def _grid_labels(draw: ImageDraw.ImageDraw, grid: BaseGrid) -> None:
    """The four sides, the four corners and the nine cells, numbered row by row from the top corner."""
    big, mid = _font(34), _font(24)
    for cell in range(9):
        at = grid.at((cell % 3 + 0.5) / 3, (cell // 3 + 0.5) / 3)
        _tag(draw, at, str(cell + 1), WHITE, big, lowest=655)
    for name, one, two, (dx, dy) in SIDES:
        a, b = getattr(grid, one), getattr(grid, two)
        at = ((a[0] + b[0]) / 2 + dx * 95, (a[1] + b[1]) / 2 + dy * 45)
        _tag(draw, at, name, SIDE_COLOUR, mid, lowest=690)
    for name, corner, (dx, dy) in CORNERS:
        x, y = getattr(grid, corner)
        text = f"{name} (空地)" if corner in grid.empty else name
        _tag(draw, (x + dx, y + dy), text, CORNER_COLOUR, mid)


Label = tuple[tuple[float, float], str]


def _lines(
    draw: ImageDraw.ImageDraw, step: PlannedDrop, colour: Colour, label: str, *, ghost: bool
) -> list[Label]:
    """The lines troops are spread along, labelled at the first one's start."""
    for line in step.lines:
        if ghost:
            for one, two in itertools.islice(itertools.pairwise(line), 0, None, 2):
                draw.line([one, two], fill=(*colour, 255), width=5)
            continue
        draw.line(line, fill=(*colour, 255), width=6, joint="curve")
        for x, y in line:
            draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=(*colour, 255))
    return [((line[0][0], line[0][1] - 26), label) for line in step.lines[:1]]


def _bottles(
    draw: ImageDraw.ImageDraw, step: PlannedDrop, colour: Colour, label: str, *, ghost: bool
) -> list[Label]:
    """A spell's footprint at each spot a bottle lands, and in dashes where the plan has none to put."""
    half = _footprint(step.act)
    for spot in step.unused:
        _ring(draw, spot, half, colour, dashed=True)
    labels: list[Label] = []
    for spot in dict.fromkeys(step.at):
        _ring(draw, spot, half, colour, dashed=ghost)
        stacked = step.at.count(spot)
        text = f"{label}{TIMES}{stacked}" if stacked > 1 else label
        labels.append(((spot[0], spot[1] - half[1] + 16), text))
    return labels


def _spots(
    draw: ImageDraw.ImageDraw, step: PlannedDrop, colour: Colour, label: str, *, ghost: bool
) -> list[Label]:
    """Where a one-off card goes: a pentagon for a hero, a square for a machine."""
    for x, y in dict.fromkeys(step.at):
        if ghost:
            _ring(draw, (x, y), (16, 16), colour, dashed=True)
        elif step.act == "hero":
            draw.regular_polygon((x, y, 18), 5, fill=(*colour, 255), outline=(0, 0, 0, 255))
        else:
            box = (x - 14, y - 14, x + 14, y + 14)
            draw.rectangle(box, fill=(*colour, 255), outline=(0, 0, 0, 255))
    return [((x, y + 34), label) for x, y in dict.fromkeys(step.at)]


def _marks(
    draw: ImageDraw.ImageDraw, steps: Iterable[PlannedDrop], *, ghost: bool
) -> dict[tuple[float, float], tuple[list[str], Colour]]:
    """Every step where it lands, and the number to write at each place.

    A ghost is the plan another is held against: white dashes, each labelled
    `GHOST`. The labels come back rather than being written here, so a later
    step's shape does not cover them, and steps sharing a spot share one.
    """
    labels: dict[tuple[float, float], tuple[list[str], Colour]] = {}
    placed = (step for step in steps if step.act != "ability")
    for number, step in enumerate(placed, 1):
        colour = WHITE if ghost else COLOURS[step.act]
        # A step the frame's row has no card for is still drawn where it would
        # go, since the next army may carry it; the legend says it is missing.
        label = GHOST if ghost else f"#{number}"
        shape = _bottles if step.act in SPELL_TILES else _spots
        named = _lines(draw, step, colour, label, ghost=ghost)
        named += shape(draw, step, colour, label, ghost=ghost)
        for at, text in named:
            texts = labels.setdefault(at, ([], colour))[0]
            if text not in texts:
                texts.append(text)
    return labels


def _only_in(other: list[PlannedDrop], steps: list[PlannedDrop]) -> list[PlannedDrop]:
    """What `other` marks that `steps` does not, so a mark both plans share is drawn once."""
    spots = {(step.act, spot) for step in steps for spot in (*step.at, *step.unused)}
    lines = {(step.act, tuple(line)) for step in steps for line in step.lines}
    return [
        step.model_copy(
            update={
                "at": [spot for spot in step.at if (step.act, spot) not in spots],
                "unused": [],
                "lines": [line for line in step.lines if (step.act, tuple(line)) not in lines],
            }
        )
        for step in other
    ]


def row_line(row: CardRow | None) -> str:
    """The army a picture assumes, as the frame's card row was counted."""
    if row is None:
        return "這張圖讀不到卡片列: 不知道有幾張卡、幾瓶法術, 法術的點都用虛線畫"

    def counts(held: list[int | None]) -> str:
        return " ".join("x?" if count is None else f"x{count}" for count in held) or "沒有"

    parts = [f"部隊 {len(row.troops)} 張卡 ({counts(row.troops)})", f"單張的卡 {row.singles} 張"]
    if row.rages or row.freezes:
        parts += [f"狂暴 {counts(row.rages)}", f"冰凍 {counts(row.freezes)}"]
    return f"這張圖的卡片列: {', '.join(parts)}"


def _named(step: PlannedDrop, cards: dict[HeroKind, int], sent: int) -> str:
    """One step as the legend names it: by the card it uses, since no card says which hero it is."""
    name = ACT_NAMES[step.act]
    if step.act == "hero" and step.who == "unknown":
        name = "其餘英雄卡" + (f" {step.cards} 張" if step.cards else "")
    elif step.act == "hero":
        name = f"第 {sent + 1} 張英雄卡 (計畫寫{HERO_NAMES[step.who]})"
    elif step.act == "ability":
        name = f"第 {cards[step.who]} 張英雄卡{name}" if step.who in cards else f"英雄{name}"
    elif step.act == "troops" and step.cards:
        name += f" {step.cards} 張卡"
    elif step.act in SPELL_TILES and step.cards is None:
        name += " (卡片列讀不到, 瓶數不明)"
    elif step.act in SPELL_TILES:
        name += f" {len(step.at)} 瓶"
        if step.unused:
            name += f" (虛線 {len(step.unused)} 處沒有瓶子可放)"
    if step.cards == 0:
        name += " (這張圖的卡片列沒有這張卡)"
    return name


def legend(steps: list[PlannedDrop]) -> list[str]:
    """One line per moment of the plan's own clock, the placed steps numbered as on the picture."""
    lines: dict[int, list[str]] = {}
    cards: dict[HeroKind, int] = {}
    number = sent = 0
    for step in steps:
        name = _named(step, cards, sent)
        if step.act == "hero":
            if step.who != "unknown" and step.cards != 0:
                cards[step.who] = sent + 1
            sent += step.cards or 0
        if step.act != "ability":
            number += 1
            name = f"#{number} {name}"
        lines.setdefault(step.seconds, []).append(name)
    return [f"{seconds} 秒: {' → '.join(names)}" for seconds, names in lines.items()]


def draw_plan(
    frame: bytes,
    grid: BaseGrid | None,
    steps: list[PlannedDrop],
    against: list[PlannedDrop],
    notes: list[str],
) -> bytes:
    """The frame with the grid, a plan's steps and the plan it is held against, over a strip of notes."""
    image = open_frame(frame).convert("RGBA")
    over = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(over)
    if grid is not None:
        _grid_lines(draw, grid, alone=not steps)
    small = _font(20)
    labels = _marks(draw, _only_in(against, steps), ghost=True)
    for at, (texts, colour) in (labels | _marks(draw, steps, ghost=False)).items():
        _tag(draw, at, " ".join(texts), colour, small)
    if grid is not None:
        _grid_labels(draw, grid)
    drawn = Image.alpha_composite(image, over)
    # The card row back on top of whatever reached it: a grid's lower corner does.
    drawn.paste(image.crop((0, CARD_ROW_TOP, *image.size)), (0, CARD_ROW_TOP))
    sheet = Image.new("RGB", (image.width, image.height + NOTE_PAD * 2 + NOTE_LINE * len(notes)))
    sheet.paste(drawn.convert("RGB"), (0, 0))
    words = ImageDraw.Draw(sheet)
    font = _font(22)
    for row, text in enumerate(notes):
        words.text((20, image.height + NOTE_PAD + row * NOTE_LINE), text, font=font, fill=WHITE)
    out = io.BytesIO()
    sheet.save(out, format="PNG")
    return out.getvalue()
