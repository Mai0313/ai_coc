from __future__ import annotations

import os
import json
import time
from typing import Any
import winreg
import logging
from pathlib import Path
import subprocess

from .adb import AdbController, use_adb_executable
from .models import UiElement, EmulatorInstance, MuMuInstanceInfo
from .constants import COC_PACKAGE

logger = logging.getLogger(__name__)


class MuMuError(RuntimeError):
    pass


class MuMuAdapter:
    def __init__(self, install_root: Path | None = None) -> None:
        self.install_root = install_root or self.detect_install_path()
        self.cli = self.install_root / "nx_main" / "mumu-cli.exe"
        self.adb = self.install_root / "nx_main" / "adb.exe"
        if not self.cli.is_file():
            raise MuMuError(f"找不到 MuMu CLI：{self.cli}")
        if not self.adb.is_file():
            raise MuMuError(f"找不到 MuMu ADB：{self.adb}")
        use_adb_executable(self.adb)
        self._controllers: dict[str, AdbController] = {}
        logger.info("MuMu install root: %s", self.install_root)

    @staticmethod
    def detect_install_path() -> Path:
        candidates: list[Path] = []
        for hive, key in (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
            ),
        ):
            try:
                with winreg.OpenKey(hive, key) as parent:
                    for i in range(winreg.QueryInfoKey(parent)[0]):
                        try:
                            with winreg.OpenKey(parent, winreg.EnumKey(parent, i)) as child:
                                name = str(winreg.QueryValueEx(child, "DisplayName")[0])
                                if "MuMuPlayer" in name:
                                    candidates.append(
                                        Path(str(winreg.QueryValueEx(child, "InstallLocation")[0]))
                                    )
                        except OSError:
                            continue
            except OSError:
                pass
        candidates.extend([
            Path(r"C:\Program Files\Netease\MuMuPlayer"),
            Path(r"D:\Program Files\Netease\MuMuPlayer"),
        ])
        for candidate in candidates:
            if (candidate / "nx_main" / "mumu-cli.exe").is_file():
                return candidate
        raise MuMuError("找不到支援 mumu-cli 的 MuMuPlayer")

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
            result = subprocess.run(  # noqa: S603 - command is the located MuMu CLI or ADB plus literal arguments
                command,
                capture_output=True,
                timeout=timeout,
                creationflags=self._flags(),
                env=self._clean_environment(),
                # The exit code is turned into a MuMuError below, with the
                # emulator's own message attached.
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise MuMuError(f"命令逾時：{' '.join(command[1:])}") from exc
        if result.returncode != 0:
            message = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
            raise MuMuError(message or f"命令失敗 ({result.returncode})")
        return result.stdout

    def cli_json(self, *args: str, timeout: float = 15) -> dict[str, Any]:
        data = self._run([str(self.cli), *args], timeout)
        try:
            return json.loads(data.decode("utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise MuMuError(f"無法解析 MuMu CLI 回應：{data[:200]!r}") from exc

    def version(self) -> str:
        return str(self.cli_json("version").get("version", "unknown"))

    def controller(self, serial: str) -> AdbController:
        """One adbutils-backed controller per instance, keyed by its own ADB port."""
        if serial not in self._controllers:
            self._controllers[serial] = AdbController(serial)
        return self._controllers[serial]

    def enumerate_instances(self) -> list[EmulatorInstance]:
        raw = self.cli_json("info", "--vmindex", "all")
        instances: list[EmulatorInstance] = []
        for value in raw.values():
            if not isinstance(value, dict) or "index" not in value:
                continue
            info = MuMuInstanceInfo.model_validate(value)
            resolution = dpi = "unknown"
            coc_running = False
            if info.is_android_started and info.endpoint.ready:
                try:
                    adb = self.controller(info.endpoint.serial)
                    resolution, dpi = adb.screen_geometry()
                    coc_running = adb.is_running(COC_PACKAGE)
                except Exception:
                    logger.warning("Unable to inspect MuMu instance %s", info.index, exc_info=True)
            instances.append(
                EmulatorInstance(
                    emulator_id=f"mumu:{info.index}",
                    index=info.index,
                    name=info.name or f"MuMu {info.index}",
                    android_version=info.android_version,
                    adb_serial=info.endpoint.serial,
                    process_started=info.is_process_started,
                    android_started=info.is_android_started,
                    state=info.player_state,
                    pid=info.pid,
                    main_hwnd=info.hwnd("main_wnd"),
                    render_hwnd=info.hwnd("render_wnd"),
                    resolution=resolution,
                    dpi=dpi,
                    coc_running=coc_running,
                )
            )
        logger.info("MuMu reported %d instance(s)", len(instances))
        return sorted(instances, key=lambda item: item.index)

    def launch_instance(self, index: int) -> None:
        self.cli_json("control", "--vmindex", str(index), "launch", timeout=30)

    def restart_instance(self, index: int) -> None:
        self.cli_json("control", "--vmindex", str(index), "restart", timeout=30)

    def close_instance(self, index: int) -> None:
        self.cli_json("control", "--vmindex", str(index), "shutdown", timeout=30)

    def launch_coc(self, instance: EmulatorInstance) -> None:
        self.controller(instance.adb_serial).launch_app(COC_PACKAGE)

    def ensure_coc(self, index: int) -> EmulatorInstance:
        """Bring one MuMu instance to a running CoC screen, recovering stale launches."""
        logger.info("Ensuring Clash of Clans is running on MuMu instance %s", index)
        items = self.enumerate_instances()
        current = next((item for item in items if item.index == index), None)
        if current is None:
            raise MuMuError(f"找不到 MuMu instance {index}")
        if not current.android_started:
            self.launch_instance(index)
            for _ in range(18):
                time.sleep(2)
                current = next(
                    (item for item in self.enumerate_instances() if item.index == index), current
                )
                if current.android_started and current.endpoint.ready:
                    break
        self.launch_coc(current)
        for _ in range(5):
            time.sleep(2)
            current = next(
                (item for item in self.enumerate_instances() if item.index == index), current
            )
            if current.coc_running:
                return current
        # MuMu can report Android ready while the first monkey launch is ignored.
        logger.warning("Clash of Clans did not come up on %s; restarting the instance", index)
        self.restart_instance(index)
        for _ in range(18):
            time.sleep(2)
            current = next(
                (item for item in self.enumerate_instances() if item.index == index), current
            )
            if current.android_started and current.endpoint.ready:
                break
        self.launch_coc(current)
        time.sleep(3)
        refreshed = next(
            (item for item in self.enumerate_instances() if item.index == index), current
        )
        if not refreshed.coc_running:
            raise MuMuError("已重啟模擬器，但部落衝突仍未啟動")
        return refreshed

    def restart_coc(self, instance: EmulatorInstance) -> None:
        adb = self.controller(instance.adb_serial)
        adb.stop_app(COC_PACKAGE)
        adb.launch_app(COC_PACKAGE)

    def screenshot(self, instance: EmulatorInstance) -> bytes:
        return self.controller(instance.adb_serial).screenshot()

    def tap(self, instance: EmulatorInstance, x: int, y: int) -> None:
        self.controller(instance.adb_serial).tap(x, y)

    def swipe(
        self,
        instance: EmulatorInstance,
        start: tuple[int, int],
        end: tuple[int, int],
        duration_ms: int,
    ) -> None:
        self.controller(instance.adb_serial).swipe(start, end, duration_ms)

    def back(self, instance: EmulatorInstance) -> None:
        self.controller(instance.adb_serial).back()

    def ui_elements(self, instance: EmulatorInstance) -> list[UiElement]:
        return self.controller(instance.adb_serial).ui_elements()
