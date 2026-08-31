# 走進遊戲裡看

## 一段可以互動的探索腳本

`commands._controller()` 已經把找 MuMu instance 跟確認遊戲開著這兩件事做完了, 所以探索不需要自己接 ADB; display 是唯一還要自己解析的一步:

```bash
uv run python - <<'PY'
from pathlib import Path
from ai_coc.commands import _controller
from ai_coc.constants import COC_PACKAGE

adb = _controller()
display = adb.display_for(COC_PACKAGE)
adb.zoom("out", 2, COC_PACKAGE, display)                # 先把鏡頭拉回最遠

Path("shot.png").write_bytes(adb.screenshot(display))   # 現在畫面
adb.tap(800, 450, display)                              # 點一下
adb.swipe((800, 400), (600, 400), 350, display)         # 拖曳, 最後一個是毫秒
adb.back(display)                                       # BACK 鍵
PY
```

**量任何東西之前先把鏡頭拉回最遠.** 這個專案的每一個座標都是在遊戲最遠的 zoom 量出來的, 所以鏡頭停在別的地方的時候, 你量出來的常數全部是錯的 —— 而底下「把一個新畫面量成常數」那一整節的前提就是它. 拉回去不需要先知道現在在哪, 因為到了最遠處再往外縮什麼都不會發生, 所以這一行直接送出去就好, 遊戲自己也報不出目前的 zoom 給你檢查. 程式那邊 `commands.py` 的 `_settle_game` 跟 `ui/runner.py` 的 `GameRunner._settle_zoom` 每次開工都在做這件事, 手寫的探索腳本反而是唯一沒人幫你做的入口.

方法的完整清單在 `src/ai_coc/adapters/adb.py` 的 `AdbController`, 每個方法上面的 docstring 都值得讀一次, 尤其是 `tap_many`, 那裡面兩條規則各自是一場沒下出任何兵的戰鬥換來的.

**先確認自己在哪個村莊.** 這個遊戲有兩張地圖, 日世界 (主村) 跟夜世界 (建築大師基地), 而遊戲會開在上次離開的那一個 —— 所以探索腳本的第一行不是截圖而是 `current_world(png)`, 不然你會拿夜世界的畫面去量日世界的常數. 它讀畫面最上面那排面板的藍色 `i` 徽章, 主村三個, 夜世界兩個 (夜世界沒有護盾那一格), 不點任何東西也不動鏡頭. 要換過去用 `ui.world.cross(adb, display, "night")`, 它會把鏡頭滑到地圖角落再點船.

```python
from ai_coc.parsers.world import current_world
from ai_coc.ui.world import cross

print(current_world(adb.screenshot(display)))  # "day" / "night" / None
cross(adb, display, "night")  # 已經在那邊就什麼都不做
```

**每個呼叫都要帶 `display`.** MuMu 跑好幾個 Android display, 遊戲在它自己那個上面, display 0 是模擬器的 launcher. 少帶這個參數的結果是截圖解碼失敗, 或者點擊安靜地落在 launcher 上, 兩種都不會報錯.

## 三件不能亂做的事

**BACK 鍵在乾淨的主村上會叫出「確定退出遊戲嗎」**, 開著建築選單的時候一樣會. 而那個對話框跟付錢升級的對話框是**同一個面板, 同樣的綠色, 同樣的像素**, 所以只有問問題的人知道現在該按哪一個. 探索的時候按了要記得處理掉, 不要留給後面的迴圈. `ui/runner.py` 的 `_home` 是既有的正確作法.

**不要點按鈕列上圖示看起來像資源的那兩個**: 加速所有同類項目花的是魔法物品, 城牆戒指花的是戒指. 它們坐在藍色底板上, `CLAUDE.md` 搜 `blue plate`.

**pinch 一定要帶 `display`.** MuMu 在它其他的 display 上留著一個 launcher, 而兩根手指落在那上面的意思是「切換前景 app」. 實測過一次: 縮放本身成功了, 而模擬器顯示的是 launcher, 遊戲被壓在後面, 於是那之後每一個指令都在對著桌布點. `AdbController.zoom` 拿到 `display` 就只送給那個 display 的觸控節點, 拿不到就送給每一個多點觸控節點, 而後者不會報錯.

## 把一個新畫面量成常數

探索的產物不是截圖, 是數字. 一個畫面要變成判讀器需要三樣東西:

1. **一個穩定的錨點.** 不能是遊戲美術, 因為那個每一級都不同, 而且改版會全部重畫. 要找的是遊戲**畫在上面的 UI**: 按鈕, 圖示, 底板, 數字, 邊框. 讀 `CLAUDE.md` 的 Wall loop 那一段, 那裡示範了一次極端的作法, 整個迴圈沒有任何一行在看城牆, 它是靠「只有城牆能用金幣或聖水擇一升級」這條遊戲規則認出城牆的
2. **一個閾值, 以及它兩邊的實測值.** 不要寫「紅色大於 200」, 要寫「紅色 125 到 220, 而 `red - max(green, blue)` 是 85 到 105, 旁邊的草地是 -20 到 -30」. 現有的 parser 每個常數旁邊都是這樣註解的, 照著寫
3. **反例.** 一個只在對的畫面上驗證過的判讀器沒有價值, 因為它在錯的畫面上也會給答案. 收集器泡泡那條就是這樣: 光靠顏色在一張主村畫面上會找到十三塊金色跟九塊洋紅, 是尺寸把它們濾掉的

按鈕的 x 座標不一定是固定的. 遊戲有些按鈕列是從畫面中央往兩邊排的, 少一顆按鈕整排就位移. `CLAUDE.md` 搜 `BUTTON_PITCH`. 量之前先在不同狀態下各抓一張比對.

## 從畫面找題目的幾條線索

- **有紅點或數字徽章的地方**: 遊戲自己在說這裡有事情待處理
- **需要重複點很多下的地方**: 那就是自動化的定義
- **有計時器的地方**: 計時器結束就是一個可以被自動接手的時刻
- **`ai_coc read` 全部回 `None` 的畫面**: 沒有任何現有判讀器認得, 也就是完全沒開發過

## 讀 UI 而不讀底下的東西, 為什麼

這是這個專案最重要的一條, 而且違反它的代價是延遲的: 教一個判讀器認得某一級的城牆, 它會在測試裡通過, 在你的村莊上通過, 然後在升級之後或者遊戲改版之後安靜地壞掉, 而沒有任何東西會發現.

遊戲畫在上面的 UI 不會這樣: 資源圖示, 價格, 按鈕底板, 對話框, 這些不管底下選的是什麼建築都長一樣.
