from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING
import logging

import adbutils
from pydantic import BaseModel
from defusedxml import ElementTree as ET  # noqa: N817 - the conventional alias for ElementTree

from ai_coc.models import UiElement, AdbEndpoint, DisplayTarget

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

PNG_MAGIC = b"\x89PNG"

# How long `tap_many` leaves between one tap and the next. It exists because the
# game samples touches once a display frame and keeps one of whatever arrived in
# that frame, so the taps have to be more than a frame apart or troops go missing.
# Measured against this emulator: the shell round trip is 47 ms and each `input`
# inside it costs about 12 ms, so five taps chained with no sleep at all take
# 109 ms in total — a spacing well under one frame, which is the bug. This adds
# up to roughly 62 ms between taps, two frames at 30 fps and four at 60. It was
# 0.12, which is safe and is also two thirds of the time a whole army takes to
# deploy: 300 taps at that spacing is 36 seconds of a three-minute battle spent
# sleeping.
TAP_GAP = 0.05


class AdbControlError(RuntimeError):
    pass


def use_adb_executable(path: Path) -> None:
    """Point adbutils at the emulator's own adb.exe instead of whatever is on PATH."""
    os.environ["ADBUTILS_ADB_PATH"] = str(path)
    logger.info("adbutils will use %s", path)


def focused_display(window_dump: str, package: str) -> str:
    """Read `dumpsys window displays` for the logical display holding a package's window.

    Only the topmost display carries `mCurrentFocus`, so `mFocusedApp` counts too: the
    game keeps that one on its own display while anything else holds the focus.
    """
    blocks = re.split(r"Display: mDisplayId=(\d+)", window_dump)
    for logical, block in zip(blocks[1::2], blocks[2::2], strict=True):
        focus = re.findall(r"m(?:CurrentFocus|FocusedApp)=(.+)", block)
        if any(package in line for line in focus):
            return logical
    return ""


def physical_display(display_dump: str, logical_id: str) -> str:
    """Bridge `dumpsys display`'s two id schemes through the display name they share."""
    names = {
        logical: name
        for name, logical in re.findall(r'DisplayInfo\{"([^"]*)", displayId (\d+)', display_dump)
    }
    physical = dict(
        re.findall(r'DisplayDeviceInfo\{"([^"]*)": uniqueId="local:(\d+)"', display_dump)
    )
    return physical.get(names.get(logical_id, ""), "")


class AdbController(BaseModel):
    """Every ADB call in the application goes through adbutils, one per serial."""

    endpoint: AdbEndpoint

    @property
    def serial(self) -> str:
        return self.endpoint.serial

    def connect(self) -> adbutils.AdbDevice:
        """Reconnect on every call: a restarted instance drops the old handle."""
        if not self.endpoint.ready:
            raise AdbControlError(f"模擬器尚未開放 ADB 連接埠：{self.serial}")
        logger.debug("Connecting ADB %s", self.serial)
        try:
            adbutils.adb.connect(self.serial, timeout=8.0)
            device = adbutils.adb.device(serial=self.serial)
            state = device.get_state()
        except adbutils.AdbError as exc:
            raise AdbControlError(f"ADB 連線失敗 {self.serial}：{exc}") from exc
        if state != "device":
            raise AdbControlError(f"ADB 尚未就緒 {self.serial}：{state}")
        return device

    def shell(self, command: list[str] | str, timeout: float = 12) -> str:
        """A list has every argument escaped; pass a string to use shell syntax."""
        printable = command if isinstance(command, str) else " ".join(command)
        logger.debug("adb -s %s shell %s", self.serial, printable)
        try:
            return str(self.connect().shell(command, timeout=timeout))
        except adbutils.AdbError as exc:
            raise AdbControlError(f"ADB 指令失敗 {printable}：{exc}") from exc

    def display_for(self, package: str) -> DisplayTarget:
        """Locate the display holding a package; MuMu leaves display 0 on its own launcher."""
        logical = focused_display(self.shell(["dumpsys", "window", "displays"]), package)
        if not logical:
            raise AdbControlError(f"{package} 目前沒有出現在任何 display 上")
        physical = physical_display(self.shell(["dumpsys", "display"]), logical)
        if not physical:
            raise AdbControlError(f"找不到 display {logical} 的實體編號")
        logger.debug("%s is on display %s (physical %s)", package, logical, physical)
        return DisplayTarget(logical_id=logical, physical_id=physical)

    def screenshot(self, display: DisplayTarget) -> bytes:
        """Name the display: with several of them screencap prefixes the PNG with a warning."""
        device = self.connect()
        try:
            data = device.shell(
                ["screencap", "-p", "-d", display.physical_id], encoding=None, timeout=20
            )
        except adbutils.AdbError as exc:
            raise AdbControlError(f"ADB 截圖失敗 {self.serial}：{exc}") from exc
        if not data.startswith(PNG_MAGIC):
            raise AdbControlError(f"ADB 截圖失敗 {self.serial}：{data[:120]!r}")
        # DEBUG, not INFO: the live preview calls this twice a second, which at
        # INFO buries every decision the loop logs under a wall of captures.
        logger.debug(
            "Captured %s display %s (%d bytes)", self.serial, display.logical_id, len(data)
        )
        return data

    def tap(self, x: int, y: int, display: DisplayTarget) -> None:
        logger.info("Tap %s display %s at (%d, %d)", self.serial, display.logical_id, x, y)
        self.input(display, "tap", str(int(x)), str(int(y)))

    def tap_many(
        self, points: list[tuple[int, int]], display: DisplayTarget, gap: float = TAP_GAP
    ) -> None:
        """A burst of taps in one shell round-trip, spaced far enough apart to land.

        Deploying an army one call at a time spends most of the battle timer on
        ADB latency. Two things here were each paid for in a battle that
        deployed nothing at all, both of them silent:

        - it goes as a shell string, because adbutils escapes every argument of
          a list, which turns the separators into literal text;
        - the sleeps are load-bearing. Chained `input` calls land about 10 ms
          apart, well inside one display frame, and the game keeps only the
          first of them.
        """
        logger.info(
            "Tapping %d points on %s display %s", len(points), self.serial, display.logical_id
        )
        taps = [f"input -d {display.logical_id} tap {int(x)} {int(y)}" for x, y in points]
        self.shell(f";sleep {gap};".join(taps), timeout=len(points) * (gap + 0.5) + 15)

    def swipe(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        duration_ms: int,
        display: DisplayTarget,
    ) -> None:
        logger.info("Swipe %s %s -> %s in %d ms", self.serial, start, end, duration_ms)
        coordinates = [str(start[0]), str(start[1]), str(end[0]), str(end[1])]
        self.input(display, "swipe", *coordinates, str(duration_ms))

    def back(self, display: DisplayTarget) -> None:
        logger.info("Back key on %s display %s", self.serial, display.logical_id)
        self.input(display, "keyevent", "BACK")

    def input(self, display: DisplayTarget, *arguments: str) -> None:
        """`input` defaults to display 0, which under MuMu is the launcher, not the game."""
        self.shell(["input", "-d", display.logical_id, *arguments])

    def screen_geometry(self) -> tuple[str, str]:
        """Landscape resolution and density, both `unknown` when ADB will not say."""
        device = self.connect()
        size = device.window_size()
        resolution = f"{max(size.width, size.height)}x{min(size.width, size.height)}"
        density = re.findall(r"(\d+)", self.shell(["wm", "density"], timeout=5))
        return resolution, density[-1] if density else "unknown"

    def is_running(self, package: str) -> bool:
        return bool(self.shell(["pidof", package], timeout=5).strip())

    def launch_app(self, package: str) -> None:
        logger.info("Launching %s on %s", package, self.serial)
        self.shell(
            ["monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"], timeout=15
        )

    def stop_app(self, package: str) -> None:
        logger.info("Force-stopping %s on %s", package, self.serial)
        self.shell(["am", "force-stop", package], timeout=8)

    def ui_elements(self) -> list[UiElement]:
        """Read Android's accessibility hierarchy without extra device agents."""
        try:
            xml_data = self.connect().dump_hierarchy()
        except adbutils.AdbError as exc:
            logger.warning("uiautomator dump failed on %s: %s", self.serial, exc)
            return []
        try:
            root = ET.fromstring(xml_data[xml_data.find("<?xml") :])
        except (ET.ParseError, ValueError):
            logger.warning("uiautomator dump on %s was not parseable XML", self.serial)
            return []
        elements: list[UiElement] = []
        for node in root.iter("node"):
            text = (node.get("text") or node.get("content-desc") or "").strip()
            match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
            if not match or not (text or node.get("clickable") == "true"):
                continue
            left, top, right, bottom = map(int, match.groups())
            elements.append(
                UiElement(
                    text=text,
                    resource_id=node.get("resource-id", ""),
                    clickable=node.get("clickable") == "true",
                    x=(left + right) // 2,
                    y=(top + bottom) // 2,
                )
            )
        logger.info("Accessibility dump on %s returned %d elements", self.serial, len(elements))
        return elements[:120]
