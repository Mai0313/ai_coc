"""看一眼村莊, 點一下村莊. 這是純視覺流程唯一需要的原語.

`ai_coc` 的每個指令都是一整套迴圈, 沒有一個是「點這裡然後給我看」. 這個腳本補上
那一格: 它自己解析設定裡那台模擬器的 instance 跟 display, 所以呼叫端只要給座標.

    uv run python <skill>/scripts/eye.py shot out.png
    uv run python <skill>/scripts/eye.py tap 1157,500 out.png
    uv run python <skill>/scripts/eye.py zoom in.png 1050,340,1400,580 out.png

`zoom` 不碰遊戲, 純粹把一張存下來的圖裁出一塊放大, 因為 1600x900 縮到對話裡看,
一棟建築只剩幾十像素, 認不出來.
"""

import sys
import time
from pathlib import Path

from PIL import Image


def _live():
    from ai_coc.commands import _controller
    from ai_coc.constants import COC_PACKAGE

    adb = _controller()
    return adb, adb.display_for(COC_PACKAGE)


def main() -> None:
    action = sys.argv[1]
    if action == "zoom":
        source, box, out = sys.argv[2], sys.argv[3], Path(sys.argv[4])
        left, top, right, bottom = (int(part) for part in box.split(","))
        image = Image.open(source).crop((left, top, right, bottom))
        # 放大到四倍上限, 但不超過一個對話裝得下的尺寸; 比那還寬的裁切就不放大.
        scale = max(1, min(4, 1400 // max(1, right - left)))
        image.resize((image.width * scale, image.height * scale), Image.LANCZOS).save(out)
        # 倍率跟原點一起印出來, 因為在放大圖上量到的座標要換算回去才能拿來點,
        # 而倍率是隨裁切寬度變的, 讀圖的人沒辦法從輸出反推.
        print(f"{out} scale={scale} origin={left},{top}")
        print(f"原圖座標 = ({left} + 放大圖x/{scale}, {top} + 放大圖y/{scale})")
        return

    adb, display = _live()
    if action == "tap":
        x, y = (int(part) for part in sys.argv[2].split(","))
        adb.tap(x, y, display)
        out = Path(sys.argv[3])
        # 選單滑出來要一點時間, 太早截會拍到一半的動畫.
        time.sleep(1.2)
    else:
        out = Path(sys.argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(adb.screenshot(display))
    print(out)


if __name__ == "__main__":
    main()
