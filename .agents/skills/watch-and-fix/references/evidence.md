# 收證據

## 三種畫面

**迴圈自己讀的那些** (`--debug`): 檔名是迴圈當下在問的問題 (`0006_scout`, `0010_pass`, `0018_dropped`; 標籤是 `_frame` (`ui/runner.py` 的 `ScreenRunner`, `AttackRunner` 繼承它) 的呼叫端給的, 現有哪些 grep `self._frame(`). 這是判讀器真的看過的那一張, 判讀錯的證據在這裡. 存下時 `run.log` 有一行 `Saved frame <檔名>`, 它前後的 log 就是讀這張的那一步; 出兵連點的每一個座標也在 `run.log` (`Tapping N points …: (x, y) …`), 不用開 `--debug` 也有.

**判英雄有沒有下去的那幾張**: 第一波出兵後只判一次, 是 `settled` (`_settle_drops`); 沒下去的重試 (跟夜世界送機器) 是一對 `before-drop` 跟 `dropped` (`_drop_singles`). 比的是卡片上有沒有畫出血條, 有沒有變灰. 查英雄有沒有下去看這幾張, 不看戰場, 也不看 log.

**心跳那些** (`--shot-every`): `tick_00012.3s.png`, 按檔名排就是按時間排, 補迴圈兩次截圖之間的洞. 五秒一張夠看出兵怎麼走.

## 把一張畫面丟給所有判讀器

```bash
uv run ai_coc read <png>
```

回 `FrameReading` (欄位看 `models.py` 的 `FrameReading` 跟 `commands.read`), 是每個 parser 對這張圖的說法, 包括 `counts`: 每張卡的 `xN` 讀到幾, 決定點幾下, 查出兵節奏先看它.

**一輪卡住先看「這是哪個畫面」的欄位, 不看兵**: `world` (None 是根本不是村莊), `attack_menu` / `night_menu` (兩個世界的攻擊對話框, 互相認不得), `searching` (夜世界配對在等真人), `loot_cart` / `cart_ready` (推車面板開著沒, 收集鈕活不活; 灰的原因看 `CartReport.held`), `battle_over` (結算畫面, 這才是戰鬥結束的判準, 不是 `scout` 讀到 None), `welcome_back` (被打之後開場的 首領，歡迎回來 報告, 按 `back` 就關), `dialog` (確定／取消面板跟兩顆按鈕座標), `idle_dialog` / `loading` (掉線, 伺服器沒回應). `outcome` 是 `no_attack_menu` 的那一輪, 這幾個欄位說出當時看到什麼.

**這是分辨「判讀錯」跟「點沒中」的唯一辦法**: parser 讀到的跟眼睛看到的一樣, 問題在後面的動作; 不一樣, 問題在 parser.

## 從活著的遊戲抓畫面

```bash
uv run ai_coc capture --count 30 --gap 1.5 --label <這次在測什麼>
```

量判讀器還讀不懂的新畫面用這個, 連拍才抓得到瞬間的畫面. 存到那次執行的 `frames/`, 不能指定, 所以 `--label` 要寫. **畫面只留七天** (`FRAME_RETENTION_DAYS`): 過期的 `frames/` 由下一次開跑刪掉, `run.log` 跟 `result.json` 不動. 要跨好幾天對照就自己複製出去.

**不要自己下裸的 `adb shell screencap`**: 不指定 display 截圖會解碼失敗, `input tap` 會點到 launcher. `ai_coc capture` 已經用 `AdbController.display_for(package)` 做對了.

## 花一場戰鬥去量

```bash
uv run ai_coc probe --debug          # 實測出兵邊界, 對照判讀器的說法
uv run ai_coc bounds --debug         # 實測地圖邊緣, 回推村莊範圍
```

各丟一場戰鬥換一組數字. 值得跑的時機: 遊戲更新後, 村莊換主題後, 或連續好幾場在同一處出兵失敗而畫面看不出原因. 細節在 `AGENTS.md` (`probe`) 跟 `parsers/boundary.py` 的註解 (`bounds`). 它們用 `LootThresholds()` 不用設定檔, 要的是任何一場戰鬥.

## 讀一場戰鬥

**log 跟畫面都要看**: log 說迴圈以為在做什麼, 畫面說實際發生什麼. 只讀 log 找不到「兵下在沒用的地方但每一步都成功」, 那才是最值得優化的一類.

夠用的路徑, 不是規定:

1. `result.json` 這一輪的 `AttackReport` (迴圈還在跑的話還沒有這個檔, 整個 series 收工才寫; 那時看 `run.log` 那一輪的 `Attack finished:` 跟 `plans.jsonl`): 有沒有開打, 對手**擺出**多少 (`attacked` 是偵察讀的可搶量, 不是帶回家的量), `outcome` (十八種, 清單在 `models.py` 的 `AttackOutcome`)
2. `run.log` 這一輪: 走到哪一步, 哪一步發了 WARNING
3. 那一步的畫面, 用檔名找
4. 丟進 `ai_coc read`, 看 parser 讀到什麼
5. 心跳畫面把整場看一次: 兵往哪走, 打到什麼, 什麼時候停

第 5 步不要跳過: 前四步只說迴圈有沒有照規則跑完, 戰術對不對只有整場畫面回答得了.
