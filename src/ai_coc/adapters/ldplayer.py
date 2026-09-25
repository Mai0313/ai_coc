from __future__ import annotations

import re
from typing import ClassVar
import logging
from pathlib import Path

from pydantic import Field

from ai_coc.models import EmulatorInstance, LDPlayerInstanceInfo
from ai_coc.constants import COC_PACKAGE

from .emulator import Emulator, EmulatorError, uninstall_dirs

logger = logging.getLogger(__name__)

# Where LDPlayer puts an instance's ADB port: 5555 for index 0 and 2 more for
# each index after it, the pair adb itself lists as `emulator-5554`. Measured on
# this machine's only instance, index 0: `ldconsole adb --index 0 --command
# get-serialno` answers `emulator-5554`, and `127.0.0.1:5555` connects to it.
FIRST_ADB_PORT = 5555
ADB_PORT_STEP = 2


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

    @property
    def adb(self) -> Path:
        return self.install_root / "adb.exe"

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
            coc_running = False
            if info.android_started:
                try:
                    coc_running = self.controller(serial).is_running(COC_PACKAGE)
                except Exception:
                    logger.warning(
                        "Unable to inspect LDPlayer instance %s", info.index, exc_info=True
                    )
            instances.append(
                EmulatorInstance(
                    emulator_id=f"ldplayer:{info.index}",
                    index=info.index,
                    name=info.title or f"LDPlayer {info.index}",
                    android_version="unknown",
                    adb_serial=serial,
                    process_started=info.pid > 0,
                    android_started=info.android_started,
                    state="running" if info.android_started else "stopped",
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

    def launch_instance(self, index: int) -> None:
        self._console("launch", "--index", str(index), timeout=30)

    def restart_instance(self, index: int) -> None:
        self._console("reboot", "--index", str(index), timeout=30)

    def close_instance(self, index: int) -> None:
        self._console("quit", "--index", str(index), timeout=30)
