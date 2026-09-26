from __future__ import annotations

import os
import time
from typing import ClassVar
import winreg
import logging
from pathlib import Path
import subprocess

from pydantic import BaseModel, PrivateAttr

from ai_coc.models import AdbEndpoint, EmulatorInstance
from ai_coc.constants import COC_PACKAGE

from .adb import AdbController

logger = logging.getLogger(__name__)

# How long to give an instance to boot, and how often to look. MuMu reports
# Android ready a while before its ADB port is listening, so both are waited on.
BOOT_POLLS = 18
POLL_GAP = 2.0
# How long to give a launch to produce a process before the instance is
# restarted and the launch tried again, and how long the second launch gets.
LAUNCH_POLLS = 5
RELAUNCH_SETTLE = 3.0


class EmulatorError(RuntimeError):
    pass


class Emulator(BaseModel):
    """What every emulator does the same way: ADB for the game, its own CLI for its instances.

    A subclass names its CLI and answers the five questions only its CLI can:
    which instances exist, how to start, restart and stop one, and which version
    it is. Everything else goes through ADB, which is the same on all of them.

    **ADB is whichever adb adbutils finds, never the emulator's own**: the one on
    PATH, else the copy adbutils ships. Every adb talks to one server on port
    5037, so the binary only decides which build starts it. Measured on
    2026-09-26 with three builds installed (platform-tools 37.0.1, MuMu 36.0.0,
    LDPlayer 34.0.4): a server started by platform-tools outlived three LDPlayer
    restarts and a MuMu boot while LDPlayer ran its own adb against it, and a
    shell round trip (45 ms against 46) and a screencap (about 0.6 s) timed the
    same through it as through LDPlayer's.

    The cost, accepted: a PyInstaller build on a machine with no adb on PATH
    starts the server from the copy bundled inside itself, and the server
    outlives the app, so a `--onefile` run leaves its extraction folder behind
    and a `--onedir` folder cannot be replaced until `adb kill-server`.
    Installing platform-tools avoids both.
    """

    # Which emulator a message is about, since two of them can be installed.
    label: ClassVar[str]
    install_root: Path

    _controllers: dict[str, AdbController] = PrivateAttr(default_factory=dict)

    def model_post_init(self, context: object, /) -> None:
        if not self.cli.is_file():
            raise EmulatorError(f"找不到 {self.label} CLI：{self.cli}")
        logger.info("%s install root: %s", self.label, self.install_root)

    @property
    def cli(self) -> Path:
        raise NotImplementedError

    def version(self) -> str:
        raise NotImplementedError

    def enumerate_instances(self) -> list[EmulatorInstance]:
        raise NotImplementedError

    def serial_for(self, index: int) -> str:
        """The serial an instance answers on once it is up, which the emulator fixes by its index."""
        raise NotImplementedError

    def serial_of(self, instance: EmulatorInstance) -> str:
        """What the instance reports while it is up, and where it will listen while it is down."""
        return instance.adb_serial if instance.endpoint.ready else self.serial_for(instance.index)

    def launch_instance(self, index: int) -> None:
        raise NotImplementedError

    def restart_instance(self, index: int) -> None:
        raise NotImplementedError

    def close_instance(self, index: int) -> None:
        raise NotImplementedError

    @staticmethod
    def _flags() -> int:
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)

    @staticmethod
    def _clean_environment() -> dict[str, str]:
        # The packaged controller contains PyQt's Qt plugin paths. MuMu CLI is
        # a separate Qt application and must not inherit those paths.
        environment = os.environ.copy()
        for key in (
            "QT_PLUGIN_PATH",
            "QT_QPA_PLATFORM",
            "QT_QPA_PLATFORM_PLUGIN_PATH",
            "QML2_IMPORT_PATH",
        ):
            environment.pop(key, None)
        return environment

    def _run(self, command: list[str], timeout: float = 15) -> bytes:
        try:
            result = subprocess.run(  # noqa: S603 - command is the located emulator CLI plus literal arguments
                command,
                capture_output=True,
                timeout=timeout,
                creationflags=self._flags(),
                env=self._clean_environment(),
                # The exit code is turned into an EmulatorError below, with the
                # emulator's own message attached.
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise EmulatorError(f"命令逾時：{' '.join(command[1:])}") from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
            raise EmulatorError(message or f"命令失敗 ({result.returncode})")
        return result.stdout

    def controller(self, serial: str) -> AdbController:
        """One adbutils-backed controller per instance, keyed by its own ADB port."""
        if serial not in self._controllers:
            self._controllers[serial] = AdbController(endpoint=AdbEndpoint.parse(serial))
        return self._controllers[serial]

    def launch_coc(self, instance: EmulatorInstance) -> None:
        self.controller(instance.adb_serial).launch_app(COC_PACKAGE)

    def instance(self, index: int) -> EmulatorInstance | None:
        """One instance by index, or None while the emulator leaves it out of its listing.

        None is a real state rather than an error: MuMu drops an instance from
        the listing for a moment while it restarts, so a caller waiting on one
        keeps whatever it last saw rather than giving up.
        """
        return next((item for item in self.enumerate_instances() if item.index == index), None)

    def _booted(self, index: int, current: EmulatorInstance) -> EmulatorInstance:
        """Wait for the instance to report Android up with its ADB port listening."""
        for _ in range(BOOT_POLLS):
            time.sleep(POLL_GAP)
            current = self.instance(index) or current
            if current.android_started and current.endpoint.ready:
                break
        return current

    def ensure_coc(self, index: int) -> EmulatorInstance:
        """Bring one instance to a running CoC screen, recovering stale launches."""
        logger.info("Ensuring Clash of Clans is running on %s instance %s", self.label, index)
        current = self.instance(index)
        if current is None:
            raise EmulatorError(f"找不到 {self.label} instance {index}")
        if not current.android_started:
            self.launch_instance(index)
            current = self._booted(index, current)
        self.launch_coc(current)
        for _ in range(LAUNCH_POLLS):
            time.sleep(POLL_GAP)
            current = self.instance(index) or current
            if current.coc_running:
                return current
        # MuMu can report Android ready while the first launch is ignored.
        logger.warning("Clash of Clans did not come up on %s; restarting the instance", index)
        self.restart_instance(index)
        current = self._booted(index, current)
        self.launch_coc(current)
        time.sleep(RELAUNCH_SETTLE)
        refreshed = self.instance(index) or current
        if not refreshed.coc_running:
            raise EmulatorError("已重啟模擬器，但部落衝突仍未啟動")
        return refreshed

    def restart_coc(self, instance: EmulatorInstance) -> None:
        adb = self.controller(instance.adb_serial)
        adb.stop_app(COC_PACKAGE)
        adb.launch_app(COC_PACKAGE)

    def screenshot(self, instance: EmulatorInstance) -> bytes:
        adb = self.controller(instance.adb_serial)
        return adb.screenshot(adb.display_for(COC_PACKAGE))

    def tap(self, instance: EmulatorInstance, x: int, y: int) -> None:
        adb = self.controller(instance.adb_serial)
        adb.tap(x, y, adb.display_for(COC_PACKAGE))

    def swipe(
        self,
        instance: EmulatorInstance,
        start: tuple[int, int],
        end: tuple[int, int],
        duration_ms: int,
    ) -> None:
        adb = self.controller(instance.adb_serial)
        adb.swipe(start, end, duration_ms, adb.display_for(COC_PACKAGE))


def _value(key: winreg.HKEYType, field: str) -> str:
    try:
        return str(winreg.QueryValueEx(key, field)[0])
    except OSError:
        return ""


def uninstall_dirs(name: str) -> list[Path]:
    """The install folder of every program whose Windows uninstall entry carries `name`.

    Matched against the entry's key as well as its display name, because
    LDPlayer's display name is localised (雷電模擬器14) while its key is
    `LDPlayer14`. Its entry also leaves `InstallLocation` empty, so the folder
    holding the uninstaller stands in for it.
    """
    found: list[Path] = []
    for key in (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as parent:
                for i in range(winreg.QueryInfoKey(parent)[0]):
                    entry = winreg.EnumKey(parent, i)
                    try:
                        with winreg.OpenKey(parent, entry) as child:
                            if name not in entry and name not in _value(child, "DisplayName"):
                                continue
                            folder = _value(child, "InstallLocation")
                            uninstaller = _value(child, "UninstallString").strip('"')
                    except OSError:
                        continue
                    if folder:
                        found.append(Path(folder))
                    elif uninstaller:
                        found.append(Path(uninstaller).parent)
        except OSError:
            pass
    return found
