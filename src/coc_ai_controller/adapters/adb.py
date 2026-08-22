from __future__ import annotations

import io
import os
import re
from typing import TYPE_CHECKING
import logging

import adbutils
from pydantic import BaseModel
from defusedxml import ElementTree as ET  # noqa: N817 - the conventional alias for ElementTree

from coc_ai_controller.models import UiElement, AdbEndpoint

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class AdbControlError(RuntimeError):
    pass


def use_adb_executable(path: Path) -> None:
    """Point adbutils at the emulator's own adb.exe instead of whatever is on PATH."""
    os.environ["ADBUTILS_ADB_PATH"] = str(path)
    logger.info("adbutils will use %s", path)


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

    def shell(self, command: list[str], timeout: float = 12) -> str:
        logger.debug("adb -s %s shell %s", self.serial, " ".join(command))
        try:
            return str(self.connect().shell(command, timeout=timeout))
        except adbutils.AdbError as exc:
            raise AdbControlError(f"ADB 指令失敗 {' '.join(command)}：{exc}") from exc

    def screenshot(self) -> bytes:
        device = self.connect()
        try:
            image = device.screenshot(error_ok=False)
        except adbutils.AdbError as exc:
            raise AdbControlError(f"ADB 截圖失敗 {self.serial}：{exc}") from exc
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        data = buffer.getvalue()
        logger.info(
            "Captured %s %dx%d (%d bytes)", self.serial, image.width, image.height, len(data)
        )
        return data

    def tap(self, x: int, y: int) -> None:
        logger.info("Tap %s at (%d, %d)", self.serial, x, y)
        self.connect().click(int(x), int(y))

    def swipe(self, start: tuple[int, int], end: tuple[int, int], duration_ms: int) -> None:
        logger.info("Swipe %s %s -> %s in %d ms", self.serial, start, end, duration_ms)
        self.connect().swipe(start[0], start[1], end[0], end[1], duration=duration_ms / 1000)

    def back(self) -> None:
        logger.info("Back key on %s", self.serial)
        self.connect().keyevent("BACK")

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
