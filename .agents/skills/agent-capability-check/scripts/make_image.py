"""Draw a frame-sized image with a known answer, for agent-capability-check.

    uv run --no-sync python .agents/skills/agent-capability-check/scripts/make_image.py --out <png> --key <json>

The image is 1600x900, the size of the emulator frames ai_coc taps on. It holds a
panel-style row of text (a building's name in Traditional Chinese and a price) and
three filled dots, red, green and blue, all at random places. The answer is written
to the key file only, so the agent being tested reads the image without knowing what
it holds. The text uses Microsoft JhengHei, which Windows ships.
"""

import json
import random
import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SIZE = (1600, 900)
FONT = "C:/Windows/Fonts/msjh.ttc"
NAMES = ("迫擊砲", "巨石碑", "地獄之塔", "弓箭女皇", "聖水瓶", "法師塔", "空氣炮")
DOTS = {"red": (220, 40, 40), "green": (40, 180, 60), "blue": (40, 90, 220)}
RADIUS = 18
GAP = 40


def _overlaps(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    return a[0] - GAP < b[2] and b[0] - GAP < a[2] and a[1] - GAP < b[3] and b[1] - GAP < a[3]


def main() -> None:
    """Write the image to ``--out`` and its answer to ``--key``."""
    parser = argparse.ArgumentParser(description="Draw a test frame with a known answer.")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    args = parser.parse_args()
    rng = random.SystemRandom()
    image = Image.new("RGB", SIZE, (52, 48, 44))
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(FONT, 48)
    price = f"{rng.randint(100, 9999) * 1000:,}".replace(",", " ")
    text = f"{rng.choice(NAMES)} {price}"
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    x = rng.randint(GAP, SIZE[0] - GAP - right)
    y = rng.randint(GAP, SIZE[1] - GAP - bottom)
    draw.text((x, y), text, font=font, fill=(245, 240, 230))
    taken = [(x + left, y + top, x + right, y + bottom)]
    markers = {}
    for name, colour in DOTS.items():
        while True:
            cx = rng.randint(RADIUS, SIZE[0] - RADIUS)
            cy = rng.randint(RADIUS, SIZE[1] - RADIUS)
            box = (cx - RADIUS, cy - RADIUS, cx + RADIUS, cy + RADIUS)
            if not any(_overlaps(box, other) for other in taken):
                break
        draw.ellipse(box, fill=colour)
        taken.append(box)
        markers[name] = [cx, cy]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.out)
    answer = {"text": text, "markers": markers}
    args.key.write_text(json.dumps(answer, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
