from __future__ import annotations

import os
import re
import time
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

# How long `tap_many` leaves between one tap and the next, and it is now nothing
# at all. **This was 0.05 and the reason given for it did not survive being
# measured again.** The reasoning was that the game samples touches once a
# display frame and keeps one of whatever arrived in it, so chained `input`
# calls landing ~12 ms apart would lose all but the first; the evidence was a
# battle that deployed nothing while reporting success, which is consistent with
# that story and with several others.
#
# Measured directly instead, on the builder base where an attack costs nothing:
# five troop cards holding four each, tapped **exactly four times with no gap**,
# emptied five out of five. A whole attack run at this spacing then deployed all
# twenty troops for 80% destruction, and took the troop half of the deployment
# from about 19 seconds to about 8 — pass one from 10 s to 4 and pass two from
# 9 to 4.
#
# What makes zero safe to keep is not the spacing on its own but that every
# caller either has slack or checks: a deployment pass taps `DROPS_PER_PASS`
# against a card holding about four, and `_drop_singles` and `_cast` read the
# cards afterwards and offer another spot to whatever the game did not take.
# The separator itself is left in place because that is the configuration that
# was measured — `sleep 0` is still a process spawn between the taps, worth
# about 10 ms, and taking it out would be a spacing nobody has tried.
TAP_GAP = 0.0

# The Linux input-event codes a pinch is written with. `input` has no two-finger
# gesture of any kind, so the only way to zoom is to write the multi-touch
# protocol straight to the device node.
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
SYN_REPORT = 0
BTN_TOUCH = 0x14A
ABS_MT_SLOT, ABS_MT_POSITION_X, ABS_MT_POSITION_Y, ABS_MT_TRACKING_ID = 0x2F, 0x35, 0x36, 0x39
# Any two ids the kernel is not already using for a live finger.
FIRST_TRACKING_ID = 100
# How many moves the gesture is broken into. One jump from start to end reads as
# a teleport and the game keeps the scale it started at.
PINCH_STEPS = 16
# Where a pinch puts its two fingers, and how far they travel. Centred on the
# playfield so the zoom keeps the village in view, and wide enough that the game
# reads it as a gesture rather than as two taps.
#
# Here rather than beside `view`, because everything that drives the game needs
# to be able to put the camera back and `commands` already imports the `ui`
# layer — a caller in `ui` reaching the other way would be a cycle.
PINCH_NEAR, PINCH_FAR = 150, 500
PINCH_ROW = 450
# How long the camera takes to settle after one.
PINCH_SETTLE = 1.5
# How many pinches to spend putting the camera back at the far zoom. `view`
# measured one gesture as covering the whole range and a second as changing
# nothing, so this is that plus a spare: about three seconds, against a battle
# of three minutes or a run that spends them sweeping the village. Every loop
# that puts the camera back — before a battle, on the first village a runner
# reads, after a crossing, after a restart — spends the same two.
ZOOM_PINCHES = 2


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


def pinch_events(
    first: tuple[tuple[int, int], tuple[int, int]],
    second: tuple[tuple[int, int], tuple[int, int]],
    steps: int = PINCH_STEPS,
) -> list[tuple[int, int, int]]:
    """One two-finger gesture as the multi-touch events it is written with.

    Each finger is given as (start, end) in **screen** coordinates. **The
    device's own axes are the screen's swapped**: it reports x to 900 and y to
    1600 against a 1600x900 screen, so a point goes down as (y, x). Measured by
    tapping (430, 990) through this path and watching the building at screen
    (990, 430) open.

    **`BTN_TOUCH` is not optional.** Without it the whole gesture is accepted,
    reported, and ignored — which is what a first attempt at this looked like,
    several times over, on all three of the device nodes MuMu publishes. Both
    tracking ids are cleared at the end for the matching reason: one left live
    holds the touch down, and the next gesture reads as one finger moving.
    """
    events: list[tuple[int, int, int]] = []

    def place(slot: int, point: tuple[int, int]) -> None:
        events.append((EV_ABS, ABS_MT_SLOT, slot))
        events.append((EV_ABS, ABS_MT_POSITION_X, point[1]))
        events.append((EV_ABS, ABS_MT_POSITION_Y, point[0]))

    for slot, (start, _) in enumerate((first, second)):
        events.append((EV_ABS, ABS_MT_SLOT, slot))
        events.append((EV_ABS, ABS_MT_TRACKING_ID, FIRST_TRACKING_ID + slot))
        place(slot, start)
        if slot == 0:
            events.append((EV_KEY, BTN_TOUCH, 1))
    events.append((EV_SYN, SYN_REPORT, 0))
    for step in range(1, steps + 1):
        for slot, (start, end) in enumerate((first, second)):
            place(
                slot,
                (
                    round(start[0] + (end[0] - start[0]) * step / steps),
                    round(start[1] + (end[1] - start[1]) * step / steps),
                ),
            )
        events.append((EV_SYN, SYN_REPORT, 0))
    for slot in (0, 1):
        events.append((EV_ABS, ABS_MT_SLOT, slot))
        events.append((EV_ABS, ABS_MT_TRACKING_ID, -1))
    events.append((EV_KEY, BTN_TOUCH, 0))
    events.append((EV_SYN, SYN_REPORT, 0))
    return events


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
        """A burst of taps in one shell round-trip, which is how an army goes down fast.

        Deploying an army one call at a time spends most of the battle timer on
        ADB latency. Two things here were each paid for in a battle that
        deployed nothing at all, both of them silent:

        Going as a shell string is what was paid for in a battle that deployed
        nothing at all, and silently: adbutils escapes every argument of a list,
        which turns the separators into literal text and the whole burst into
        one unknown command name.

        The sleeps between them were the other half of that story and are gone;
        see `TAP_GAP` for what replaced the reasoning behind them.
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

    def touch_devices(self) -> list[str]:
        """Every multi-touch input node this device exposes.

        `input` cannot do two fingers, so a pinch has to be written straight to
        the kernel with `sendevent` — and that goes to a device node rather than
        to a display, so nothing routes it the way `input -d` is routed.

        Which node belongs to which display is what `touch_device_for` answers;
        this is the fallback for when that cannot be worked out, and sending to
        all of them is not harmless — see there.

        An empty list is a failure rather than a quiet nothing — a pinch with
        nowhere to send it would otherwise report a zoom that never happened.
        """
        nodes: list[str] = []
        for block in self.shell("getevent -pl 2>/dev/null").split("add device ")[1:]:
            if "ABS_MT_POSITION_X" not in block:
                continue
            node = block.split(":", 1)[1].split()[0] if ":" in block else ""
            if node.startswith("/dev/input/"):
                nodes.append(node)
        logger.debug("Multi-touch nodes: %s", nodes)
        return nodes

    def touch_device_for(self, display: DisplayTarget) -> str | None:
        """The one touch node bound to this display, or None if it cannot be told.

        **Android knows this and says so**, which is worth spelling out because
        the first version of the pinch assumed the opposite and sent every
        gesture to every node. That is not harmless: MuMu keeps a launcher on
        its other displays, and two fingers landing there switch away from the
        foreground app — observed live, a zoom that worked and left the emulator
        showing the launcher with the game behind it.

        The chain is three hops through `dumpsys input`, each one printed by the
        system rather than guessed:

        - the InputReader device whose viewport carries `uniqueId=local:<the
          display's physical id>`, which is the same number `display_for`
          already returns;
        - its `EventHub Devices: [ N ]`;
        - the EventHub entry headed `N:` whose `Path:` is the node.

        None rather than a guess when any hop is missing, because the caller's
        fallback — every node, then relaunch the game — is survivable, while a
        pinch aimed at the wrong display silently does nothing to the camera it
        was meant to fix.
        """
        dump = self.shell("dumpsys input 2>/dev/null")
        reader = re.search(
            rf"EventHub Devices:\s*\[\s*(\d+)[^\]]*\](?:(?!EventHub Devices).)*?"
            rf"uniqueId=local:{display.physical_id}\b",
            dump,
            re.S,
        )
        if reader is None:
            logger.debug("No input device is reported against display %s", display.physical_id)
            return None
        path = re.search(rf"\n\s*{reader.group(1)}:[^\n]*\n(?:[^\n]*\n)*?\s*Path:\s*(\S+)", dump)
        if path is None:
            logger.debug("EventHub device %s reports no path", reader.group(1))
            return None
        logger.debug("Display %s is driven by %s", display.physical_id, path.group(1))
        return path.group(1)

    def pinch(
        self,
        first: tuple[tuple[int, int], tuple[int, int]],
        second: tuple[tuple[int, int], tuple[int, int]],
        steps: int = PINCH_STEPS,
        node: str | None = None,
    ) -> None:
        """Two fingers, each moving from its own start to its own end.

        Screen coordinates, like everything else here; `pinch_events` owns the
        protocol and the reasons for it.

        `node` is the one to send to. Without it the stream goes to every
        multi-touch node, which reaches the game but also the launcher MuMu
        keeps on its other displays — see `touch_device_for` for why that is
        not free.
        """
        nodes = [node] if node else self.touch_devices()
        if not nodes:
            raise AdbControlError(f"{self.serial} 找不到任何多點觸控裝置，縮放送不出去")
        stream = pinch_events(first, second, steps)
        logger.info("Pinch %s %s and %s on %s", self.serial, first, second, nodes)
        for target in nodes:
            self.shell(
                " ; ".join(
                    f"sendevent {target} {kind} {code} {value}" for kind, code, value in stream
                ),
                timeout=30,
            )

    def zoom(
        self,
        direction: str,
        times: int = 1,
        package: str = "",
        display: DisplayTarget | None = None,
    ) -> None:
        """Pinch the camera in or out, however many times.

        **Zooming out past the far limit does nothing at all**, which is what
        makes `out` safe to send without knowing where the camera currently is
        — and knowing is not on offer, since the game reports no zoom level and
        every attempt here to read one off a frame was defeated by something
        else moving in it. So the way to be at the far limit is to ask for it
        rather than to check for it.

        **`display` is what keeps the gesture off the other screens.** A pinch
        with nowhere to aim goes to every multi-touch node, and MuMu keeps a
        launcher on its other displays where two fingers switch away from the
        foreground app — observed live, a zoom that worked and left the emulator
        showing the launcher with the game behind it. Given a display, the
        gesture goes to that display's node alone and nothing else sees it.

        **`package` is the belt to that pair of braces.** It relaunches the game
        afterwards, which costs nothing when it is already in front, and covers
        the case where the node could not be worked out and the pinch had to go
        everywhere after all.
        """
        node = self.touch_device_for(display) if display is not None else None
        near = ((800 - PINCH_NEAR, PINCH_ROW), (800 + PINCH_NEAR, PINCH_ROW))
        far = ((800 - PINCH_FAR, PINCH_ROW), (800 + PINCH_FAR, PINCH_ROW))
        # Fingers converging is the game zooming out, which widens the view.
        starts, ends = (far, near) if direction == "out" else (near, far)
        for _ in range(times):
            self.pinch((starts[0], ends[0]), (starts[1], ends[1]), node=node)
            time.sleep(PINCH_SETTLE)
        # Skipped when the gesture was aimed, because then nothing else saw it.
        if package and node is None:
            self.launch_app(package)
            time.sleep(PINCH_SETTLE)

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
