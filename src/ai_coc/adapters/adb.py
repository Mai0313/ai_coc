from __future__ import annotations

import os
import re
import time
import base64
import struct
from typing import TYPE_CHECKING
import logging

import adbutils
from pydantic import BaseModel

from ai_coc.models import AdbEndpoint, DisplayTarget

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
# struct input_event as the 64-bit kernel lays it out: two 8-byte timeval fields,
# then type, code and value. The kernel stamps the arrival time itself, so the
# first two go out as zero.
INPUT_EVENT = struct.Struct("<qqHHi")
# How many moves the gesture is broken into. One jump from start to end reads as
# a teleport and the game keeps the scale it started at.
#
# It was 16 while every event cost a `sendevent`, which put a step 225 ms and
# 22 px apart — visibly a series of jumps rather than a drag, and MuMu draws a
# dot under every touch it receives, so it is visible. `gesture_script` took the
# same 48 steps from 3.6 s to 0.28 s, which is what makes three times the
# resolution cheaper than the old sixteen rather than dearer.
PINCH_STEPS = 48
# What separates one step from the next. `gesture_script` writes a report in
# about 6 ms on its own, so this is what puts a step on roughly a display frame
# instead of three: measured, 48 steps come out at 0.66 s, which is a hand's
# pace. It is not what makes the gesture *work* — the game reads the same zoom
# at every gap from 0 to 0.02 — it is what makes it look like one.
PINCH_GAP = 0.008
# Where a pinch puts its two fingers, and how far they travel. Centred on the
# playfield so the zoom keeps the village in view, and wide enough that the game
# reads it as a gesture rather than as two taps.
#
# Here rather than beside `view`, because everything that drives the game needs
# to be able to put the camera back and `commands` already imports the `ui`
# layer — a caller in `ui` reaching the other way would be a cycle.
PINCH_NEAR, PINCH_FAR = 150, 500
PINCH_ROW = 450
# How long the camera takes to settle after one. Swept from a camera zoomed
# fully in, two `out` gestures reach the far limit at every settle from 1.5 s
# down to none at all — 0.505 to 0.507 of bare ground in all five — so this is
# not what makes the second gesture land. What it is for is the frame the caller
# takes next: `_settle_zoom` hands its capture to `_settle_camera`, which
# measures the village against the screen, and a frame taken mid-animation
# measures a village that is still moving. 0.4 s is that margin, and it takes a
# zoom out from 10.2 s to 2.6 s alongside the gesture below.
PINCH_SETTLE = 0.4
# What the game needs to come back to the front after the fallback relaunch
# below, which is a different question from the camera's own animation and was
# borrowing this one's number until the number moved.
RELAUNCH_SETTLE = 1.5
# How many pinches to spend putting the camera back at the far zoom. **Two is
# needed rather than spare, and this said the opposite for a long time.** The
# claim here was that one gesture covered the whole range; swept from a camera
# zoomed fully in, one `out` takes the bare-ground share of the frame from 0.19
# to 0.40 and a second to 0.507, where a third moves it by 0.002. Two thirds,
# not all of it.
#
# **And a longer finger travel does not buy the rest**, which is the obvious
# thing to try: at 150/500 one gesture reaches 0.404, at 150/550 0.426, at
# 150/600 0.430 — and past that the game stops reading the gesture at all, with
# 100/700 and 60/760 both leaving the camera exactly where it was. So the game
# caps what one pinch may do rather than scaling it to the distance, and the way
# to cover the range is to ask twice.
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
    swapped: bool,
    steps: int = PINCH_STEPS,
) -> list[tuple[int, int, int]]:
    """One two-finger gesture as the multi-touch events it is written with.

    Each finger is given as (start, end) in **screen** coordinates. **Whether
    the device's axes are the screen's swapped depends on the emulator**, which
    is what `swapped` says. MuMu's node reports x to 900 and y to 1600 against a
    1600x900 screen, so a point goes down as (y, x): measured by tapping
    (430, 990) through this path and watching the building at screen (990, 430)
    open. LDPlayer's reports x to 1600 and y to 900 and takes the point as it
    is; measured by zooming its village in and back out. `touch_devices` reads
    which from the node's own ranges.

    **`BTN_TOUCH` is not optional.** Without it the whole gesture is accepted,
    reported, and ignored — which is what a first attempt at this looked like,
    several times over, on all three of the device nodes MuMu publishes. Both
    tracking ids are cleared at the end for the matching reason: one left live
    holds the touch down, and the next gesture reads as one finger moving.
    """
    events: list[tuple[int, int, int]] = []

    def place(slot: int, point: tuple[int, int]) -> None:
        x, y = (point[1], point[0]) if swapped else point
        events.append((EV_ABS, ABS_MT_SLOT, slot))
        events.append((EV_ABS, ABS_MT_POSITION_X, x))
        events.append((EV_ABS, ABS_MT_POSITION_Y, y))

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


def gesture_script(events: list[tuple[int, int, int]], node: str, gap: float = PINCH_GAP) -> str:
    """An event stream as one shell command writing it to a device node.

    **The node is opened once for the whole gesture**, which is what this exists
    for. `sendevent` opens it per event, and measured on this emulator that is
    what a pinch was really paying: 40 `sendevent` cost 1.23 s against 0.08 s
    for 40 `true`, so 31 of the 33 ms an event cost was the open. Even one
    `> node` redirect per report still costs about 37 ms. Redirecting the whole
    group takes a 48-step gesture from 3.6 s to 0.28 s.

    **The pacing is the other half, and without it there is no gesture.** The
    stream written in one go arrives with one timestamp and the game keeps the
    scale it started at — measured, a batch that moved the camera not at all,
    0.199 to 0.195. A `sleep` between reports is what makes it a drag, and the
    game reads the same zoom at every gap from 0 up to 0.02, so `gap` buys how
    it looks rather than whether it works: MuMu draws a dot under every touch it
    receives, and a step every 14 ms at `PINCH_GAP` reads as a slide where one
    every 225 ms read as a series of jumps. The 0.28 s above is the ungapped
    figure, which is what the grouping bought; paced, the same gesture is 0.66 s.

    Split at `SYN_REPORT`, because that is the boundary the kernel hands a
    complete picture to the app at; pausing inside one would deliver half a
    gesture's fingers.
    """
    parts: list[str] = []
    report: list[tuple[int, int, int]] = []
    for event in events:
        report.append(event)
        if event[0] != EV_SYN:
            continue
        blob = base64.b64encode(b"".join(INPUT_EVENT.pack(0, 0, *e) for e in report)).decode()
        if parts and gap:
            parts.append(f"sleep {gap}")
        parts.append(f"echo {blob} | base64 -d")
        report = []
    return "{ " + " ; ".join(parts) + " ; } > " + node


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

        **No points is a no-op, and it used to be fatal.** Joining an empty list
        gives the empty string, so the call became `adb shell ""` — an
        interactive shell that never returns — while the timeout below evaluates
        to exactly its 15 second floor. Swept over every run recorded on this
        machine, seven `attack` runs died that way between 2026-09-13 and
        2026-09-15, each one 15 seconds after its own `Tapping 0 points` line,
        and each took the whole `--repeat 0` series with it: the exception
        escapes as `AdbControlError` with nothing above it to catch one, so
        `result.json` was never written and the rounds already played were
        countable only out of `run.log`. One of them had just won 100%.

        **The guard belongs here rather than at the call sites**, because
        arriving with nothing is legitimate: every recorded traceback came from
        `AttackRunner._act`'s `ability` step, which taps the card of each named
        hero that is actually on the field and so yields nothing at all when
        none of them landed. That is a round with no ability to fire, not a
        round that went wrong, and the same shape is reachable from other
        callers that build their list by filtering. The log line stays ahead of
        the return so `Tapping 0 points` survives as the signature to grep for.
        """
        logger.info(
            "Tapping %d points on %s display %s", len(points), self.serial, display.logical_id
        )
        if not points:
            return
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

    def touch_devices(self) -> dict[str, bool]:
        """Every multi-touch input node this device exposes, and whether its axes are swapped.

        `input` cannot do two fingers, so a pinch has to be written straight to
        the kernel — and that goes to a device node rather than to a display, so
        nothing routes it the way `input -d` is routed.

        Which node belongs to which display is what `touch_device_for` answers;
        this is the fallback for when that cannot be worked out, and sending to
        all of them is not harmless — see there.

        Swapped means the node's x range is shorter than its y range against a
        landscape screen, which is how MuMu builds its node and LDPlayer does
        not; `pinch_events` owns what that does to a point.

        An empty answer is a failure rather than a quiet nothing — a pinch with
        nowhere to send it would otherwise report a zoom that never happened.
        """
        nodes: dict[str, bool] = {}
        for block in self.shell("getevent -pl 2>/dev/null").split("add device ")[1:]:
            ranges = dict(re.findall(r"ABS_MT_POSITION_([XY])\s*:[^\n]*?max (\d+)", block))
            if not {"X", "Y"} <= ranges.keys():
                continue
            node = block.split(":", 1)[1].split()[0] if ":" in block else ""
            if node.startswith("/dev/input/"):
                nodes[node] = int(ranges["X"]) < int(ranges["Y"])
        logger.debug("Multi-touch nodes (node: axes swapped): %s", nodes)
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

        **More than one reader can carry that viewport**, and only a multi-touch
        node is an answer. LDPlayer binds a `VirtualMouse` to its one display
        ahead of the touchscreen, with `<virtual-mouse>` for a path, and taking
        the first reader aimed every gesture at that: a redirect the shell
        refuses and nothing reports.

        None rather than a guess when any hop is missing, because the caller's
        fallback — every node, then relaunch the game — is survivable, while a
        pinch aimed at the wrong display silently does nothing to the camera it
        was meant to fix.
        """
        dump = self.shell("dumpsys input 2>/dev/null")
        touch = self.touch_devices()
        for reader in re.finditer(
            rf"EventHub Devices:\s*\[\s*(\d+)[^\]]*\](?:(?!EventHub Devices).)*?"
            rf"uniqueId=local:{display.physical_id}\b",
            dump,
            re.S,
        ):
            path = re.search(
                rf"\n\s*{reader.group(1)}:[^\n]*\n(?:[^\n]*\n)*?\s*Path:\s*(\S+)", dump
            )
            if path is not None and path.group(1) in touch:
                logger.debug("Display %s is driven by %s", display.physical_id, path.group(1))
                return path.group(1)
        logger.debug("No multi-touch node is reported against display %s", display.physical_id)
        return None

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
        not free. Either way each node gets the stream its own axes read.
        """
        touch = self.touch_devices()
        targets = [node] if node else list(touch)
        if not targets:
            raise AdbControlError(f"{self.serial} 找不到任何多點觸控裝置，縮放送不出去")
        logger.info("Pinch %s %s and %s on %s", self.serial, first, second, targets)
        for target in targets:
            stream = pinch_events(first, second, touch[target], steps)
            self.shell(gesture_script(stream, target), timeout=30)

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
            time.sleep(RELAUNCH_SETTLE)

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
