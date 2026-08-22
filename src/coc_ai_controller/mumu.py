from __future__ import annotations

import os
import re
import json
import time
from typing import Any
import winreg
import logging
from pathlib import Path
import subprocess

from defusedxml import ElementTree as ET  # noqa: N817 - the conventional alias for ElementTree

from .models import EmulatorInstance
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

    def adb_run(self, serial: str, *args: str, timeout: float = 12) -> bytes:
        return self._run([str(self.adb), "-s", serial, *args], timeout)

    def connect(self, serial: str) -> None:
        self._run([str(self.adb), "connect", serial], 12)
        state = self.adb_run(serial, "get-state", timeout=6).decode(errors="replace").strip()
        if state != "device":
            raise MuMuError(f"ADB 尚未就緒：{state}")

    def enumerate_instances(self) -> list[EmulatorInstance]:
        raw = self.cli_json("info", "--vmindex", "all")
        instances: list[EmulatorInstance] = []
        for value in raw.values():
            if not isinstance(value, dict) or "index" not in value:
                continue
            index = int(value["index"])
            serial = f"{value.get('adb_host_ip', '127.0.0.1')}:{value.get('adb_port', 0)}"
            resolution = dpi = "unknown"
            coc_running = False
            if value.get("is_android_started") and not serial.endswith(":0"):
                try:
                    self.connect(serial)
                    size = self.adb_run(serial, "shell", "wm", "size", timeout=5).decode(
                        errors="replace"
                    )
                    match = re.findall(r"(\d+)x(\d+)", size)
                    if match:
                        width, height = map(int, match[-1])
                        resolution = f"{max(width, height)}x{min(width, height)}"
                    density = self.adb_run(serial, "shell", "wm", "density", timeout=5).decode(
                        errors="replace"
                    )
                    match_dpi = re.findall(r"(\d+)", density)
                    if match_dpi:
                        dpi = match_dpi[-1]
                    coc_running = bool(
                        self.adb_run(serial, "shell", "pidof", COC_PACKAGE, timeout=5).strip()
                    )
                except Exception:
                    logger.debug("Unable to inspect MuMu instance %s", index, exc_info=True)
            instances.append(
                EmulatorInstance(
                    emulator_id=f"mumu:{index}",
                    index=index,
                    name=str(value.get("name", f"MuMu {index}")),
                    android_version=str(value.get("android_version", "unknown")),
                    adb_serial=serial,
                    process_started=bool(value.get("is_process_started")),
                    android_started=bool(value.get("is_android_started")),
                    state=str(value.get("player_state", "unknown")),
                    pid=int(value.get("pid", 0) or 0),
                    main_hwnd=int(str(value.get("main_wnd", "0")), 16),
                    render_hwnd=int(str(value.get("render_wnd", "0")), 16),
                    resolution=resolution,
                    dpi=dpi,
                    coc_running=coc_running,
                )
            )
        return sorted(instances, key=lambda item: item.index)

    def launch_instance(self, index: int) -> None:
        self.cli_json("control", "--vmindex", str(index), "launch", timeout=30)

    def restart_instance(self, index: int) -> None:
        self.cli_json("control", "--vmindex", str(index), "restart", timeout=30)

    def close_instance(self, index: int) -> None:
        self.cli_json("control", "--vmindex", str(index), "shutdown", timeout=30)

    def launch_coc(self, instance: EmulatorInstance) -> None:
        self.connect(instance.adb_serial)
        self.adb_run(
            instance.adb_serial,
            "shell",
            "monkey",
            "-p",
            COC_PACKAGE,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
            timeout=15,
        )

    def ensure_coc(self, index: int) -> EmulatorInstance:
        """Bring one MuMu instance to a running CoC screen, recovering stale launches."""
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
                if current.android_started and not current.adb_serial.endswith(":0"):
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
        self.restart_instance(index)
        for _ in range(18):
            time.sleep(2)
            current = next(
                (item for item in self.enumerate_instances() if item.index == index), current
            )
            if current.android_started and not current.adb_serial.endswith(":0"):
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
        self.connect(instance.adb_serial)
        self.adb_run(instance.adb_serial, "shell", "am", "force-stop", COC_PACKAGE, timeout=8)
        self.launch_coc(instance)

    def screenshot(self, instance: EmulatorInstance) -> bytes:
        self.connect(instance.adb_serial)
        data = self.adb_run(instance.adb_serial, "exec-out", "screencap", "-p", timeout=12)
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise MuMuError("MuMu 截圖不是有效 PNG")
        return data

    def tap(self, instance: EmulatorInstance, x: int, y: int) -> None:
        self.adb_run(
            instance.adb_serial, "shell", "input", "tap", str(int(x)), str(int(y)), timeout=5
        )

    def swipe(
        self,
        instance: EmulatorInstance,
        start: tuple[int, int],
        end: tuple[int, int],
        duration_ms: int,
    ) -> None:
        self.adb_run(
            instance.adb_serial,
            "shell",
            "input",
            "swipe",
            str(start[0]),
            str(start[1]),
            str(end[0]),
            str(end[1]),
            str(duration_ms),
            timeout=8,
        )

    def back(self, instance: EmulatorInstance) -> None:
        self.adb_run(instance.adb_serial, "shell", "input", "keyevent", "4", timeout=5)

    def ui_elements(self, instance: EmulatorInstance) -> list[dict[str, object]]:
        """Read Android's accessibility hierarchy without extra device agents."""
        self.connect(instance.adb_serial)
        elements: list[dict[str, object]] = []
        try:
            self.adb_run(
                instance.adb_serial,
                "shell",
                "uiautomator",
                "dump",
                "/sdcard/coc_ui.xml",
                timeout=8,
            )
        except MuMuError as exc:
            if "dumped to" not in str(exc).lower():
                return elements
        xml_data = self.adb_run(
            instance.adb_serial, "shell", "cat", "/sdcard/coc_ui.xml", timeout=5
        ).decode("utf-8", errors="replace")
        try:
            root = ET.fromstring(xml_data[xml_data.find("<?xml") :])
        except (ET.ParseError, ValueError):
            return elements
        for node in root.iter("node"):
            text = (node.get("text") or node.get("content-desc") or "").strip()
            bounds = node.get("bounds", "")
            match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
            if not match or not (text or node.get("clickable") == "true"):
                continue
            left, top, right, bottom = map(int, match.groups())
            elements.append({
                "text": text,
                "resource_id": node.get("resource-id", ""),
                "clickable": node.get("clickable") == "true",
                "x": (left + right) // 2,
                "y": (top + bottom) // 2,
            })
        return elements[:120]
