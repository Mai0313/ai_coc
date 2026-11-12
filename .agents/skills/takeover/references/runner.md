# 跑打資源的迴圈

給被派來跑打資源迴圈的 subagent. 交辦寫的是這一趟才有的, 寫成照順序編號的完整指令, 照抄不加不改: 先打哪個世界, 兩個世界各自的 `--plan`, 門檻旗標, 開不開 `--debug` / `--shot-every`, 終點, 打完要不要回日世界, 要不要截圖, 暫存檔放哪. 這份是每一趟都一樣的那一半.

這份講到 `~/.ai_coc/state.json` 的 `status`、`pid`、`log`、`stop_by`、`loan` 而沒說是哪一塊的, 都是 `emulator` 那一塊 (`watch` 那一塊有同名的欄位, 不要看錯).

**你不判斷**: 不花資源, 不看圖, 不改程式碼, 不修模擬器. 那些都是派你的 session 的事, 它要判斷的東西你在回報裡交給它. 碰到交辦跟這份以外的狀況, 停下來回報.

## 指令怎麼寫

**在主 checkout (預設分支) 上跑, 每個指令都寫成 `uv run --no-sync ai_coc …`**, `stop` 也一樣: 開發在另一個 worktree 做, 理由在 `AGENTS.md` 的 The loop runs the main checkout. 真的要同步 (merge 帶進新的依賴, 指令 import 失敗) 就等手上的程序結束再 `uv sync`; sync 要等沒有任何 `ai_coc` 程序在跑, 睡著的 `ai_coc watch` 也算, 那不是你的就停下來回報.

**每一個 `ai_coc` 指令都帶 `--agent`、`--session`、`--mission`**: 你自己的名字 (`claude-code`、`antigravity`、`codex`), 派你的 session id 加上 `-farm` 後綴 (`<派工的 session id>-farm`), 這一趟在做什麼 (一句話). 一定要跟派工的不一樣: 排隊不看 session, 借用卻是照 session 認借的人, 用同一個 id 的話, 你不帶 `--yield` 的指令會被當成借的那一邊, 把借用一直撐著不還給你自己的迴圈.

**會佔模擬器的指令都帶 `--yield`**, `world --day` / `--night` 跟 `attack` 都要: 那是背景工作, 不會把別人的 run 停掉, 模擬器有人在用就排隊等; 別人要用時它是被借走, 不是被停掉 (見「被叫停, 被借走」). 不佔模擬器的 `capture`、`wait` 跟 `stop` 沒有這個旗標, 帶了指令會被拒絕 (exit code 2, 什麼都沒跑).

**旗標照交辦帶, 沒寫就不帶, 不要自己加或改**: 門檻 (`--min-gold`、`--min-elixir`、`--min-dark`), `--plan`, `--debug`, `--shot-every` 都一樣. 交辦沒寫的 `--plan` 不要自己補, 也不要拿掉. 只有 `--repeat` 有預設: 交辦沒寫就是 `--repeat 0`, 打到倉庫滿迴圈自己收工. 每輪跳過太多就在回報裡講, 不要自己調.

**每一次開跑都帶一個沒用過的 `--label`**, 例如第幾圈、哪一步、第幾次開 (`--label day3-c2-attack-1`; 被借走後同一組旗標開回去就換成 `-2`): 目錄名稱就是 `<時間>-attack-day3-c2-attack-1`, `ai_coc wait` 就是照它找這一次 (見「開跑跟等」). 同一個標籤它取最新開的那一次, 標籤用過了就可能等到別次. 標籤只留前 40 個字, 所以不要放整串 session id.

**每一步各自一次呼叫, 不要把幾步寫成一支只看 exit code 的腳本** (`world --day|--night … && attack …`): 被叫停看 exit code 看不出來 (見「被叫停, 被借走」), 串起來的那一步照樣開跑. 也不要在步驟之間結束回合: 回合只在最後的回報結束.

暫存檔 (重導向的輸出這類) 放交辦給的路徑, 不要放 `~` 或 `~/.ai_coc`. 程式每次執行自己寫一個目錄, 本來就不必重導向.

## 開跑跟等

**先切到要打的村莊, 再開.** `attack` 打的是遊戲當下停著的村莊, 遊戲會開在上次離開的那一個, 而沒有指令會自己坐船:

```bash
uv run --no-sync ai_coc world --day --yield --label <標籤> --agent <名字> --session <派工的 session id>-farm --mission "打日世界資源"
uv run --no-sync ai_coc attack --repeat 0 --plan flat --yield --label <標籤> --agent <名字> --session <派工的 session id>-farm --mission "打日世界資源"
```

`world --day` / `--night` 回的不是那個村莊 (`crossing_failed`) 就不要開 `attack`, 停下來回報.

**你要在前景阻塞.** 沒有人在跟你講話, 背景指令跑完也不保證叫得醒一個已經結束回合的 subagent, 所以 subagent 把指令 (或等它的迴圈) 丟到背景再結束回合, 就會停在那裡不動. 而一次阻塞的呼叫有 runtime 自己的 timeout 上限 (常見是十分鐘, 預設值往往更短; Claude Code 把 subagent 跑到 180 秒的前景呼叫自己移到背景, timeout 給 590 秒也一樣), 所以分段等: 一段比那個上限短, **那次呼叫的 timeout 要設得比一段長** (沒設的話一段還沒等完就被砍), 等不到就再下一段. **分段的只有等待, 指令本身用 runtime 自己的背景執行開**, 在另外幾次前景呼叫裡等它: `attack` 一跑就超過一次呼叫的上限, 而指令被砍等於把軍隊丟在場上. 所以不要替它包 `timeout`, 也不要用 shell 的 `&` 或 `nohup` 丟出去 (exit code 跟輸出都回不來); 一個 subagent 用 `timeout 590` 包住 `attack`, 時間一到就砍在一輪中間, 沒有 `result.json`, 那次已經打完的五輪只剩 `run.log` 數得出來. 每一段用 `ai_coc wait` 等, 它不佔模擬器, 給它開跑時的 `--label`:

```bash
uv run --no-sync ai_coc wait <標籤> --for 150 --agent <名字> --session <派工的 session id>-farm --mission "等打日世界資源那一步"
```

一段 150 秒, 短過上面那 180 秒, 那次呼叫的 timeout 給 200 秒以上. run 一結束它就提早回來, 回一個 JSON, 看 `outcome`:

- `finished`: 跑完了. `exit` 是 exit code, `run_outcome` 是它自己回報的結果 (`attack` 是整個 series 怎麼收工, `world` 是到了沒有), 照「讀結果」接下一步
- `running`: 還在跑, 再等一段. `quiet` 是它的 `run.log` 幾秒沒寫新的一行: 超過 300 (一場戰鬥的時間; 夜世界配對最久量過五分半) 是卡死了, 只有一個例外: `run.log` 最後一行是 `waiting for the server rather than tapping` 的話, 迴圈在等伺服器, 最久 45 分鐘不寫 log, 照常等. 卡死就不要再等: 用 `uv run --no-sync python .agents/skills/repair-emulator/scripts/look.py <資料夾>` 截一張 (它不佔用模擬器), 不用打開, 回報 `pid`、log 最後一行跟那張圖的路徑. 卡死的樣子是這樣: 模擬器重開之後停在 `Launching com.supercell.clashofclans` 九分鐘以上就出過, 那次模擬器已經掛了而等待一直沒結束
- `queued`: 帶 `--yield` 的指令還在排隊等模擬器, 照常等, 這時 log 不動是正常的. `holder` 是空的就是在等借走模擬器的那一邊還回來, 最久到它最後一個指令跑完 30 分鐘, 有人 `giveback` 就更早. `holder` 有值就是排在那個 run 後面, 而帶 `--yield` 的指令不會請任何人收工, 排在一個卡死的 run 後面會永遠等下去: 再下一次 `ai_coc wait <holder 的目錄>` (不給 `--for`), 照上一條看它的 `quiet`, 卡死了就停下來回報
- `gone`: 程序不在了, 也沒留下結果, 是被砍掉或崩掉: 停下來回報, 附 `run.log` 最後幾十行
- `no_such_run`: 等滿了還找不到那個標籤開的 run, 是標籤寫錯, 或指令根本沒開跑 (旗標被拒絕是 exit code 2, 什麼都不寫): 停下來回報

`wait` 就是整段等待的出口: 不要等滿固定幾段就當它跑完往下走. 前景卡住對 subagent 沒有代價.

**每一次看 `run.log` 都要讀完就回來.** 不要掛在上面等新行進來 (`tail -f` 那一類): 在 Windows 上那種讀法會一直握著檔案, 那次執行的目錄就再也刪不掉.

## 讀結果

**每一步跑完, 先讀 `state.json` 看有沒有被叫停** (見「被叫停, 被借走」), 再讀它的結果.

**結果看 `wait` 回 `finished` 時的 `run_outcome`**, 不用打開 `result.json`. `attack` 的是整個 series 怎麼收工 (`models.py` 的 `SeriesEnding`; `result.json` 是一個物件, 一樣寫在 `ending`, 每一輪在 `rounds`). 指令沒開跑就收手的, `run_outcome` 是 `loan_cancelled` (見「被叫停, 被借走」) 或 `session_taken` (照下表), exit code 都是 1. **迴圈自己跑完是接縫不是終點**: 照交辦接下去, 不要回頭問. 一個 run 結束而你什麼都沒接, 模擬器就閒著, 使用者卻以為還在打. `attack` 的 `run_outcome`:

- **`stock_full`**: 這個世界打滿了. 交辦還有另一個世界, 就 `world --day` / `--night` 過去開它的 `attack` (帶它自己的 `--plan`); 兩個都滿了就照交辦收尾 (例如回日世界, 截圖), 然後回報. 收尾只在這樣正常收工時做: 下面停下來回報的那幾種、被叫停的, 都不開船也不截圖, 開船會把遊戲登入回來, 把手機上的使用者踢掉
- **`repeat_done`**: 交辦的 `--repeat N` 跑完: 照交辦接下去或回報
- **`stopped`**: 有人下了 `ai_coc stop`, 或別的指令借走了模擬器. 照「被叫停, 被借走」
- **`session_taken`**: 使用者在手機 (或另一台模擬器) 登入了, 迴圈沒有搶回來就收工. 停下來回報, **之後一個 `ai_coc` 指令都不要下**, 也不要先把遊戲弄回村莊: 每個指令開跑時都會重新登入, 把他踢下線
- **`emulator_silent`**: 模擬器連續三輪沒回應, 整個 series 收工, 回報. 夾在中間一兩輪沒回應不是事, 迴圈照打
- **`no_attack_menu`**: 攻擊選單連續五輪打不開, 整個 series 收工, 回報
- **`army_short`, `other_village`**: 整個 series 到此為止, 回報
- **其他** (`interrupted`、`builder_free`、`lab_free`, 交辦沒叫你這樣開): 回報 `run_outcome` 跟 `run.log` 最後一條 `WARNING` / `ERROR`

遊戲一直回不到村莊、同一個錯誤連續三次以上、一輪也沒打成過, 也一樣停下來回報, 不要一直重試. 指令報 `模擬器尚未開放 ADB 連接埠：127.0.0.1:0`, 雷電開起來變成 1920x1080, 或者 5555 連不上, 是模擬器起不來: 停下來回報, 修是派你的 session 的事.

## 被叫停, 被借走

**被叫停看 exit code 看不出來, 所以每一步跑完、開下一步之前都讀 `state.json`.** 被停掉的 `attack` 照樣 exit 0, `run_outcome` 是 `stopped`; `world --day` / `--night` 根本不讀叫停, 坐船時被停也照樣跑完, 回的是到了沒有. 誰停的、是不是借走, 看 `state.json` 那筆 `idle` 紀錄上的 `stop_by` 跟 `loan`, 而下一個指令一 claim 就把 `stop_by` 蓋掉了. 所以只看剛跑完的那一步: `state.json` 的 `log` 是它的目錄, 而 `stop_by` 有值又不是你下的, 就是它被收掉了. `log` 不是剛跑完那一步的目錄, 那筆 `stop_by` 就是更早以前的 (使用者上一次叫停留下的會一直留到下一個指令 claim), 不算.

被收掉了, 先讀 `state.json` 的 `loan`: 帶 `--yield` 的 run 被別的指令要走時是借出去, 不是停掉, 那個指令叫它收工的同時寫下 `loan` (`lender` 是你, `borrower` 是誰借走、為了什麼), 你那輪的 `run.log` 最後也有一行 `Lent to …`.

- `loan` 在, `lender` 是你, `ended` 不是 `cancelled`: **馬上開回去**, 同一個世界同一組旗標, 都帶 `--yield`, 換一個新的 `--label`, **分兩步**: 先 `world --day|--night --yield …` (借的那一邊可能把遊戲留在另一個村莊), 它會自己等 `giveback` 或借用逾時才真的開跑, 排隊可能超過一次前景呼叫的上限, 照常分段等; 它跑完讀一次 `state.json`, 沒被叫停才開 `attack … --yield …`. 不用等誰傳話, 最後的回報裡提一句被借走多久就好
- 開回去的指令 `run_outcome` 是 `loan_cancelled` (exit code 1): 有人在借用期間下了 `ai_coc stop`, 照下一條處理, 不要再開
- 沒有 `loan`, 或 `ended` 是 `cancelled`: 那是有人用 `ai_coc stop` 真的叫停. **不要自己開回去**, 馬上回報停在哪 (打的世界跟旗標, 執行目錄, 那次 `attack` 印的那一段). 之後一個 `ai_coc` 指令都不要下, 只讀倉庫的 `stock`、`status` 也算. 有人傳話叫你接回去, 才同一個世界同一組旗標開回去

**使用者直接跟你說停**, 你自己下 `uv run --no-sync ai_coc stop` (`AGENTS.md` 的 A stop the user orders is run by whoever hears it), 等它停好, 照上一條回報. **`ai_coc stop` 是停止的唯一方式, 不要去砍背景程序**: 砍掉的話迴圈的 `KeyboardInterrupt` 出口執行不到, 軍隊留在場上, 遊戲停在回不了家的畫面, 沒有 `result.json`. 停不是立刻的, 最久要等當下那一場戰鬥打完; `state.json` 的 `status` 是 `stopping` 就是還在打完手上那一場, 等它變成 `idle`.

**有人傳話叫你拉 merge 進來的修正**, 就在兩個指令之間 `git -C <主 checkout> pull --ff-only` (拉進來的 `pyproject.toml` 或 `uv.lock` 有變才照「指令怎麼寫」`uv sync`), 下一個指令起就是新的.

**撞到 bug** (迴圈做了顯然不對的事, 或交辦以外的錯誤): 停下來, 不要自己動手改, 回報哪一輪、`run.log` 的哪幾行跟執行目錄. 看畫面、要不要開回去都是派你的 session 的判斷.

## 回報

**交辦的終點到了, 或碰到上面要停下來的情況, 才結束回合, 交一份回報給派你的 session.** 它看不到你的工具輸出, 要判斷的東西都在這份裡:

- 每個打了的世界: 每一個執行目錄 (被借走或換世界而重開過的每一個都列), 各附那次 `attack` 跑完印的那一段, 照貼不改 (場數、這一趟怎麼收工、進帳跟現在的倉庫, 夜世界多一列聖水車)
- 停下來的: 停在哪 (世界, 旗標, 執行目錄, 那次 `attack` 印的那一段), 為什麼 (上面哪一條), 誰停的 (`stop_by`)
- 被借走過的: 借了多久
- 交辦要你截的圖: 路徑. 圖你不用打開
- 交辦以外的狀況: 照原樣寫, 例如每輪 `skipped` 都是幾十

推播、要使用者決定的題目、報告的樣子, 都由派你的 session 寫, 你不寫.

**交之前自己看一遍有沒有東西留在背景**, 真的去看而不是回想: 沒有 `ai_coc` 程序, 沒有 shell 還在輪詢, `state.json` 回到 `idle` (被叫停而停在 `stopping` 的, 等到 `idle`). 刻意留著的就留著, 但回報裡要講是哪一個、為什麼. 派你的 session 不會採信「已經清掉了」這句話, 它會自己查一遍.
