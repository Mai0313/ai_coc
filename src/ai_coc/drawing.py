"""Draw a plan's steps and a pointing grid on a battle frame, for a person to look at.

Pure: a frame and what to mark go in, a PNG comes out. Where a plan lands is
`ui.attack.planned_drops`'s and where the grid sits `parsers.boundary`'s; this
only paints them, with labels in Traditional Chinese since the picture is for
the user. Nothing is painted over the card row, which the picture is read
against, and the picture carries no prose: a step's mark has its number and
the plan's second, and what the picture assumes or cannot show is the
command's text to say (the user, 2026-11-21: those are what the discussion is
about, so they do not belong on the picture).
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


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for name in FONTS:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    logger.warning("No font with Chinese glyphs among %s; the labels will not read", FONTS)
    return ImageFont.load_default(size)


Box = tuple[float, float, float, float]


def _tag(  # noqa: PLR0913 - one label: where it may stand, what it says, how it looks, what it avoids
    draw: ImageDraw.ImageDraw,
    spots: list[tuple[float, float]],
    text: str,
    colour: Colour,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    *,
    taken: list[Box],
    lowest: int = PLAYFIELD[3] - 3,
) -> tuple[float, float]:
    """A label on a dark plate, at the first of `spots` where it covers nothing in `taken`; answers where.

    Every spot is kept on the playfield first. A grid's lower corner lies under
    the card row, so its labels are lifted onto the ground above it, each kind
    to a height of its own (`lowest`). With no spot free the first one is used,
    since a label over another still says more than a missing one. The plate
    joins `taken`.
    """
    left, top, right, _bottom = PLAYFIELD
    plates: list[tuple[Box, tuple[float, float]]] = []
    for x, y in spots:
        at = (min(max(x, left + 50), right - 50), min(max(y, top + 16), lowest))
        box = draw.textbbox(at, text, font=font, anchor="mm")
        plates.append(((box[0] - 5, box[1] - 3, box[2] + 5, box[3] + 3), at))
    plate, at = next(
        (
            (plate, at)
            for plate, at in plates
            if not any(
                plate[0] < other[2]
                and other[0] < plate[2]
                and plate[1] < other[3]
                and other[1] < plate[3]
                for other in taken
            )
        ),
        plates[0],
    )
    draw.rectangle(plate, fill=(0, 0, 0, 200))
    draw.text(at, text, font=font, fill=(*colour, 255), anchor="mm")
    taken.append(plate)
    return at


def _beside(point: tuple[float, float], step: tuple[int, int]) -> list[tuple[float, float]]:
    """A label's own place, then the places a step or two to either side of it."""
    return [(point[0] + step[0] * k, point[1] + step[1] * k) for k in (0, -1, 1, -2, 2)]


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


def _grid_labels(draw: ImageDraw.ImageDraw, grid: BaseGrid, taken: list[Box]) -> None:
    """The four sides, the four corners and the nine cells, numbered row by row from the top corner.

    Each stands clear of what is already written or marked (`taken`): a cell's
    number moves up or down off a step's label, and a corner's label, which
    stands just outside the corner where a plan can put a one-off drop (the
    flat plan's siege machine sits outside the left corner), moves along the
    corner's other axis.
    """
    big, mid = _font(34), _font(24)
    for cell in range(9):
        at = grid.at((cell % 3 + 0.5) / 3, (cell // 3 + 0.5) / 3)
        _tag(draw, _beside(at, (0, 46)), str(cell + 1), WHITE, big, taken=taken, lowest=655)
    for name, one, two, (dx, dy) in SIDES:
        a, b = getattr(grid, one), getattr(grid, two)
        at = ((a[0] + b[0]) / 2 + dx * 95, (a[1] + b[1]) / 2 + dy * 45)
        _tag(draw, [at], name, SIDE_COLOUR, mid, taken=taken, lowest=690)
    for name, corner, (dx, dy) in CORNERS:
        x, y = getattr(grid, corner)
        text = f"{name} (空地)" if corner in grid.empty else name
        aside = (60, 0) if dx == 0 else (0, 60)
        _tag(draw, _beside((x + dx, y + dy), aside), text, CORNER_COLOUR, mid, taken=taken)


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


def _worded(entries: list[tuple[str, int | None]]) -> str:
    """Steps sharing a place as one label: their numbers, then the second of the plan's clock they share."""
    parts: list[str] = []
    for seconds, group in itertools.groupby(entries, key=lambda entry: entry[1]):
        tags = " ".join(tag for tag, _ in group)
        parts.append(tags if seconds is None else f"{tags} {seconds}秒")
    return " ".join(parts)


def _marks(
    draw: ImageDraw.ImageDraw, steps: Iterable[PlannedDrop], *, ghost: bool
) -> dict[tuple[float, float], tuple[list[tuple[str, int | None]], Colour]]:
    """Every step where it lands, and what to write at each place: its number and its second.

    A ghost is the plan another is held against: white dashes, each labelled
    `GHOST` with no second. The labels come back rather than being written
    here, so a later step's shape does not cover them, and steps sharing a
    spot share one.
    """
    labels: dict[tuple[float, float], tuple[list[tuple[str, int | None]], Colour]] = {}
    placed = (step for step in steps if step.act != "ability")
    for number, step in enumerate(placed, 1):
        colour = WHITE if ghost else COLOURS[step.act]
        # A step the frame's row has no card for is still drawn where it would
        # go, since the next army may carry it; the command's text says it is missing.
        label = GHOST if ghost else f"#{number}"
        shape = _bottles if step.act in SPELL_TILES else _spots
        named = _lines(draw, step, colour, label, ghost=ghost)
        named += shape(draw, step, colour, label, ghost=ghost)
        for at, text in named:
            entries = labels.setdefault(at, ([], colour))[0]
            entry = (text, None if ghost else step.seconds)
            if entry not in entries:
                entries.append(entry)
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

    # The builder base's cards write their count another way and none of it reads.
    held = f" ({counts(row.troops)})" if any(count is not None for count in row.troops) else ""
    parts = [f"部隊 {len(row.troops)} 張卡{held}", f"單張的卡 {row.singles} 張"]
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
    frame: bytes, grid: BaseGrid | None, steps: list[PlannedDrop], against: list[PlannedDrop]
) -> bytes:
    """The frame with the grid, a plan's steps and the plan it is held against, and nothing added to it."""
    image = open_frame(frame).convert("RGBA")
    over = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(over)
    if grid is not None:
        _grid_lines(draw, grid, alone=not steps)
    small = _font(20)
    labels = _marks(draw, _only_in(against, steps), ghost=True)
    labels |= _marks(draw, steps, ghost=False)
    # A one-off drop's own mark is not to be written over; a spell's footprint may be.
    taken: list[Box] = [
        (x - 18, y - 18, x + 18, y + 18)
        for step in steps
        if step.act not in SPELL_TILES
        for x, y in step.at
    ]
    for at, (entries, colour) in labels.items():
        _tag(draw, _beside(at, (0, 28)), _worded(entries), colour, small, taken=taken)
    if grid is not None:
        _grid_labels(draw, grid, taken)
    drawn = Image.alpha_composite(image, over)
    # The card row back on top of whatever reached it: a grid's lower corner does.
    drawn.paste(image.crop((0, CARD_ROW_TOP, *image.size)), (0, CARD_ROW_TOP))
    out = io.BytesIO()
    drawn.convert("RGB").save(out, format="PNG")
    return out.getvalue()
