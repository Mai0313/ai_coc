from __future__ import annotations

from typing import TypeVar, ClassVar
import logging
from pathlib import Path

from pydantic import Field, BaseModel

from ai_coc.models import MuMuCliResult, MuMuCliVersion, EmulatorInstance, MuMuInstanceTable
from ai_coc.constants import COC_PACKAGE

from .emulator import Emulator, EmulatorError, uninstall_dirs

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Where MuMu 12 puts an instance's ADB port: 16384 for index 0 and 32 more for
# each index after it. Measured on this machine's only instance, index 3: the
# CLI and its own `vm_config.json` both say 16480. Only used while an instance
# is down, since the CLI reports port 0 until the bridge is listening.
FIRST_ADB_PORT = 16384
ADB_PORT_STEP = 32


def detect_install_path() -> Path:
    candidates = uninstall_dirs("MuMuPlayer")
    candidates.extend([
        Path(r"C:\Program Files\Netease\MuMuPlayer"),
        Path(r"D:\Program Files\Netease\MuMuPlayer"),
    ])
    for candidate in candidates:
        if (candidate / "nx_main" / "mumu-cli.exe").is_file():
            return candidate
    raise EmulatorError("找不到支援 mumu-cli 的 MuMuPlayer")


class MuMuAdapter(Emulator):
    label: ClassVar[str] = "MuMu"
    install_root: Path = Field(default_factory=detect_install_path)

    @property
    def cli(self) -> Path:
        return self.install_root / "nx_main" / "mumu-cli.exe"

    def cli_model(self, model: type[T], *args: str, timeout: float = 15) -> T:
        """Run one CLI command and validate its JSON answer into `model`."""
        data = self._run([str(self.cli), *args], timeout)
        try:
            return model.model_validate_json(data.decode("utf-8-sig"))
        except ValueError as exc:
            raise EmulatorError(f"無法解析 MuMu CLI 回應：{data[:200]!r}") from exc

    def version(self) -> str:
        return self.cli_model(MuMuCliVersion, "version").version

    def serial_for(self, index: int) -> str:
        return f"127.0.0.1:{FIRST_ADB_PORT + ADB_PORT_STEP * index}"

    def enumerate_instances(self) -> list[EmulatorInstance]:
        table = self.cli_model(MuMuInstanceTable, "info", "--vmindex", "all")
        instances: list[EmulatorInstance] = []
        for info in table.instances():
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
        self.cli_model(MuMuCliResult, "control", "--vmindex", str(index), "launch", timeout=30)

    def restart_instance(self, index: int) -> None:
        self.cli_model(MuMuCliResult, "control", "--vmindex", str(index), "restart", timeout=30)

    def close_instance(self, index: int) -> None:
        self.cli_model(MuMuCliResult, "control", "--vmindex", str(index), "shutdown", timeout=30)
