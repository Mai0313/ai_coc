# 走進遊戲裡看

## 一段可以互動的探索腳本

`commands._controller()` 已經找好 `config.json` 的 `adb_serial` 指的那台模擬器 (MuMu 或雷電) 並確認遊戲開著, 只剩 display 要自己解析. 這段腳本不經過 `commands.claim`, 跑的時候 `~/.ai_coc/state.json` 照樣寫著 `idle`, 別的 session 看不出模擬器有人在用; 所以只在畫面已經從背景那輪拿回來之後跑 (SKILL.md 的「打資源要一直在背景跑」), 而且在你的 worktree 裡跑:

```bash
uv run python - <<'PY'
from pathlib import Path
from ai_coc.commands import _controller
from ai_coc.constants import COC_PACKAGE
from ai_coc.ui.world import park_camera
from ai_coc.parsers.world import current_world

adb = _controller()
display = adb.display_for(COC_PACKAGE)
adb.zoom("out", 2, COC_PACKAGE, display)                # 拉遠: 哪個畫面都安全
here = current_world(adb.screenshot(display))           # 停鏡頭要先知道在哪個村莊
if here:                                                # 戰鬥或載入畫面不能叫 park
    park_camera(adb, display, here)                     # 停回定位 (它自己也會先拉遠)

Path("shot.png").write_bytes(adb.screenshot(display))   # 現在畫面
adb.tap(800, 450, display)                              # 點一下
adb.swipe((800, 400), (600, 400), 350, display)         # 拖曳, 最後一個是毫秒
adb.back(display)                                       # BACK 鍵
PY
```

**量任何東西之前先拉到最遠再停回定位**, 因為每個座標都是在那裡量的. 拉遠可以盲送 (`AdbController.zoom`), 但它不動鏡頭位置: 坐船、一滑、湊近看都會讓記下的座標安靜地失效. 停回定位是 `park_camera`, 它自己會先拉遠, 為什麼要這樣讀它的 docstring. 叫它的規矩:

- **看回傳值**: False 就是所有記下來的座標都失效了, 別拿它們去點
- **告訴它是哪個村莊**, 兩張地圖夾在相反的角落
- **戰鬥畫面上絕對不能叫它**: 選著卡片時滑動會沿路把兵丟出去, 所以 `if here` 不能省. 不是村莊的畫面一樣該拉遠, 所以範例自己先 pinch 一次
- 它會先按掉 `ai_coc worker` / `lab` / `status` 開的牌子面板, 按不掉就回 False 不滑; 沒有東西在跑的閒置面板它認不出來, 會照樣滑, 全螢幕面板也不歸它管

方法的完整清單在 `src/ai_coc/adapters/adb.py` 的 `AdbController`, docstring 都值得讀, 尤其是 `tap_many`.

**先確認在哪個村莊**: 腳本裡問 `current_world(png)` (讀上排牌子, 不點東西也不動鏡頭). 要換過去用 `ui.world.cross` (`ai_coc world --go` 用的就是它), 它會把鏡頭滑到角落再點船:

```python
from ai_coc.parsers.world import current_world
from ai_coc.ui.world import cross

print(current_world(adb.screenshot(display)))  # "day" / "night" / None
cross(adb, display, "night")  # 回傳最後停在哪個村莊; 已經在那邊就什麼都不做
```

**每個呼叫都要帶 `display`**: MuMu 的 display 0 是它的 launcher, 少帶的截圖解碼失敗, 點擊安靜地落在 launcher 上, 兩種都不報錯. 雷電的遊戲就在 display 0, 但照樣帶, 程式碼不分是哪一台.

## 三件不能亂做的事

**BACK 鍵在乾淨的主村上會叫出「確定退出遊戲嗎」**, 開著建築選單時也會, 而它跟付錢升級的對話框像素一模一樣, 只有問問題的人知道該按哪個. 按了要自己處理掉, 不要留給後面的迴圈; 正確作法是 `ui/runner.py` 的 `_home`.

**不要點按鈕列上圖示像資源的那兩個**: 加速所有同類項目 (花魔法物品) 跟城牆戒指 (花戒指), 都在藍色底板上, `AGENTS.md` 搜 `blue plate`.

**pinch 一定要帶 `display`**: 兩根手指落在其他 display 的 launcher 上會切換前景 app, 之後每個指令都對著桌布點. `AdbController.zoom` 拿到 `display` 就只送給那個 display 的觸控節點, 拿不到 (沒帶, 或者解析不出節點) 就送給每一個多點觸控節點, 而且不會報錯. 有帶 `package` 的話它事後會把遊戲叫回前景, 那是最後一道, 不是不帶 `display` 的理由.

## 把一個新畫面量成常數

探索的產物不是截圖, 是數字. 一個判讀器需要三樣東西:

1. **一個穩定的錨點**: 遊戲**畫在上面的 UI** (按鈕, 圖示, 底板, 數字, 邊框), 不是美術. 極端的示範在 `AGENTS.md` 的 Wall loop 那一段: 沒有一行在看城牆, 靠「只有城牆能用金幣或聖水擇一升級」這條遊戲規則認出它. 畫在上面的顏色也可能換 (黑水泡泡的底板, 同一個村莊隔幾週拍的兩張, 一張是橘的, 一張是淡的), 顏色對不上就找不靠那個顏色的訊號, 或從同一張畫面學, 不要再加一段顏色範圍
2. **一個閾值, 以及它兩邊的實測值**: 不寫「紅色大於 200」, 寫「紅色量到 130 到 215, `red - max(green, blue)` 是 85 到 105, 旁邊的草地是 -20 到 -30」, 閾值 (125 到 220) 再從實測值往外留一點. 範本是 `parsers/boundary.py` 的 `STROKE_RED` 上面那段註解
3. **反例**: 只在對的畫面上驗過的判讀器在錯的畫面上也會給答案. 收集器泡泡光靠顏色在一張主村畫面上會找到十三塊金色跟九塊洋紅, 是尺寸把它們濾掉的

按鈕的 x 不一定固定: 有些按鈕列從中央往兩邊排, 少一顆整排就位移, `AGENTS.md` 搜 `BUTTON_PITCH`. 量之前在不同狀態下各抓一張比對.

## 從畫面找題目的幾條線索

- **有紅點或數字徽章的地方**: 遊戲自己在說這裡有事情待處理
- **需要重複點很多下的地方**: 那就是自動化的定義
- **有計時器的地方**: 結束的那一刻可以被自動接手
- **`ai_coc read` 每個欄位都是 `None`、空的或 `false` 的畫面**: 沒有任何判讀器認得, 完全沒開發過

## 讀 UI 而不讀底下的東西, 為什麼

違反它的代價是延遲的: 認得某一級城牆的判讀器會通過測試, 在你的村莊上也通過, 然後在升級或改版之後安靜地壞掉, 沒有東西會發現. 遊戲畫在上面的 UI (資源圖示, 價格, 按鈕底板, 對話框) 不管底下選的是什麼都長一樣.
