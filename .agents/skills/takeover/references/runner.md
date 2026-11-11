# 跑打資源的迴圈

給被派來跑打資源迴圈的 subagent. 交辦寫的是這一趟才有的: 先打哪個世界, 兩個世界各自的 `--plan`, 門檻旗標, 開不開 `--debug` / `--shot-every`, 終點, 打完要不要回日世界, 要不要截圖, 暫存檔放哪. 這份是每一趟都一樣的那一半.

這份講到 `~/.ai_coc/state.json` 的 `status`、`pid`、`log`、`stop_by`、`loan` 而沒說是哪一塊的, 都是 `emulator` 那一塊 (`watch` 那一塊有同名的欄位, 不要看錯).

**你不判斷**: 不花資源, 不看圖, 不改程式碼, 不修模擬器. 那些都是派你的 session 的事, 它要判斷的東西你在回報裡交給它. 碰到交辦跟這份以外的狀況, 停下來回報.

## 指令怎麼寫

**在主 checkout (預設分支) 上跑, 每個指令都寫成 `uv run --no-sync ai_coc …`**, `stop` 也一樣: 開發在另一個 worktree 做, 理由在 `AGENTS.md` 的 The loop runs the main checkout. 真的要同步 (merge 帶進新的依賴, 指令 import 失敗) 就等手上的程序結束再 `uv sync`; sync 要等沒有任何 `ai_coc` 程序在跑, 睡著的 `ai_coc watch` 也算, 那不是你的就停下來回報.

**每一個 `ai_coc` 指令都帶 `--agent`、`--session`、`--mission`**: 你自己的名字 (`claude-code`、`antigravity`、`codex`), 派你的 session id 加上 `-farm` 後綴 (`<派工的 session id>-farm`), 這一趟在做什麼 (一句話). 一定要跟派工的不一樣: 排隊不看 session, 借用卻是照 session 認借的人, 用同一個 id 的話, 你不帶 `--yield` 的指令會被當成借的那一邊, 把借用一直撐著不還給你自己的迴圈.

**會佔模擬器的指令都帶 `--yield`**, `world --day` / `--night` 跟 `attack` 都要: 那是背景工作, 不會把別人的 run 停掉, 模擬器有人在用就排隊等; 別人要用時它是被借走, 不是被停掉 (見「被叫停, 被借走」). 不佔模擬器的 `capture` 跟 `stop` 沒有這個旗標, 帶了指令會被拒絕 (exit code 2, 什麼都沒跑).

**旗標照交辦帶, 沒寫就不帶, 不要自己加或改**: 門檻 (`--min-gold`、`--min-elixir`、`--min-dark`), `--plan`, `--debug`, `--shot-every` 都一樣. 交辦沒寫的 `--plan` 不要自己補, 也不要拿掉. 只有 `--repeat` 有預設: 交辦沒寫就是 `--repeat 0`, 打到倉庫滿迴圈自己收工. 每輪跳過太多就在回報裡講, 不要自己調.

**每一次開跑都帶一個沒用過的 `--label`**, 例如第幾圈、哪一步、第幾次開 (`--label day3-c2-attack-1`; 被借走後同一組旗標開回去就換成 `-2`): 目錄名稱就是 `<時間>-attack-day3-c2-attack-1`, `ls -d ~/.ai_coc/logs/*-day3-c2-attack-1` 出現了就是它. 列出不只一個目錄就是標籤用過了. 標籤只留前 40 個字, 所以不要放整串 session id.

**每一步各自一次呼叫, 不要把幾步寫成一支只看 exit code 的腳本** (`world --day|--night … && attack …`): 被叫停看 exit code 看不出來 (見「被叫停, 被借走」), 串起來的那一步照樣開跑. 也不要在步驟之間結束回合: 回合只在最後的回報結束.

暫存檔 (等待用的腳本, 重導向的輸出) 放交辦給的路徑, 不要放 `~` 或 `~/.ai_coc`. 程式每次執行自己寫一個目錄, 本來就不必重導向.

## 開跑跟等

**先切到要打的村莊, 再開.** `attack` 打的是遊戲當下停著的村莊, 遊戲會開在上次離開的那一個, 而沒有指令會自己坐船:

```bash
uv run --no-sync ai_coc world --day --yield --label <標籤> --agent <名字> --session <派工的 session id>-farm --mission "打日世界資源"
uv run --no-sync ai_coc attack --repeat 0 --plan src/ai_coc/plans/flat.json --yield --label <標籤> --agent <名字> --session <派工的 session id>-farm --mission "打日世界資源"
```

`world --day` / `--night` 回的不是那個村莊 (`crossing_failed`) 就不要開 `attack`, 停下來回報.

**你要在前景阻塞.** 沒有人在跟你講話, 背景指令跑完也不保證叫得醒一個已經結束回合的 subagent, 所以 subagent 把指令 (或等它的迴圈) 丟到背景再結束回合, 就會停在那裡不動. 而一次阻塞的呼叫有 runtime 自己的 timeout 上限 (常見是十分鐘, 預設值往往更短; Claude Code 把 subagent 跑到 180 秒的前景呼叫自己移到背景, timeout 給 590 秒也一樣), 所以分段等: 一段比那個上限短, **那次呼叫的 timeout 要設得比一段長** (沒設的話一段還沒等完就被砍), 等不到就再下一段. **分段的只有等待, 指令本身用 runtime 自己的背景執行開**, 在另外幾次前景呼叫裡等它: `attack` 一跑就超過一次呼叫的上限, 而指令被砍等於把軍隊丟在場上. 所以不要替它包 `timeout`, 也不要用 shell 的 `&` 或 `nohup` 丟出去 (exit code 跟輸出都回不來); 一個 subagent 用 `timeout 590` 包住 `attack`, 時間一到就砍在一輪中間, 沒有 `result.json`, 那次已經打完的五輪只剩 `run.log` 數得出來. 每一段不要只看 `result.json`:

```bash
RUN=~/.ai_coc/logs/<這次的目錄>
for i in 1 2 3; do   # 一段最多兩分半, 短過上面那 180 秒
    [ -s "$RUN/result.json" ] && break
    jq -e --arg run "$(basename "$RUN")" '.emulator.status == "idle" and (.emulator.log // "" | contains($run))' ~/.ai_coc/state.json >/dev/null && break
    sleep 50
done
```

`RUN` 從這次執行 log 開頭那行 `This run is being kept in ...` 拿 (第一行), 或照上面的 `--label` 找, 不要拿 `ls -t` 最上面那個: 新目錄還沒建好之前, 最上面的是上一次, 它的 `result.json` 已經有內容, 等待會立刻結束, 接著你就在還在跑的那一輪上面再開一輪. 用時間猜 (`ls -dt` 加上開跑那一分鐘的字樣) 一樣會猜錯.

一輪四五分鐘, 所以 `--repeat 3` 要好幾段, `--repeat 0` 要很多段. **每一段等完還沒結束, 而 `state.json` 的 `log` 是這次的目錄, 就看它的 `pid` 還在不在** (還不是這次的目錄是還在排隊, 見下一段): 還在就是 run 還活著, 接著等下一段 (順便看 `run.log` 有沒有往前走); 不在了就是被砍掉 (這種 run 會一直停在 `running`), 停下來回報. 這個 pid 檢查就是整段等待的出口: 不要等滿固定幾段就當它跑完往下走. 前景卡住對 subagent 沒有代價. 不要用 `os.kill(pid, 0)` 查 (`AGENTS.md` 的 Look before driving).

**帶 `--yield` 的指令可能還在排隊**: `run.log` 最後一行是 `waiting for it to come back` (等借走模擬器的那一邊還回來) 或 `holds the emulator; waiting for it to finish` (等別人的指令跑完), `state.json` 上也還不是它, 所以上面那段只在 `state.json` 的 `log` 是這次的目錄時才算它收工. 照常分段等, 這兩行之後 log 不動是正常的, 不算下面說的卡死; 借用最久到借的那一邊最後一個指令跑完 30 分鐘, 有人 `giveback` 就更早. **但排在一個卡死的 run 後面會永遠等下去**: 帶 `--yield` 的指令不會請任何人收工, 也不會結束任何人. 所以停在 `holds the emulator; waiting for it to finish` 的時候, 去看佔著的那個 run (`state.json` 的 `log`) 的 `run.log` 還有沒有在動, 超過一場戰鬥沒動就照下一段的卡死處理, 停下來回報.

**pid 還在但 `run.log` 超過一場戰鬥的時間 (五分鐘上下; 夜世界配對最久量過五分半) 沒有新的一行, 是卡死了**, 只有一個例外: 最後一行是 `waiting for the server rather than tapping` 的話, 迴圈在等伺服器, 最久 45 分鐘不寫 log, 那是正常的, 照常等. 卡死的樣子是這樣: 模擬器重開之後停在 `Launching com.supercell.clashofclans` 九分鐘以上就出過, 那次模擬器已經掛了而等待一直沒結束. 不要再等下一段: 用 `uv run --no-sync python .agents/skills/repair-emulator/scripts/look.py <資料夾>` 截一張 (它不佔用模擬器), 不用打開, 回報 pid, log 最後一行跟那張圖的路徑.

**每一次看 `run.log` 都要讀完就回來.** 不要掛在上面等新行進來 (`tail -f` 那一類): 在 Windows 上那種讀法會一直握著檔案, 那次執行的目錄就再也刪不掉.

## 讀結果

**每一步跑完, 先讀 `state.json` 看有沒有被叫停** (見「被叫停, 被借走」), 再讀它的結果.

**`result.json` 是一個陣列, 不是一個物件.** `attack` 寫的是 `AttackSeries`, 一輪一個 `AttackReport`, 頂層沒有 `stock_full`. 「這個世界打完了沒」看**最後一個元素**:

```bash
jq '.[-1] | {world, outcome, stock_full, attacked}' ~/.ai_coc/logs/<run>/result.json
```

`jq '.stock_full'` 對陣列直接報錯, 別的讀法多半拿到不是 `true` 的東西, 讀起來像「還沒滿」. 其他指令 (`world` …) 多半寫單一個物件. 純文字的有兩種要認得: `借用被 ai_coc stop 取消了…` (見「被叫停, 被借走」) 跟 `另一部裝置登入了這個帳號…` (照下表的 `session_taken`), exit code 都不是 0. 其他情況下沒有這個檔案, 或 exit code 不是 0: 程序被砍掉或崩掉, 停下來回報, 附 `run.log` 最後幾十行.

檔案完整的話, 最後一個元素加上 `run.log` 最後幾行說它怎麼結束, 照下面接下一步. **迴圈自己跑完是接縫不是終點**: 照交辦接下去, 不要回頭問. 一個 run 結束而你什麼都沒接, 模擬器就閒著, 使用者卻以為還在打.

- **`stock_full` 是 `true`**: 這個世界打滿了. 交辦還有另一個世界, 就 `world --day` / `--night` 過去開它的 `attack` (帶它自己的 `--plan`); 兩個都滿了就照交辦收尾 (例如回日世界, 截圖), 然後回報. 收尾只在這樣正常收工時做: 下面停下來回報的那幾種、被叫停的, 都不開船也不截圖, 開船會把遊戲登入回來, 把手機上的使用者踢掉
- **交辦的 `--repeat N` 跑完**: 照交辦接下去或回報
- **`run.log` 結尾附近有 `Stop requested`, 或 `outcome` 是 `stopped`** (搜尋中、配對中或等伺服器時被停): 有人下了 `ai_coc stop`. 照「被叫停, 被借走」
- **最後一個元素是 `session_taken`, 或 `另一部裝置登入了這個帳號…`**: 使用者在手機 (或另一台模擬器) 登入了, 迴圈沒有搶回來就收工. 停下來回報, **之後一個 `ai_coc` 指令都不要下**, 也不要先把遊戲弄回村莊: 每個指令開跑時都會重新登入, 把他踢下線
- **結尾是 `The emulator has not answered for 3 rounds; ending the series`**: 連續三輪 `emulator_silent`, 整個 series 收工, 回報. 夾在中間一兩輪 `emulator_silent` 不是事, 迴圈照打. 這種有完整的 `result.json`, 不要跟被砍掉的搞混
- **結尾是 `The attack menu has not opened for 5 rounds in a row; ending the series`**: 連續 `no_attack_menu`, 整個 series 自己收工, 回報
- **`army_short`, `other_village`**: 整個 series 到此為止, 回報
- **都不是**: 迴圈自己放棄了, 回報 `outcome` 跟最後一條 `WARNING` / `ERROR`

遊戲一直回不到村莊、同一個錯誤連續三次以上、一輪也沒打成過, 也一樣停下來回報, 不要一直重試. 指令報 `模擬器尚未開放 ADB 連接埠：127.0.0.1:0`, 雷電開起來變成 1920x1080, 或者 5555 連不上, 是模擬器起不來: 停下來回報, 修是派你的 session 的事.

## 被叫停, 被借走

**被叫停看 exit code 看不出來, 所以每一步跑完、開下一步之前都讀 `state.json`.** 在回合之間被停掉的 `attack` 照樣 exit 0, `result.json` 最後一個元素是那一輪自己的 `outcome` (多半是 `took_loot`), 搜尋中、配對中或等伺服器時被停的才是 `stopped`; `world --day` / `--night` 根本不讀叫停, 坐船時被停也照樣跑完. 看得出來的只有 `run.log` 結尾的 `Stop requested`, 跟 `state.json` 那筆 `idle` 紀錄上的 `stop_by`, 而下一個指令一 claim 就把 `stop_by` 蓋掉了. 所以只看剛跑完的那一步: `state.json` 的 `log` 是它的目錄, 而 `stop_by` 有值又不是你下的, 就是它被收掉了. `log` 不是剛跑完那一步的目錄, 那筆 `stop_by` 就是更早以前的 (使用者上一次叫停留下的會一直留到下一個指令 claim), 不算.

被收掉了, 先讀 `state.json` 的 `loan`: 帶 `--yield` 的 run 被別的指令要走時是借出去, 不是停掉, 那個指令叫它收工的同時寫下 `loan` (`lender` 是你, `borrower` 是誰借走、為了什麼), 你那輪的 `run.log` 最後也有一行 `Lent to …`.

- `loan` 在, `lender` 是你, `ended` 不是 `cancelled`: **馬上開回去**, 同一個世界同一組旗標, 都帶 `--yield`, 換一個新的 `--label`, **分兩步**: 先 `world --day|--night --yield …` (借的那一邊可能把遊戲留在另一個村莊), 它會自己等 `giveback` 或借用逾時才真的開跑, 排隊可能超過一次前景呼叫的上限, 照常分段等; 它跑完讀一次 `state.json`, 沒被叫停才開 `attack … --yield …`. 不用等誰傳話, 最後的回報裡提一句被借走多久就好
- 開回去的指令 `result.json` 是 `借用被 ai_coc stop 取消了,這個指令沒有開跑` (exit code 不是 0): 有人在借用期間下了 `ai_coc stop`, 照下一條處理, 不要再開
- 沒有 `loan`, 或 `ended` 是 `cancelled`: 那是有人用 `ai_coc stop` 真的叫停. **不要自己開回去**, 馬上回報停在哪 (打的世界跟旗標, 執行目錄, 那次 `attack` 印的那一段). 之後一個 `ai_coc` 指令都不要下, 只讀倉庫的 `stock`、`status` 也算. 有人傳話叫你接回去, 才同一個世界同一組旗標開回去

**使用者直接跟你說停**, 你自己下 `uv run --no-sync ai_coc stop` (`AGENTS.md` 的 A stop the user orders is run by whoever hears it), 等它停好, 照上一條回報. **`ai_coc stop` 是停止的唯一方式, 不要去砍背景程序**: 砍掉的話迴圈的 `KeyboardInterrupt` 出口執行不到, 軍隊留在場上, 遊戲停在回不了家的畫面, 沒有 `result.json`. 停不是立刻的, 最久要等當下那一場戰鬥打完; `state.json` 的 `status` 是 `stopping` 就是還在打完手上那一場, 等它變成 `idle`.

**有人傳話叫你拉 merge 進來的修正**, 就在兩個指令之間 `git -C <主 checkout> pull --ff-only` (拉進來的 `pyproject.toml` 或 `uv.lock` 有變才照「指令怎麼寫」`uv sync`), 下一個指令起就是新的.

**撞到 bug** (迴圈做了顯然不對的事, 或交辦以外的錯誤): 停下來, 不要自己動手改, 回報哪一輪、`run.log` 的哪幾行跟執行目錄. 看畫面、要不要開回去都是派你的 session 的判斷.

## 回報

**交辦的終點到了, 或碰到上面要停下來的情況, 才結束回合, 交一份回報給派你的 session.** 它看不到你的工具輸出, 要判斷的東西都在這份裡:

- 每個打了的世界: 每一個執行目錄 (被借走或換世界而重開過的每一個都列), 各附那次 `attack` 跑完印的那一段, 照貼不改 (場數、最後一輪怎麼收工、進帳跟現在的倉庫, 夜世界多一列聖水車)
- 停下來的: 停在哪 (世界, 旗標, 執行目錄, 那次 `attack` 印的那一段), 為什麼 (上面哪一條), 誰停的 (`stop_by`)
- 被借走過的: 借了多久
- 交辦要你截的圖: 路徑. 圖你不用打開
- 交辦以外的狀況: 照原樣寫, 例如每輪 `skipped` 都是幾十

推播、要使用者決定的題目、報告的樣子, 都由派你的 session 寫, 你不寫.

**交之前自己看一遍有沒有東西留在背景**, 真的去看而不是回想: 沒有 `ai_coc` 程序, 沒有 shell 還在輪詢, `state.json` 回到 `idle` (被叫停而停在 `stopping` 的, 等到 `idle`). 刻意留著的就留著, 但回報裡要講是哪一個、為什麼. 派你的 session 不會採信「已經清掉了」這句話, 它會自己查一遍.
