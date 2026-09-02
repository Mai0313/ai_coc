"""One captured frame, decoded the way every parser here reads it.

Every coordinate in this project was measured on a 1600x900 screen, so a frame
of any other size is not read at all: a reader handed one would answer with
whatever those coordinates happen to land on, which is the silent kind of
wrong. Each parser used to carry its own copy of that check, and the ones that
did not carry one read the garbage.
"""

from __future__ import annotations

import io

from PIL import Image

SCREEN_SIZE = (1600, 900)


def open_frame(png: bytes) -> Image.Image:
    """The frame as an RGB image, or a `ValueError` for one of the wrong size."""
    image = Image.open(io.BytesIO(png)).convert("RGB")
    if image.size != SCREEN_SIZE:
        raise ValueError(f"判讀座標只適用 1600x900，收到 {image.size[0]}x{image.size[1]}")
    return image
