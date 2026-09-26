"""The adapters: what each one makes of what the emulator, the OS and the network hand it.

Every call that would reach a real device is patched at the subprocess or
adbutils seam, and the canned text it is fed is the shape the real tools
print. The DPAPI round trip is the one exception: it runs against the real
API, on the one platform this project runs on.
"""

from __future__ import annotations

import os
import sys
import base64
import ctypes
from ctypes import wintypes
from pathlib import Path
import tempfile
import unittest
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from ai_coc.models import (
    TouchNode,
    AdbEndpoint,
    DisplayTarget,
    EmulatorInstance,
    MuMuInstanceTable,
)
from ai_coc.adapters import adb as adb_module
from ai_coc.adapters import mapping, secrets, clipboard
from ai_coc.adapters import emulator as emulator_module
from ai_coc.adapters import ldplayer as ldplayer_module
from ai_coc.constants import COC_PACKAGE
from ai_coc.adapters.adb import AdbController, AdbControlError
from ai_coc.adapters.mumu import MuMuAdapter
from ai_coc.adapters.secrets import SecretStore
from ai_coc.adapters.emulator import EmulatorError
from ai_coc.adapters.ldplayer import LDPlayerAdapter

DISPLAY = DisplayTarget(logical_id="2", physical_id="4619827767814508545")
# What `touch_devices` makes of MuMu's two touch nodes: both report the screen's
# axes swapped.
MUMU_NODE = TouchNode(swapped=True, pressure=False)
MUMU_TOUCH = {"/dev/input/event8": MUMU_NODE, "/dev/input/event9": MUMU_NODE}

# `getevent -pl` on a MuMu instance: one keyboard node and one multi-touch node.
GETEVENT = """add device 1: /dev/input/event3
  name:     "mumu_keyboard"
  events:
    KEY (0001): 0001  0002  0003
add device 2: /dev/input/event8
  name:     "mumu_touch"
  events:
    ABS (0003): ABS_MT_SLOT           : value 0, min 0, max 9
                ABS_MT_POSITION_X     : value 0, min 0, max 899
                ABS_MT_POSITION_Y     : value 0, min 0, max 1599
                ABS_MT_PRESSURE       : value 0, min 0, max 0
"""

# `dumpsys input`, trimmed to the three hops `touch_device_for` walks: the
# reader device whose viewport names the display, its EventHub id, and the
# EventHub entry that carries the node's path.
DUMPSYS_INPUT = """INPUT MANAGER (dumpsys input)

Input Reader State:
  Device 8: mumu_touch_1
    IsExternal: false
    EventHub Devices: [ 8 ]
    Touch Input Mapper (mode: DIRECT):
      Viewport INTERNAL: displayId=2, uniqueId=local:4619827767814508545, port=0
  Device 9: mumu_touch_2
    IsExternal: false
    EventHub Devices: [ 9 ]
    Touch Input Mapper (mode: DIRECT):
      Viewport INTERNAL: displayId=3, uniqueId=local:4619826888814064386, port=0

Event Hub State:
  Devices:
    8: mumu_touch_1
      Classes: 0x00000014
      Path: /dev/input/event8
    9: mumu_touch_2
      Classes: 0x00000014
      Path: /dev/input/event9
"""

# The same two, trimmed from LDPlayer 14: its multi-touch node reports the
# screen's own axes, and a `VirtualMouse` whose path is not a node is bound to
# the one display ahead of the touchscreen.
LD_GETEVENT = """add device 1: /dev/input/event6
  name:     "mouse"
add device 3: /dev/input/event4
  name:     "input"
  events:
    ABS (0003): ABS_MT_SLOT           : value 0, min 0, max 15, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_X     : value 0, min 0, max 1599, fuzz 0, flat 0, resolution 0
                ABS_MT_POSITION_Y     : value 0, min 0, max 899, fuzz 0, flat 0, resolution 0
                ABS_MT_TRACKING_ID    : value 0, min 0, max 65535, fuzz 0, flat 0, resolution 0
                ABS_MT_PRESSURE       : value 0, min 0, max 2, fuzz 0, flat 0, resolution 0
"""

LD_DUMPSYS_INPUT = """INPUT MANAGER (dumpsys input)

Event Hub State:
  Devices:
    3: input
      Classes: KEYBOARD | TOUCH | TOUCH_MT
      Path: /dev/input/event4
    8: VirtualMouse
      Classes: CURSOR | VIRTUAL
      Path: <virtual-mouse>

Input Reader State (Nums of device: 9):
  Device 2: VirtualMouse
    EventHub Devices: [ 8 ]
    Sources: MOUSE
    Touch Input Mapper (mode - DISABLED):
      Viewport INTERNAL: displayId=0, uniqueId=local:0, port=0, orientation=0
  Device 7: input
    EventHub Devices: [ 3 ]
    Sources: KEYBOARD | TOUCHSCREEN
    Touch Input Mapper (mode - DIRECT):
      Viewport INTERNAL: displayId=0, uniqueId=local:0, port=0, orientation=0
"""

WINDOW_DISPLAYS = """WINDOW MANAGER DISPLAY CONTENTS (dumpsys window displays)
  Display: mDisplayId=0 (organized)
    mCurrentFocus=null
    mFocusedApp=ActivityRecord{202723142 u0 app.lawnchair/.LawnchairLauncher t2}
  Display: mDisplayId=2 (organized)
    mCurrentFocus=Window{964a007 u0 com.supercell.clashofclans/com.supercell.titan.GameApp}
    mFocusedApp=ActivityRecord{14144584 u0 com.supercell.clashofclans/com.supercell.titan.GameApp}
"""

DISPLAY_DEVICES = """Display Devices: size=2
  DisplayDeviceInfo{"mumuscreen000": uniqueId="local:4619827820427265280", 1080 x 1920}
  DisplayDeviceInfo{"mumuscreen001": uniqueId="local:4619827767814508545", 1080 x 1920}
Logical Displays: size=2
  mDisplayId=0
    mBaseDisplayInfo=DisplayInfo{"mumuscreen000", displayId 0, displayGroupId 0}
  mDisplayId=2
    mBaseDisplayInfo=DisplayInfo{"mumuscreen001", displayId 2, displayGroupId 0}
"""


def _controller() -> AdbController:
    return AdbController(endpoint=AdbEndpoint(port=16384))


class AdbConnectionTests(unittest.TestCase):
    def test_an_instance_whose_port_is_not_up_yet_cannot_be_connected(self) -> None:
        with pytest.raises(AdbControlError, match="尚未開放"):
            AdbController(endpoint=AdbEndpoint()).connect()

    def test_a_device_that_is_not_in_the_device_state_is_not_ready(self) -> None:
        device = MagicMock()
        device.get_state.return_value = "offline"
        with (
            patch.object(adb_module.adbutils.adb, "connect"),
            patch.object(adb_module.adbutils.adb, "device", return_value=device),
            pytest.raises(AdbControlError, match="尚未就緒"),
        ):
            _controller().connect()

    def test_a_device_the_server_already_holds_is_not_connected_again(self) -> None:
        """Each connect opens a connection to adbd, and LDPlayer's NAT died under them."""
        device = MagicMock()
        device.get_state.return_value = "device"
        with (
            patch.object(adb_module.adbutils.adb, "connect") as connected,
            patch.object(adb_module.adbutils.adb, "device", return_value=device),
        ):
            assert _controller().connect() is device
        connected.assert_not_called()

    def test_a_device_the_server_lost_is_connected_again(self) -> None:
        """A restarted instance drops the old handle."""
        device = MagicMock()
        device.get_state.side_effect = [adb_module.adbutils.AdbError("not found"), "device"]
        with (
            patch.object(adb_module.adbutils.adb, "connect") as connected,
            patch.object(adb_module.adbutils.adb, "device", return_value=device),
        ):
            assert _controller().connect() is device
        connected.assert_called_once()

    def test_an_adb_failure_is_reported_with_the_command_that_failed(self) -> None:
        device = MagicMock()
        device.shell.side_effect = adb_module.adbutils.AdbError("closed")
        with (
            patch.object(AdbController, "connect", return_value=device),
            pytest.raises(AdbControlError, match="pidof"),
        ):
            _controller().shell(["pidof", COC_PACKAGE])

    def test_a_screenshot_that_is_not_a_png_is_an_error_rather_than_a_frame(self) -> None:
        """Unqualified, screencap prefixes the PNG with a multi-display warning."""
        device = MagicMock()
        device.shell.return_value = b"WARNING: multiple displays\x89PNG"
        with (
            patch.object(AdbController, "connect", return_value=device),
            pytest.raises(AdbControlError, match="截圖失敗"),
        ):
            _controller().screenshot(DISPLAY)
        device.shell.return_value = b"\x89PNG\r\n..."
        with patch.object(AdbController, "connect", return_value=device):
            assert _controller().screenshot(DISPLAY) == b"\x89PNG\r\n..."
        assert device.shell.call_args.args[0] == ["screencap", "-p", "-d", DISPLAY.physical_id]

    def test_every_input_names_the_games_own_display(self) -> None:
        with patch.object(AdbController, "shell") as shell:
            adb = _controller()
            adb.tap(10, 20, DISPLAY)
            adb.back(DISPLAY)
            adb.swipe((1, 2), (3, 4), 350, DISPLAY)
        assert [call.args[0] for call in shell.call_args_list] == [
            ["input", "-d", "2", "tap", "10", "20"],
            ["input", "-d", "2", "keyevent", "BACK"],
            ["input", "-d", "2", "swipe", "1", "2", "3", "4", "350"],
        ]

    def test_a_burst_of_taps_is_one_shell_string(self) -> None:
        """A list would have every argument escaped, and the burst becomes one unknown command."""
        with patch.object(AdbController, "shell") as shell:
            _controller().tap_many([(1, 2), (3, 4)], DISPLAY, gap=0.1)
        command = shell.call_args.args[0]
        assert isinstance(command, str)
        assert command == "input -d 2 tap 1 2;sleep 0.1;input -d 2 tap 3 4"

    def test_a_burst_of_no_taps_sends_nothing_at_all(self) -> None:
        """An empty join is `adb shell ""`, an interactive shell that never returns.

        Seven recorded `attack` runs died on this, each 15 seconds after its own
        `Tapping 0 points` line, taking the whole series and its `result.json`.
        """
        with patch.object(AdbController, "shell") as shell:
            _controller().tap_many([], DISPLAY)
        shell.assert_not_called()

    def test_the_display_is_found_through_both_id_schemes(self) -> None:
        with patch.object(AdbController, "shell", side_effect=[WINDOW_DISPLAYS, DISPLAY_DEVICES]):
            assert _controller().display_for(COC_PACKAGE) == DISPLAY

    def test_a_package_with_no_window_has_no_display(self) -> None:
        with (
            patch.object(AdbController, "shell", return_value=WINDOW_DISPLAYS),
            pytest.raises(AdbControlError, match="沒有出現"),
        ):
            _controller().display_for("com.example.absent")

    def test_the_geometry_is_reported_landscape_whatever_the_device_says(self) -> None:
        """`wm size` reports 900x1600 while the screenshot arrives as 1600x900."""
        device = MagicMock()
        device.window_size.return_value = MagicMock(width=900, height=1600)
        with (
            patch.object(AdbController, "connect", return_value=device),
            patch.object(AdbController, "shell", return_value="Physical density: 240\n"),
        ):
            assert _controller().screen_geometry() == ("1600x900", "240")

    def test_the_game_is_launched_by_its_resolved_component(self) -> None:
        """LDPlayer 14's image ships no `monkey`, which is what this used to call."""
        resolved = (
            "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=false\n"
            "com.supercell.clashofclans/com.supercell.titan.GameApp\n"
        )
        with patch.object(AdbController, "shell", side_effect=[resolved, ""]) as shell:
            _controller().launch_app(COC_PACKAGE)
        assert shell.call_args.args[0] == [
            "am",
            "start",
            "-n",
            "com.supercell.clashofclans/com.supercell.titan.GameApp",
        ]
        with (
            patch.object(AdbController, "shell", return_value="No activity found\n"),
            pytest.raises(AdbControlError, match="找不到可以啟動"),
        ):
            _controller().launch_app(COC_PACKAGE)

    def test_running_is_a_pid_and_nothing_else(self) -> None:
        with patch.object(AdbController, "shell", return_value="1234\n"):
            assert _controller().is_running(COC_PACKAGE)
        with patch.object(AdbController, "shell", return_value="\n"):
            assert not _controller().is_running(COC_PACKAGE)


class TouchNodeTests(unittest.TestCase):
    """Finding the one input node behind the game's display, which `input` cannot route to."""

    def test_only_the_multi_touch_nodes_are_listed(self) -> None:
        with patch.object(AdbController, "shell", return_value=GETEVENT):
            assert _controller().touch_devices() == {"/dev/input/event8": MUMU_NODE}

    def test_the_node_is_resolved_through_the_three_hops_dumpsys_prints(self) -> None:
        with (
            patch.object(AdbController, "shell", return_value=DUMPSYS_INPUT),
            patch.object(AdbController, "touch_devices", return_value=MUMU_TOUCH),
        ):
            adb = _controller()
            assert adb.touch_device_for(DISPLAY) == "/dev/input/event8"
            other = DisplayTarget(logical_id="3", physical_id="4619826888814064386")
            assert adb.touch_device_for(other) == "/dev/input/event9"

    def test_a_display_no_reader_reports_answers_nothing_rather_than_a_guess(self) -> None:
        with (
            patch.object(AdbController, "shell", return_value=DUMPSYS_INPUT),
            patch.object(AdbController, "touch_devices", return_value=MUMU_TOUCH),
        ):
            stranger = DisplayTarget(logical_id="7", physical_id="1")
            assert _controller().touch_device_for(stranger) is None

    def test_ldplayers_node_keeps_the_screens_axes(self) -> None:
        with patch.object(AdbController, "shell", return_value=LD_GETEVENT):
            assert _controller().touch_devices() == {
                "/dev/input/event4": TouchNode(swapped=False, pressure=True)
            }

    def test_a_reader_whose_path_is_no_touch_node_is_passed_over(self) -> None:
        """LDPlayer binds its `VirtualMouse` to the display first, and a gesture
        written to `<virtual-mouse>` is a redirect the shell refuses silently.
        """
        with patch.object(AdbController, "shell", side_effect=[LD_DUMPSYS_INPUT, LD_GETEVENT]):
            only = DisplayTarget(logical_id="0", physical_id="0")
            assert _controller().touch_device_for(only) == "/dev/input/event4"

    def test_each_node_gets_the_stream_its_own_axes_read(self) -> None:
        with (
            patch.object(
                AdbController,
                "touch_devices",
                return_value={
                    "/dev/input/event8": MUMU_NODE,
                    "/dev/input/event4": TouchNode(swapped=False, pressure=True),
                },
            ),
            patch.object(AdbController, "shell"),
            patch.object(adb_module, "gesture_script", return_value="") as scripted,
        ):
            _controller().pinch(((500, 450), (600, 450)), ((1100, 450), (1000, 450)), steps=1)
        swapped, straight = (call.args[0] for call in scripted.call_args_list)
        assert (adb_module.EV_ABS, adb_module.ABS_MT_POSITION_X, 450) in swapped
        assert (adb_module.EV_ABS, adb_module.ABS_MT_POSITION_X, 500) in straight
        # A finger on a node with a pressure axis has to press, or Android
        # reads it as a hover and the game never sees it.
        pressed = (adb_module.EV_ABS, adb_module.ABS_MT_PRESSURE, 1)
        assert pressed not in swapped
        assert straight.count(pressed) == 2

    def test_a_pinch_with_nowhere_to_send_it_is_an_error(self) -> None:
        with (
            patch.object(AdbController, "touch_devices", return_value={}),
            pytest.raises(AdbControlError, match="多點觸控"),
        ):
            _controller().pinch(((1, 1), (2, 2)), ((3, 3), (4, 4)))

    def test_a_pinch_goes_to_every_node_when_none_is_named(self) -> None:
        with (
            patch.object(AdbController, "touch_devices", return_value=MUMU_TOUCH),
            patch.object(AdbController, "shell") as shell,
        ):
            _controller().pinch(((1, 1), (2, 2)), ((3, 3), (4, 4)), steps=1)
        assert shell.call_count == 2
        assert [call.args[0].rsplit(" > ", 1)[-1] for call in shell.call_args_list] == [
            "/dev/input/event8",
            "/dev/input/event9",
        ]

    def test_a_gesture_opens_its_node_once_and_paces_itself_between_reports(self) -> None:
        """The open is what `sendevent` was really costing: 31 of the 33 ms an event took."""
        script = adb_module.gesture_script(
            adb_module.pinch_events(((0, 0), (10, 0)), ((100, 0), (90, 0)), MUMU_NODE, steps=4),
            "/dev/input/event8",
            gap=0.02,
        )
        # One redirect for the whole gesture, and a pause between every report
        # but never inside one: half a gesture's fingers is not a moment the
        # game should ever be handed.
        assert script.count("> /dev/input/event8") == 1
        assert script.count("sleep 0.02") == script.count("base64 -d") - 1

    def test_the_events_go_out_as_the_kernel_lays_them_out(self) -> None:
        """24 bytes each, and what comes back out is what `pinch_events` asked for."""
        events = adb_module.pinch_events(
            ((0, 0), (10, 0)), ((100, 0), (90, 0)), MUMU_NODE, steps=2
        )
        script = adb_module.gesture_script(events, "/dev/input/event8", gap=0)
        packed = b"".join(
            base64.b64decode(part.split("echo ", 1)[1].split(" |", 1)[0])
            for part in script.split(" ; ")
            if "echo " in part
        )
        assert len(packed) == len(events) * 24
        assert [
            tuple(adb_module.INPUT_EVENT.unpack_from(packed, at)[2:])
            for at in range(0, len(packed), 24)
        ] == events

    def test_an_aimed_zoom_never_relaunches_the_game(self) -> None:
        """Nothing else saw the gesture, so there is nothing to bring back to the front."""
        with (
            patch.object(AdbController, "touch_device_for", return_value="/dev/input/event8"),
            patch.object(AdbController, "pinch") as pinched,
            patch.object(AdbController, "launch_app") as launched,
            patch.object(adb_module.time, "sleep"),
        ):
            _controller().zoom("out", 2, COC_PACKAGE, DISPLAY)
        assert pinched.call_count == 2
        assert pinched.call_args.kwargs["node"] == "/dev/input/event8"
        launched.assert_not_called()

    def test_a_zoom_that_had_to_go_everywhere_brings_the_game_back_afterwards(self) -> None:
        """Two fingers on the launcher MuMu keeps on its other displays switch away from the game."""
        with (
            patch.object(AdbController, "touch_device_for", return_value=None),
            patch.object(AdbController, "pinch") as pinched,
            patch.object(AdbController, "launch_app") as launched,
            patch.object(adb_module.time, "sleep"),
        ):
            _controller().zoom("in", 1, COC_PACKAGE, DISPLAY)
        assert pinched.call_args.kwargs["node"] is None
        launched.assert_called_once_with(COC_PACKAGE)

    def test_zooming_in_and_out_move_the_fingers_opposite_ways(self) -> None:
        with (
            patch.object(AdbController, "pinch") as pinched,
            patch.object(adb_module.time, "sleep"),
        ):
            adb = _controller()
            adb.zoom("out")
            (out_first, _), *_ = pinched.call_args.args
            adb.zoom("in")
            (in_first, _), *_ = pinched.call_args.args
        # Out: fingers converge on the middle. In: they start there and spread.
        assert out_first[0] < in_first[0]


def _instance(**fields: object) -> EmulatorInstance:
    base: dict[str, object] = {
        "emulator_id": "mumu:0",
        "index": 0,
        "name": "MuMu 0",
        "android_version": "12",
        "adb_serial": "127.0.0.1:16384",
        "process_started": True,
        "android_started": True,
        "state": "running",
        "pid": 1,
        "main_hwnd": 0,
        "render_hwnd": 0,
    }
    base.update(fields)
    return EmulatorInstance.model_validate(base)


class MuMuAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        (root / "nx_main").mkdir()
        (root / "nx_main" / "mumu-cli.exe").write_bytes(b"")
        (root / "nx_main" / "adb.exe").write_bytes(b"")
        patcher = patch.object(emulator_module, "use_adb_executable")
        patcher.start()
        self.addCleanup(patcher.stop)
        sleeping = patch.object(emulator_module.time, "sleep")
        sleeping.start()
        self.addCleanup(sleeping.stop)
        self.root = root
        self.mumu = MuMuAdapter(install_root=root)

    def test_an_install_without_the_cli_is_refused(self) -> None:
        (self.root / "nx_main" / "mumu-cli.exe").unlink()
        with pytest.raises(EmulatorError, match="MuMu CLI"):
            MuMuAdapter(install_root=self.root)

    def test_the_cli_never_inherits_the_bundled_qt_paths(self) -> None:
        """MuMu CLI is a Qt application of its own, and the packaged controller's plugin paths break it."""
        with patch.dict(os.environ, {"QT_PLUGIN_PATH": "C:/bundle", "OTHER": "kept"}):
            environment = MuMuAdapter._clean_environment()
        assert "QT_PLUGIN_PATH" not in environment
        assert environment["OTHER"] == "kept"

    def test_a_failed_command_carries_the_emulators_own_message(self) -> None:
        with (
            patch.object(
                emulator_module.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"boom"),
            ),
            pytest.raises(EmulatorError, match="boom"),
        ):
            self.mumu._run(["mumu-cli.exe", "version"])
        with (
            patch.object(
                emulator_module.subprocess, "run", side_effect=subprocess.TimeoutExpired("cli", 1)
            ),
            pytest.raises(EmulatorError, match="逾時"),
        ):
            self.mumu._run(["mumu-cli.exe", "version"])

    def test_the_cli_answer_is_read_through_its_byte_order_mark(self) -> None:
        with patch.object(MuMuAdapter, "_run", return_value=b'\xef\xbb\xbf{"version": "4.1"}'):
            assert self.mumu.version() == "4.1"
        with (
            patch.object(MuMuAdapter, "_run", return_value=b"not json"),
            pytest.raises(EmulatorError, match="無法解析"),
        ):
            self.mumu.version()

    def test_enumerating_inspects_only_the_instances_that_are_up(self) -> None:
        table = MuMuInstanceTable.model_validate({
            "1": {"index": 1, "is_android_started": False},
            "0": {
                "index": 0,
                "name": "A",
                "adb_port": 16384,
                "is_android_started": True,
                "is_process_started": True,
                "main_wnd": "1A",
            },
            "errcode": 0,
        })
        with (
            patch.object(MuMuAdapter, "cli_model", return_value=table),
            patch.object(AdbController, "screen_geometry", return_value=("1600x900", "240")),
            patch.object(AdbController, "is_running", return_value=True),
        ):
            first, second = self.mumu.enumerate_instances()
        assert (first.index, first.name, first.resolution, first.coc_running) == (
            0,
            "A",
            "1600x900",
            True,
        )
        assert first.main_hwnd == 26
        assert (second.index, second.name, second.resolution, second.coc_running) == (
            1,
            "MuMu 1",
            "unknown",
            False,
        )

    def test_an_instance_is_looked_up_by_index_and_absent_when_mumu_drops_it(self) -> None:
        with patch.object(
            MuMuAdapter,
            "enumerate_instances",
            return_value=[_instance(index=0), _instance(index=1)],
        ):
            assert self.mumu.instance(1) == _instance(index=1)
            assert self.mumu.instance(5) is None

    def _ensure(
        self, seen: list[EmulatorInstance | None], repeat: EmulatorInstance | None = None
    ) -> tuple[EmulatorInstance | None, MagicMock, MagicMock, MagicMock]:
        """Run `ensure_coc` against a sequence of listings, then `repeat` forever."""
        answers = iter(seen)
        with (
            patch.object(
                MuMuAdapter, "instance", side_effect=lambda _index: next(answers, repeat)
            ),
            patch.object(MuMuAdapter, "launch_instance") as booted,
            patch.object(MuMuAdapter, "launch_coc") as launched,
            patch.object(MuMuAdapter, "restart_instance") as restarted,
        ):
            try:
                result = self.mumu.ensure_coc(0)
            except EmulatorError:
                result = None
        return result, booted, launched, restarted

    def test_a_game_already_running_costs_one_launch_and_no_boot(self) -> None:
        up = _instance(coc_running=True)
        result, booted, launched, restarted = self._ensure([_instance(), up])
        assert result == up
        booted.assert_not_called()
        launched.assert_called_once()
        restarted.assert_not_called()

    def test_a_cold_instance_is_booted_before_the_game_is_launched(self) -> None:
        cold = _instance(android_started=False, adb_serial="127.0.0.1:0")
        up = _instance(coc_running=True)
        result, booted, launched, _ = self._ensure([cold, _instance(), up])
        assert result == up
        booted.assert_called_once_with(0)
        launched.assert_called_once()

    def test_a_launch_that_never_sticks_restarts_the_instance_and_tries_once_more(self) -> None:
        """MuMu can report Android ready while the first launch is ignored."""
        stuck = _instance(coc_running=False)
        result, _, launched, restarted = self._ensure([], repeat=stuck)
        assert result is None
        restarted.assert_called_once_with(0)
        assert launched.call_count == 2

    def test_an_instance_mumu_cannot_find_is_an_error(self) -> None:
        with (
            patch.object(MuMuAdapter, "instance", return_value=None),
            pytest.raises(EmulatorError, match="找不到"),
        ):
            self.mumu.ensure_coc(3)

    def test_restarting_the_game_stops_it_and_starts_it_on_its_own_controller(self) -> None:
        with (
            patch.object(AdbController, "stop_app") as stopped,
            patch.object(AdbController, "launch_app") as launched,
        ):
            self.mumu.restart_coc(_instance())
        stopped.assert_called_once_with(COC_PACKAGE)
        launched.assert_called_once_with(COC_PACKAGE)

    def test_one_controller_per_serial(self) -> None:
        assert self.mumu.controller("127.0.0.1:16384") is self.mumu.controller("127.0.0.1:16384")
        assert self.mumu.controller("127.0.0.1:16416") is not self.mumu.controller(
            "127.0.0.1:16384"
        )


class LDPlayerAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        (root / "ldconsole.exe").write_bytes(b"")
        (root / "adb.exe").write_bytes(b"")
        patcher = patch.object(emulator_module, "use_adb_executable")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ld = LDPlayerAdapter(install_root=root)

    def test_an_install_without_ldconsole_is_refused(self) -> None:
        (self.ld.install_root / "ldconsole.exe").unlink()
        with pytest.raises(EmulatorError, match="LDPlayer CLI"):
            LDPlayerAdapter(install_root=self.ld.install_root)

    def test_the_install_is_found_through_its_uninstaller_when_no_location_is_given(self) -> None:
        with patch.object(ldplayer_module, "uninstall_dirs", return_value=[self.ld.install_root]):
            assert ldplayer_module.detect_install_path() == self.ld.install_root
        with (
            patch.object(ldplayer_module, "uninstall_dirs", return_value=[Path("absent")]),
            pytest.raises(EmulatorError, match="雷電"),
        ):
            ldplayer_module.detect_install_path()

    def test_instances_come_off_list2_and_only_one_adb_answers_is_asked_about_the_game(
        self,
    ) -> None:
        listing = b"0,LDPlayer,1839594,921748,1,30476,48656,1600,900,240\r\n1,Spare,0,0,0,-1,-1,1280,720,240\r\n"
        with (
            patch.object(LDPlayerAdapter, "_run", return_value=listing) as ran,
            patch.object(AdbController, "shell", side_effect=["1\n", AdbControlError("refused")]),
            patch.object(AdbController, "is_running", return_value=True) as asked,
        ):
            up, down = self.ld.enumerate_instances()
        assert ran.call_args.args[0][1:] == ["list2"]
        asked.assert_called_once_with(COC_PACKAGE)
        assert (up.emulator_id, up.adb_serial, up.resolution, up.coc_running) == (
            "ldplayer:0",
            "127.0.0.1:5555",
            "1600x900",
            True,
        )
        assert (down.adb_serial, down.android_started, down.process_started, down.coc_running) == (
            "127.0.0.1:0",
            False,
            False,
            False,
        )
        assert self.ld.serial_of(down) == "127.0.0.1:5557"

    def test_an_instance_whose_boot_has_not_completed_has_no_port(self) -> None:
        """`list2` says 1 ten seconds before ADB answers, and a game launched
        before the boot completes comes up and dies; `_booted` waits on the port.
        """
        listing = b"0,LDPlayer,1,2,1,43060,51556,1600,900,240\r\n"
        for answer in (AdbControlError("not found"), "0\n"):
            with (
                patch.object(LDPlayerAdapter, "_run", return_value=listing),
                patch.object(AdbController, "shell", side_effect=[answer]),
                patch.object(AdbController, "is_running") as asked,
            ):
                (booting,) = self.ld.enumerate_instances()
            assert booting.android_started
            assert not booting.endpoint.ready
            assert self.ld.serial_of(booting) == "127.0.0.1:5555"
            asked.assert_not_called()

    def test_an_instance_list2_has_lost_is_up_when_its_adb_answers(self) -> None:
        """`list2` read `0,…,-1,-1` for an instance that was up and answering
        ADB; believing it sent `ensure_coc` to launch an emulator already running.
        """
        listing = b"0,LDPlayer,0,0,0,-1,-1,1600,900,240\r\n"
        with (
            patch.object(LDPlayerAdapter, "_run", return_value=listing),
            patch.object(AdbController, "shell", return_value="1\n"),
            patch.object(AdbController, "is_running", return_value=False),
        ):
            (lost,) = self.ld.enumerate_instances()
        assert (lost.android_started, lost.adb_serial, lost.state, lost.coc_running) == (
            True,
            "127.0.0.1:5555",
            "running",
            False,
        )

    def test_each_lifecycle_call_is_ldconsole_by_index(self) -> None:
        with patch.object(LDPlayerAdapter, "_run", return_value=b"stop") as ran:
            self.ld.launch_instance(2)
            self.ld.close_instance(2)
        assert [call.args[0][1:] for call in ran.call_args_list] == [
            ["isrunning", "--index", "2"],
            ["launch", "--index", "2"],
            ["quit", "--index", "2"],
        ]

    def test_an_instance_already_booting_is_not_launched_again(self) -> None:
        """A second launch into a booting instance reset its settings to 1920x1080."""
        with patch.object(LDPlayerAdapter, "_run", return_value=b"running\r\n") as ran:
            self.ld.launch_instance(0)
        assert [call.args[0][1:] for call in ran.call_args_list] == [["isrunning", "--index", "0"]]

    def test_a_restart_quits_lets_the_vm_wind_down_and_launches(self) -> None:
        """Never `reboot`, which rewrote the instance's settings to 1920x1080 with no ADB."""
        answers = iter([b"", b"running", b"stop", b"", b"stop", b"running"])
        with (
            patch.object(
                LDPlayerAdapter, "_run", side_effect=lambda *_a, **_k: next(answers)
            ) as ran,
            patch.object(ldplayer_module.time, "sleep") as slept,
        ):
            self.ld.restart_instance(0)
        assert [call.args[0][1] for call in ran.call_args_list] == [
            "quit",
            "isrunning",
            "isrunning",
            "launch",
            "isrunning",
            "isrunning",
        ]
        # A VM process outlived `quit` by five seconds, and a launch inside
        # that never got a VM at all.
        assert call(ldplayer_module.QUIT_SETTLE) in slept.call_args_list

    def test_the_version_is_read_off_the_banner(self) -> None:
        banner = b"ldplayer v14.0.29.0 Command Line Management Interface\r\n"
        with patch.object(LDPlayerAdapter, "_run", return_value=banner):
            assert self.ld.version() == "14.0.29.0"

    def test_a_serial_is_known_before_the_instance_is_up(self) -> None:
        """MuMu reports port 0 while an instance is down, so the scheme stands in for it."""
        down = _instance(index=3, adb_serial="127.0.0.1:0", android_started=False)
        assert self.ld.serial_of(down) == "127.0.0.1:5561"
        assert self.ld.serial_of(_instance(adb_serial="127.0.0.1:5555")) == "127.0.0.1:5555"


@unittest.skipUnless(sys.platform == "win32", "DPAPI is a Windows API")
class SecretStoreTests(unittest.TestCase):
    def test_a_key_round_trips_through_dpapi_without_being_written_in_clear(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = SecretStore(path=Path(folder) / "gemini.key.dpapi")
            store.save("AQ.secret-key")
            assert b"AQ.secret-key" not in store.path.read_bytes()
            assert store.load() == "AQ.secret-key"
            store.clear()
            assert not store.path.exists()
            # Clearing twice is not an error.
            store.clear()

    def test_nothing_saved_falls_back_to_the_environment_and_then_the_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = SecretStore(path=Path(folder) / "absent")
            with (
                patch.dict(os.environ, {"GEMINI_API_KEY": "from-env"}),
                patch.object(secrets, "dotenv_value", return_value="from-file"),
            ):
                assert store.load() == "from-env"
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(secrets, "dotenv_value", return_value="from-file"),
            ):
                assert store.load() == "from-file"
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(secrets, "dotenv_value", return_value=""),
            ):
                assert store.load() == ""


class EntityMappingDownloadTests(unittest.TestCase):
    def _download(self, body: bytes | Exception, cached: str | None) -> object:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "cocMapping.json"
            if cached is not None:
                path.write_text(cached, encoding="utf-8")
            response = MagicMock()
            response.__enter__.return_value.read.return_value = body
            opener = patch.object(
                mapping.urllib.request,
                "urlopen",
                side_effect=body if isinstance(body, Exception) else None,
                return_value=response,
            )
            with opener, patch.object(mapping, "ENTITY_MAPPING_PATH", path):
                fetched = mapping.fetch_entity_mapping()
                assert path.read_text(encoding="utf-8")
            return fetched

    def test_a_download_is_kept_beside_the_data_directory(self) -> None:
        fetched = self._download(b'{"th_buildings": {"1000001": "Cannon"}}', cached=None)
        assert fetched.root["th_buildings"][1000001] == "Cannon"

    def test_a_failed_download_falls_back_to_the_cached_copy(self) -> None:
        fetched = self._download(OSError("offline"), cached='{"bh_troops": {"4000041": "Baby"}}')
        assert fetched.root["bh_troops"][4000041] == "Baby"

    def test_a_failed_download_with_nothing_cached_raises(self) -> None:
        with pytest.raises(OSError, match="offline"):
            self._download(OSError("offline"), cached=None)


class ClipboardTests(unittest.TestCase):
    """Against the real Windows clipboard, like the DPAPI round trip above.

    This is where the game's village export arrives: the copy button writes to
    the Android clipboard and MuMu mirrors it here, `cmd clipboard get` having
    no shell implementation on the emulator to read it from the other side.
    """

    def test_a_clear_and_a_write_round_trip_against_the_real_clipboard(self) -> None:
        """The round trip `export` relies on, run for real: empty is what says
        nothing was copied, and the write is how the person's own clipboard
        survives a command that had to empty it.
        """
        held = clipboard.read_clipboard()
        self.addCleanup(clipboard.write_clipboard, held)
        clipboard.clear_clipboard()
        assert clipboard.read_clipboard() == ""
        clipboard.write_clipboard("村莊資訊 round trip")
        assert clipboard.read_clipboard() == "村莊資訊 round trip"

    def test_the_handles_are_declared_wide_enough_for_a_64_bit_build(self) -> None:
        """Left on the default `c_int` these truncate to 32 bits, which reads as
        garbage rather than as a failure — the kind of bug that only shows as a
        village export that will not parse.
        """
        assert clipboard._kernel32.GlobalLock.restype is ctypes.c_void_p
        assert clipboard._user32.GetClipboardData.restype is wintypes.HANDLE

    def test_clearing_empties_it_between_an_open_and_a_close(self) -> None:
        """Patched rather than run: emptying it for real would eat whatever the
        person running the tests had on their clipboard. What matters is that
        the close happens, because Windows lets one process hold it at a time.
        """
        with patch.object(clipboard, "_user32") as user32:
            user32.OpenClipboard.return_value = 1
            clipboard.clear_clipboard()
        assert [name for name, _, _ in user32.mock_calls] == [
            # An owner window, because opening with NULL makes every later
            # `SetClipboardData` fail — see `_opened`.
            "GetDesktopWindow",
            "OpenClipboard",
            "EmptyClipboard",
            "CloseClipboard",
        ]

    def test_a_clipboard_nobody_can_open_is_an_error_rather_than_an_empty_read(self) -> None:
        """Empty means "nothing was copied" everywhere it is read, so a clipboard
        that never opened must not answer the same way.
        """
        with patch.object(clipboard, "OPEN_GAP", 0), patch.object(clipboard, "_user32") as user32:
            user32.OpenClipboard.return_value = 0
            with pytest.raises(clipboard.ClipboardError, match="剪貼簿"):
                clipboard.read_clipboard()
        assert user32.OpenClipboard.call_count == clipboard.OPEN_TRIES


if __name__ == "__main__":
    unittest.main()
