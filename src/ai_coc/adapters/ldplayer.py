from __future__ import annotations

import re
import time
from typing import ClassVar
import logging
from pathlib import Path

from pydantic import Field

from ai_coc.models import AdbEndpoint, EmulatorInstance, LDPlayerInstanceInfo
from ai_coc.constants import COC_PACKAGE

from .adb import AdbControlError
from .emulator import Emulator, EmulatorError, uninstall_dirs

logger = logging.getLogger(__name__)

# Where LDPlayer puts an instance's ADB port: 5555 for index 0 and 2 more for
# each index after it, the pair adb itself lists as `emulator-5554`. Measured on
# this machine's only instance, index 0: `ldconsole adb --index 0 --command
# get-serialno` answers `emulator-5554`, and `127.0.0.1:5555` connects to it.
FIRST_ADB_PORT = 5555
ADB_PORT_STEP = 2
# What `list2`'s Android field says, for the state a person reads in the picker.
STATES = {0: "stopped", 1: "running", 2: "starting"}
# How long a restart watches for the instance to go down and come back up.
SETTLE_POLLS = 40
SETTLE_GAP = 0.5
# How long a restart leaves the VM between `quit` and `launch`. One of its
# processes outlived `quit` by about five seconds, and a launch inside that left
# the player up with no VM behind it, stuck at 2 in `list2` for good.
QUIT_SETTLE = 10.0


def detect_install_path() -> Path:
    for candidate in uninstall_dirs("LDPlayer"):
        if (candidate / "ldconsole.exe").is_file():
            return candidate
    raise EmulatorError("找不到雷電模擬器")


class LDPlayerAdapter(Emulator):
    label: ClassVar[str] = "LDPlayer"
    install_root: Path = Field(default_factory=detect_install_path)

    @property
    def cli(self) -> Path:
        return self.install_root / "ldconsole.exe"

    def _console(self, *args: str, timeout: float = 15) -> str:
        # Titles come back in the system's code page rather than in UTF-8.
        return self._run([str(self.cli), *args], timeout).decode("mbcs", errors="replace")

    def version(self) -> str:
        """Off the banner `ldconsole` prints with no command, `ldplayer v14.0.29.0 ...`."""
        found = re.search(r"v(\d+(?:\.\d+)+)", self._console())
        return found.group(1) if found else "unknown"

    def serial_for(self, index: int) -> str:
        return f"127.0.0.1:{FIRST_ADB_PORT + ADB_PORT_STEP * index}"

    def enumerate_instances(self) -> list[EmulatorInstance]:
        instances: list[EmulatorInstance] = []
        for line in self._console("list2").splitlines():
            if not line.strip():
                continue
            info = LDPlayerInstanceInfo.parse(line)
            serial = self.serial_for(info.index)
            answered = coc_running = False
            # **Android says it is up well before it can start anything**:
            # watched through a boot, `list2` said 1 ten seconds before `adb`
            # answered at all, and `adb` answered a moment before the boot
            # completed — a game launched there came up and died. So the
            # serial goes out as port 0 until the boot has completed, which is
            # what MuMu's CLI reports in the same window and what `_booted`
            # waits on.
            #
            # **And `list2` can lose an instance that is up**, so ADB is asked
            # whatever it says. Seen twice on 2026-09-26: an instance that had
            # just farmed for an hour, and one opened from its icon minutes
            # earlier, both read `0,…,-1,-1` with the player and the VM running
            # and `adb` answering. Believing `list2` there had `ensure_coc`
            # launch an emulator that was already up, wait out the boot and
            # fail on port 0, without ever reaching the game.
            try:
                adb = self.controller(serial)
                answered = adb.shell(["getprop", "sys.boot_completed"], timeout=5).strip() == "1"
                coc_running = answered and adb.is_running(COC_PACKAGE)
            except AdbControlError:
                logger.debug("LDPlayer instance %s has no ADB bridge", info.index)
            if answered and not info.android_started:
                logger.warning(
                    "LDPlayer lists instance %s as down, but its ADB answers; driving it anyway",
                    info.index,
                )
            android = 1 if answered else info.android
            instances.append(
                EmulatorInstance(
                    emulator_id=f"ldplayer:{info.index}",
                    index=info.index,
                    name=info.title or f"LDPlayer {info.index}",
                    android_version="unknown",
                    adb_serial=serial if answered else AdbEndpoint().serial,
                    process_started=info.pid > 0,
                    android_started=android == 1,
                    state=STATES.get(android, "unknown"),
                    pid=info.pid,
                    main_hwnd=info.top_hwnd,
                    render_hwnd=info.bind_hwnd,
                    resolution=f"{info.width}x{info.height}",
                    dpi=str(info.dpi),
                    coc_running=coc_running,
                )
            )
        logger.info("LDPlayer reported %d instance(s)", len(instances))
        return sorted(instances, key=lambda item: item.index)

    def _running(self, index: int) -> bool:
        """Whether the player is up, which `isrunning` answers from the moment it starts booting."""
        return self._console("isrunning", "--index", str(index)).strip() == "running"

    def launch_instance(self, index: int) -> None:
        """Start the instance, unless it is already on its way up.

        `ensure_coc` asks for a launch whenever Android is not up, which takes
        in an instance still booting — straight after `restart_instance`, every
        time — and a second launch into one is not something to find out about
        on an emulator whose `reboot` already wipes its settings.
        """
        if self._running(index):
            logger.info("LDPlayer instance %s is already starting; not launching it twice", index)
            return
        self._console("launch", "--index", str(index), timeout=30)

    def restart_instance(self, index: int) -> None:
        """Quit, let the VM wind down, and launch, returning once the launch has taken.

        **Never `reboot`**: twice out of three it rewrote the instance's settings
        file to LDPlayer's defaults, which puts the instance at 1920x1080 and
        turns local ADB debugging off — nothing here is measured at the one and
        nothing here can reach the other. `quit` and `launch` left the file
        alone every time.

        `isrunning` still says stop straight after a `launch` and running two
        seconds later, which is the window `ensure_coc` would launch into
        again, so this returns only once it has closed.
        """
        self._console("quit", "--index", str(index), timeout=30)
        self._settle(index, running=False)
        time.sleep(QUIT_SETTLE)
        self._console("launch", "--index", str(index), timeout=30)
        self._settle(index, running=True)

    def _settle(self, index: int, *, running: bool) -> None:
        for _ in range(SETTLE_POLLS):
            if self._running(index) is running:
                return
            time.sleep(SETTLE_GAP)
        logger.warning("LDPlayer instance %s never reported running=%s", index, running)

    def close_instance(self, index: int) -> None:
        self._console("quit", "--index", str(index), timeout=30)
