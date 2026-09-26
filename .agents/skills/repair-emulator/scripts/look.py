"""看模擬器現在的畫面, 不開任何東西.

`ai_coc capture` 跟 spend-loot 的 `eye.py` 都先經過 `_controller()`, 也就是
`ensure_coc`: 模擬器沒開就開模擬器, 遊戲沒開就開遊戲. 修模擬器的時候那正是不能
做的事, 所以這支只找出設定裡那台的 ADB, 截圖, 印出前景視窗, 其他什麼都不碰.

    uv run --no-sync python <skill>/scripts/look.py <資料夾>          # 一張
    uv run --no-sync python <skill>/scripts/look.py <資料夾> 15 1     # 15 張, 每張之間睡 1 秒

每張本身還要一兩秒 (找 display 跟截圖), 所以 `1` 的實際間隔大約兩秒.

實例用 `chosen(emulators())` 找, 跟每個指令一樣: 它只列出 instance, 不開任何東西.
連的是那台的真實 port, 雷電還沒開好時回報的 port 0 不用, 所以 ADB 不通會直接看到
被拒絕的那句話.

遊戲在某個 display 上就截那個 display (MuMu 把遊戲開在自己的 display), 不在就截
display 0.
"""

import sys
import time
from pathlib import Path

from ai_coc.models import DisplayTarget
from ai_coc.commands import chosen, emulators
from ai_coc.constants import COC_PACKAGE
from ai_coc.adapters.adb import AdbControlError, physical_display


def main() -> None:
    out = Path(sys.argv[1])
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    gap = float(sys.argv[3]) if len(sys.argv) > 3 else 1.0
    out.mkdir(parents=True, exist_ok=True)
    emulator, instance = chosen(emulators())
    adb = emulator.controller(emulator.serial_for(instance.index))
    start = time.monotonic()
    for index in range(count):
        try:
            display = adb.display_for(COC_PACKAGE)
        except AdbControlError:
            display = DisplayTarget(
                logical_id="0", physical_id=physical_display(adb.shell(["dumpsys", "display"]), "0")
            )
        focus = adb.shell("dumpsys window | grep mCurrentFocus").strip()
        path = out / f"{index:03d}_{time.monotonic() - start:.1f}s.png"
        path.write_bytes(adb.screenshot(display))
        print(f"{path} display={display.logical_id} {focus}", flush=True)
        if index + 1 < count:
            time.sleep(gap)


if __name__ == "__main__":
    main()
