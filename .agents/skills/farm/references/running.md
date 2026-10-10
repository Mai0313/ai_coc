# 讓一輪在背景跑, 並且盯得住它

`farm`, `watch-and-fix`, `build-feature`, `spend-loot`, `watch-upgrades` 五個 skill 共用這一份, 路徑是 `.agents/skills/farm/references/running.md`.

## 為什麼一定要背景跑

一輪進攻四到五分鐘起跳, `--repeat 0` 打到倉庫滿可能是好幾個小時. **主 session 的角色是監督, 不是執行**: 迴圈在背景跑, 你在前景讀 log、改東西、回答使用者, 他隨時插得了話.

**會跑很久的都用背景送出去, 這裡是唯一一份清單**: `attack`; `walls` (掃描加購買好幾分鐘); `upgrade` 跟 `hero` (找不到目標會退回 `_sweep`, 兩分半起跳); `donate --rounds 0`. 幾秒就回來的前景跑: `world`, `read`, `collect`, `capture`, `view`, `status`; `world --go` 真的坐船約一分鐘, 也前景跑; 被借走之後開回去的那一次例外, 它可能要排隊很久, 見「被借走」.

**你是 subagent 的話反過來, 在前景阻塞.** 沒有人在跟你講話, 背景指令跑完也不保證叫得醒一個已經結束回合的 subagent, 所以 subagent 把指令 (或等它的迴圈) 丟到背景再結束回合, 就會停在那裡不動. **subagent 要在前景阻塞**, 而一次阻塞的呼叫有 runtime 自己的 timeout 上限 (常見是十分鐘, 預設值往往更短), 所以分段等: 一段比那個上限短, **那次呼叫的 timeout 要設得比一段長** (沒設的話一段還沒等完就被砍), 等不到就再下一段. **分段的只有等待, 指令本身用 runtime 自己的背景執行開**, 在另外幾次前景呼叫裡等它: `attack` 一跑就超過一次呼叫的上限 (`walls` 平常一兩分鐘, 排隊或退回掃網格時也會超過), 而指令被砍等於把軍隊丟在場上 (見「中止」的「不要去砍背景程序」). 所以不要替它包 `timeout`, 也不要用 shell 的 `&` 或 `nohup` 丟出去 (exit code 跟輸出都回不來); 2026-10-08 一個 subagent 用 `timeout 590` 包住 `attack`, 時間一到就砍在一輪中間, 沒有 `result.json`, 那次已經打完的五輪只剩 `run.log` 數得出來. 每一段照「等待要有出口, 收工要清乾淨」寫, 不要只看 `result.json`:

```bash
RUN=~/.ai_coc/logs/<這次的目錄>
for i in 1 2 3; do   # 一段最多九分鐘
    [ -s "$RUN/result.json" ] && break
    grep -q '"status": "idle"' ~/.ai_coc/state.json && grep -qF "$(basename "$RUN")" ~/.ai_coc/state.json && break
    sleep 180
done
```

`RUN` 從這次執行 log 開頭那行 `This run is being kept in ...` 拿 (通常是第一行; 那次剛好刪了過期畫面的話, 前面多一行 `Deleted the frames of …`), 不要拿 `ls -t` 最上面那個: 新目錄還沒建好之前, 最上面的是上一次, 它的 `result.json` 已經有內容, 等待會立刻結束, 接著你就在還在跑的那一輪上面再開一輪. 用時間猜 (`ls -dt` 加上開跑那一分鐘的字樣) 一樣會猜錯. **最不會拿錯的是每一次開跑都帶一個沒用過的 `--label`**, 例如第幾圈、哪一步、第幾次開 (`--label day3-c2-walls-1`; 被借走後同一組旗標開回去就換成 `-2`): 目錄名稱就是 `<時間>-walls-day3-c2-walls-1`, `ls -d ~/.ai_coc/logs/*-day3-c2-walls-1` 出現了就是它. 列出不只一個目錄就是標籤用過了. 標籤只留前 40 個字, 所以不要放整串 session id.

一輪四五分鐘, 所以 `--repeat 3` 要兩段左右, `--repeat 0` 要很多段. **每一段等完還沒結束, 而 `state.json` 的 `log` 是這次的目錄, 就看它的 `pid` 還在不在** (還不是這次的目錄是還在排隊, 見下一段): 還在就是 run 還活著, 接著等下一段 (順便看 `run.log` 有沒有往前走); 不在了就是被砍掉 (這種 run 會一直停在 `running`), 停下來回報. 這個 pid 檢查就是整段等待的出口, `walls` 也一樣: 不要等滿固定幾段就當它跑完往下走, Gemini 找不到牆而退回掃網格的那一次, 光掃描就要好幾分鐘. 前景卡住對 subagent 沒有代價.

**帶 `--yield` 的指令可能還在排隊**: `run.log` 最後一行是 `waiting for it to come back` (等借走模擬器的那一邊還回來) 或 `holds the emulator; waiting for it to finish` (等別人的指令跑完), `state.json` 上也還不是它, 所以上面那段只在 `state.json` 的 `log` 是這次的目錄時才算它收工. 照常分段等, 這兩行之後 log 不動是正常的, 不算下面說的卡死; 借用最久到借的那一邊最後一個指令跑完 30 分鐘, 有人 `giveback` 就更早. **但排在一個卡死的 run 後面會永遠等下去**: 帶 `--yield` 的指令不會請任何人收工, 也不會結束任何人. 所以停在 `holds the emulator; waiting for it to finish` 的時候, 去看佔著的那個 run (`state.json` 的 `log`) 的 `run.log` 還有沒有在動, 超過一場戰鬥沒動就照下一段的卡死處理, 停下來回報.

**pid 還在但 `run.log` 超過一場戰鬥的時間 (五分鐘上下; 夜世界配對最久量過五分半) 沒有新的一行, 是卡死了**, 只有一個例外: 最後一行是 `waiting for the server rather than tapping` 的話, 迴圈在等伺服器, 最久 45 分鐘不寫 log, 那是正常的, 照常等. 卡死的樣子是這樣: 模擬器重開之後停在 `Launching com.supercell.clashofclans` 九分鐘以上就出過, 那次模擬器已經掛了而等待一直沒結束. 不要再等下一段: 用 `repair-emulator` 的 `look.py` 看畫面 (它不佔用模擬器), 回報 pid, log 最後一行跟那張畫面.

## 開跑

```bash
uv run ai_coc world --go day --yield --agent <名字> --session <session id> --mission "打日世界資源"
uv run ai_coc attack --repeat 0 --yield --agent <名字> --session <session id> --mission "打日世界資源"
```

**你是跟開發同時跑的 farm subagent, 就在主 checkout (預設分支) 上跑, 每個指令都寫成 `uv run --no-sync ai_coc …`**, `stop` 也一樣: 開發在另一個 worktree 做, 理由在 `AGENTS.md` 的 The loop runs the main checkout. 真的要同步 (merge 帶進新的依賴, 指令 import 失敗) 就等手上的程序結束再 `uv sync`.

**merge 進來的修正, 迴圈不會自己用上**: 它跑的是主 checkout 開跑時的程式碼. 開發那邊 merge 之後會傳話叫你拉, 你就在兩個指令之間 `git -C <主 checkout> pull --ff-only` (拉進來的 `pyproject.toml` 或 `uv.lock` 有變才 `uv sync`), 下一個指令起就是新的. 修正要馬上驗證的話是 `watch-and-fix` 的做法: 主 session 自己 `ai_coc stop`, 等你回報收工, 自己拉, 再叫你同一組旗標開回去.

**每一個 `ai_coc` 指令都帶 `--agent`、`--session`、`--mission`**, 其他 skill 裡寫的指令也一樣: 你自己的名字 (`claude-code`、`antigravity`、`codex`), 你自己的 session id, 這一趟在做什麼 (一句話). 每個指令都把它們寫進自己 `run.log` 裡寫目錄的那一行的下一行; 會佔用模擬器的指令還會寫進 `state.json` 的 `caller`, 所以那裡記的永遠是開始這一輪的人, 誰叫停的記在 `stop_by`. 別的 session 就是靠這些查出是誰在開模擬器. 旗標是選填的, 那是留給使用者手動打指令; agent 一律要帶, 沒帶的話 log 會留一行 warning.

**打資源的迴圈每個指令都帶 `--yield`**, `world --go` 跟 `attack` 都要: 那是背景工作, 不會把別人的 run 停掉, 模擬器有人在用就排隊等; 別人要用時它是被借走, 不是被停掉 (見「被借走」). 測試、驗證、花資源這些前景的事不帶.

**先切到要打的村莊, 再開.** `attack` 打的是遊戲當下停著的村莊, 遊戲會開在上次離開的那一個, 而沒有指令會自己坐船. `world --go` 沒切成就不要開 `attack`. 夜世界是 `world --go night`, 前景跑.

**送到背景只能用 runtime 自己管的背景執行** (跑完會通知你的那一種): 它是唯一跑完會把你叫醒的開法, harness 送進對話的那個通知就是接下一步的時刻. `Start-Process`, `nohup … &`, PowerShell 的 job, 任何 shell 層的 detach 都不行: 程序照跑, 但結束時沒有東西叫醒你, 輸出跟 exit code 也回不來 (量過: 夜世界打到 `stock_full` 自己收工之後, session 就停在原地). **不必自己重導向**, 程式一次執行寫一個目錄:

```
~/.ai_coc/logs/2026-08-29-011423-attack/
├── run.log        # 這次執行的完整紀錄, 純文字
├── result.json    # 這次的答案, 跑完才會有這個檔案
├── plans.jsonl    # 只有 attack 有: 一場一行, 那一場的整份戰術
└── frames/        # 迴圈自己讀的畫面要開 --debug; capture 一定會有; world 跟 launch 截得到畫面卻找不到村莊時留 no_village.png (log 會寫路徑)
```

**目錄在開跑開頭那行 log** (`This run is being kept in ...`). 名字是「時間-指令」, `ls -t ~/.ai_coc/logs` 最上面是最近已經建好的一次 (剛開跑的那一次可能還沒建好). 任何指令加 `--label <名字>` 會接在後面 (`…-attack-baseline`), 給之後要找回來的那一次用.

**畫面只留七天, log 永久留.** 過期的 `frames/` 在下一次有指令開跑時刪掉, 同目錄的 `run.log`、`result.json`、`plans.jsonl` 不動, 刪了幾個會寫進 log. 要留久的證據自己複製一份.

**`result.json` 是一個陣列, 不是一個物件.** `attack` 寫的是 `AttackSeries`, 一輪一個 `AttackReport`, 頂層沒有 `stock_full`. 「這個世界打完了沒」看**最後一個元素**:

```bash
jq '.[-1] | {world, outcome, stock_full, attacked}' ~/.ai_coc/logs/<run>/result.json
```

`jq '.stock_full'` 對陣列直接報錯, 別的讀法多半拿到不是 `true` 的東西, 讀起來像「還沒滿」. 其他指令 (`walls`, `collect`, `world` ...) 多半寫單一個物件; `stop`、`giveback`、`capture`、被取消的借用跟遇到別的裝置登入而收手的指令寫的是純文字.

`--debug` 留下迴圈讀的每一張 (每一張都是模擬器的一次 PNG 編碼, 平均兩 MB 多: `AGENTS.md` 量過 11 456 張佔 27.2 GB), 開不開是派工的人決定 (`farm` 的「要不要留畫面」). 存下的每一張, `run.log` 都有一行 `Saved frame <檔名>`. `--shot-every` 另加一條固定心跳 (`tick_00012.3s.png`), 要搭配 `--debug`, 補兩張之間看不到的那段.

## 盯

`run.log` 用搜尋抓有意義的行, 不要整份讀進來 (一小時幾千行):

- `Round \d+ of`: 現在第幾輪
- `Attack finished:`: 每輪一行, 句子是照 `AttackReport.outcome` 組的; 下判斷讀 `result.json` 的 `outcome`, 不要比對這裡的字
- `WARNING` 跟 `ERROR`: 真的要看的

**幾分鐘看一次就好.** 背景指令跑完會通知你, 那才是必須處理的時刻.

**每一次看都要讀完就回來.** 不要掛在 `run.log` 上等新行進來: 在 Windows 上那種讀法會一直握著檔案, 活得比開它的回合久, 那次執行的目錄就再也刪不掉, 要使用者自己開工作管理員去殺 (量過五個 session 留下的殘留壓著 11.2 GB). 要看新的就再讀一次.

**通知進來, 先讀 `result.json` 的最後一個元素, 再決定下一步.** exit code 不是 0 而 `result.json` 是 `借用被 ai_coc stop 取消了…` 那一行: 借用被取消, 照「被借走」那條處理. 是 `另一部裝置登入了這個帳號…` 那一行: 他在手機上登入了, 照 `farm` 的「一輪就要停下來的」. 其他情況下沒有這個檔案, 或 exit code 不是 0: 程序被砍掉或崩掉, `run.log` 最後幾十行是唯一的線索, 遊戲多半停在回不了家的畫面, 先看畫面再決定. 檔案完整的話, 最後一個元素加上 `run.log` 最後幾行說它怎麼結束:

- `stock_full` 是 `true`: 這個世界打滿了, **是接縫不是終點**. 兩個世界都要打就換另一個; 日世界要花的話照 `farm` 的「倉庫滿了」那節; 帶 `--until-idle` 的那一趟照 `farm` 的「打到有空閒」. 不要回頭問使用者要不要繼續
- `outcome` 是 `builder_free` 或 `lab_free`: 這個村莊有工人或實驗室空出來了, 只有帶 `--until-idle` 的那一趟會有. 照 `farm` 的「打到有空閒」接
- `run.log` 結尾有 `Stop requested`: 有人下了 `ai_coc stop` (停在回合之間或兩輪中間的等待都會留這一行). 誰停的看 `~/.ai_coc/state.json` 的 `stop_by`. 不是你下的, 先看同一份檔案有沒有 `loan`, 見「被借走」
- 使用者指定的 `--repeat N` 跑完: 照他的交辦接下去或收工
- 有幾輪 `outcome` 是 `emulator_silent`: 模擬器當下不理人, 夾在中間一兩輪不是事, 照常打. 但結尾是 `The emulator has not answered for 3 rounds; ending the series` 就是連續三輪, 整個 series 收工: 先看模擬器還活著沒有, 不要直接開下一個. 這種有完整的 `result.json`, 不要跟被砍掉的搞混
- 結尾是 `The attack menu has not opened for 5 rounds in a row; ending the series`: 連續 `MENU_FAILURES` 輪 `no_attack_menu`, 整個 series 自己收工. 每一輪自己的 log 說它遇到什麼 (迴圈按 `back` 關不掉的畫面、一直掉線、有人在手機上玩). 先看畫面 (`look.py`), 照 `farm` 的「其他停手的理由」停下來講, 不要直接開下一個
- 最後一個元素是 `session_taken`: 他在手機 (或另一台模擬器) 登入了, 迴圈沒有搶回來就收工. 照 `farm` 的「一輪就要停下來的」: 交回報、發推播, 他說可以之前一個指令都不要下, 也不要先把遊戲弄回村莊
- 都不是就是迴圈自己放棄了, `outcome` 跟最後一條 WARNING / ERROR 說原因, 例如打到一半遊戲跑到另一個村莊 (最後一個元素是 `other_village`, 多半是有人切過村莊, 例如使用者在手機上玩). 這幾種歸 `farm` 的「其他停手的理由」, 先把遊戲弄回村莊, 不要直接開下一個

一個 run 結束而你什麼都沒接, 模擬器就閒著, 使用者卻以為還在打.

`plans.jsonl` 在同一個目錄, 一場一行, 是那一輪照著打的整份戰術 (AI 規劃的, 或 `--plan` 交進去的那份). `jq -c 'select(.round==3)' plans.jsonl` 拿一輪出來看, `jq -r '"\(.round) \(.plan.deploy_from)"' plans.jsonl` 一眼看完整批打了哪些側邊. **一輪打壞先讀它**: 它說打算怎麼打, `run.log` 說實際做了什麼, 對照才知道問題在指揮還是在執行.

跨好幾次跑找同一個症狀用 `grep -r ~/.ai_coc/logs/*/run.log`, 它會說每一筆出自哪一次; 這台機器最近做過什麼看 `ls -t ~/.ai_coc/logs`.

## 等待要有出口, 收工要清乾淨

**任何一個等待的迴圈都必須自己結束得了.** 只等 `result.json` 出現的寫法, 在 run 被打死時永遠等不到, 而那正是最該有人接手的時候 (量過 2026-09-16: 一個 subagent 留了四個 `until [ -s "$RUN_DIR/result.json" ]; do sleep 15; done` 在背景, 等的那一輪被 bug 打死, 八個 process 空轉了三十五個小時, 是使用者發現的). 所以一個等待要同時:

- **不要只看 `result.json`**: 也看 `~/.ai_coc/state.json` 的 `status` 有沒有回到 `idle`, 或那個 pid 還在不在. 死掉的 run 不寫 `result.json`, `state.json` 也會一直停在 `running` (或 `stopping`) 到下一個指令接手, 會變的只有那個 pid 不在了
- **sleep 拉長**: 一輪三到五分鐘, 幾分鐘一次就夠
- **給一個上限**: 到了就停下來回報「等不到」, 那是一個有用的結論. 等一個 `ai_coc` run 的時候, 上限是它的 pid 不在了 (見上面「你是 subagent 的話反過來」那段), 不是一個固定的時數

**收工之前自己看一遍有沒有東西留在背景**, 真的去看而不是回想: 沒有 `ai_coc` 程序, 沒有 shell 還在輪詢, `state.json` 回到 `idle`. 刻意留著的就留著, 但回報裡要講是哪一個、為什麼. 主 session 不會採信「已經清掉了」這句話 (上面那個 subagent 被問兩次都這樣答, 八個都還在跑), 它會自己查一遍.

## 跑好幾個小時的圈

使用者開口要花而牆有指名 (`farm` 的「倉庫滿了」), 這一圈一跑就是好幾個小時, 他加一句「循環到我說停」「打到早上」就是要它跑到他叫停. 主 session 自己跑就照常: 每個指令送背景, 通知進來就接下一步.

**交給 subagent 跑, 就不要叫它「跑到叫停為止」, 給它固定的圈數.** 2026-10-08 凌晨一個被交代跑到早上的 subagent 打了 3 圈 (約 2 小時 20 分), 在一次刷牆跑完時結束了回合, 沒有回報, 之後傳給它的訊息也叫不醒, 模擬器閒了 40 分鐘才被發現. 改成每個只跑 3 圈 (打到 `stock_full` 加一次 `walls` 算一圈) 之後, 接手的每一個都跑完回報了. 所以:

- **交辦寫明跑幾圈、跑完回報**, 加一句「步驟之間不要結束回合, 回合只在最後的回報結束」, 再加一句「每一步各自一次呼叫, 跑完讀 `state.json`, 被叫停就回報」(見「中止」). 圈數照那一晚抓 3 圈
- **回報說圈數跑完了, 派它的 session 就派下一個**, 同一份交辦. 回報說它在半路停手 (`farm` 的「沒達成的時候講什麼」, 或資源花不掉了), 就照那份回報處理, 不要再派: 那些停手的理由換一個 subagent 一樣成立, 使用者在手機上玩的話, 每派一個就把他踢下線一次. 使用者叫停 (照「中止」) 也不派
- **派它的 session 留一個心跳**: 背景等大約一小時 (runtime 管的那種, 等完會叫醒你), 醒來讀 `state.json` 跟最新的執行目錄. 跟上次看比沒有往前走, 又沒收到回報, 就是它默默停了: 結束掉它, 派下一個. 默默結束回合的 subagent 什麼都不會送出來, 只有心跳抓得到; 每次醒來就再開一個, 直到這一圈收工
- **暫存檔放派它的 session 的暫存資料夾**, 交辦裡寫明路徑: 等待用的腳本跟重導向的輸出不要放 `~` 或 `~/.ai_coc` (同一晚留了十幾個 `.out` 在 `~/.ai_coc`, 要人手刪). 程式每次執行自己寫一個目錄, 本來就不必重導向

## 中止

**使用者叫停, 誰收到誰自己下 `ai_coc stop`.** 主 session 收到就自己跑, 不要只傳話給打資源的 subagent: subagent 卡在一個指令裡的時候, 要等那個指令回來才讀得到訊息, 那段時間迴圈照打. subagent 自己收到也一樣照下. 兩邊都收得到的時候, 使用者偏好主 session 自己下.

**跑 `uv run ai_coc stop`, 這是停止的唯一方式.** 它把 `~/.ai_coc/state.json` 的 `status` 改成 `stopping` 就秒回, 不碰遊戲也不找程序. 每個會碰模擬器的指令都讀同一份檔案, 所以不必先查在跑哪一個; 它會回你叫停的是什麼、pid 多少, 沒有人在跑就說沒有. 你帶的 `--agent`、`--session`、`--mission` 會記成 `stop_by`, 那一輪收工後的 `idle` 紀錄還留著, 被停的那一邊讀得到是誰、為什麼.

**有借用的時候 `stop` 分兩層**: 借用方的指令在跑, 它只停那個指令, 借用留著, 出借方之後照樣開回去; 沒有指令在跑, 它取消借用, 出借方不會開回去, 排隊中的那個也不會開跑. 所以要連打資源一起停, 等指令停了再下一次.

**不用再手動刪那份檔案.** 程序死掉留下的 `running` 不會卡住任何人: 下一個要用模擬器的指令查得出那個程序已經不在, 直接接手.

停在每個迴圈最安全的接縫: `attack` 在回合之間跟對手之間, `walls` 在批次之間跟開場掃描村莊時. 所以**停不是立刻的**, 最久要等當下那一場戰鬥打完或那一批城牆買完 (量過一次 107 秒). 夜世界在配對中被停, 而按 取消 的那一刻剛好配到對手, 那一場也照樣打完才停. 打到一半棄權會把軍隊丟在場上、遊戲停在回不了家的畫面, 比多等兩分鐘貴得多.

**怎麼知道停好了.** 背景指令自己結束: exit code 0, `result.json` 有內容 (排隊中被這個 stop 取消的指令例外: exit code 不是 0, `result.json` 是取消的那一行). `state.json` 的 `status` 是 `stopping` 表示收到了、還在打完手上那一場; 變成 `idle` (連同 `ended`) 才是真的收工.

**不要去砍背景程序.** 砍掉的話迴圈的 `KeyboardInterrupt` 出口執行不到 (只有前景的 Ctrl-C 走得到), 軍隊留在場上, 遊戲停在回不了家的畫面, 沒有 `result.json`, 只剩 `run.log` 能數. **卡死的也不用你砍**: 別的**不帶 `--yield`** 的指令等滿 `TAKEOVER_WAIT` (約 12 分鐘) 還等不到它收工, 會自己結束它並在自己的 `run.log` 記下是誰的 run; 下一個指令照常把遊戲帶回村莊. 視窗跟沒有 `pid_created` 的舊紀錄例外, 不會被結束, 那個指令只會報錯; 視窗就請使用者在視窗按停止. 帶 `--yield` 的指令不會結束任何人, 排在卡死的 run 後面會一直等 (見「為什麼一定要背景跑」).

**使用者說停, 停的是整件事而不是一個指令.** `state.json` 只停得下當下在跑的那個迴圈, 不知道你打算接著做什麼. 所以收到停止之後**不要再開下一個指令**, 只讀倉庫的 `stock`、`status` 也算, 除非使用者又說要繼續: 叫停就是他要接手, 回報的數字從 `run.log` 讀 (`farm` 的「沒達成的時候講什麼」). 這一條沒有機制在管, 只有你.

**你是 farm subagent 而那個停不是你下的, 又沒有借用**, 是有人用 `ai_coc stop` 真的叫停. 不要自己開回去, 馬上回報停在哪 (打的世界跟旗標, 執行目錄, 打了幾場, 最後一行 `holds` 的水位); 之後有人傳話叫你接回去, 就是上面說的「又說要繼續」, 同一個世界同一組旗標開回去.

**被叫停看 exit code 看不出來, 所以每一步跑完、開下一步之前都讀 `state.json`.** 在回合之間被停掉的 `attack` 照樣 exit 0, `result.json` 最後一個元素是那一輪自己的 `outcome` (多半是 `took_loot`), 不是 `stopped`; `world --go` 根本不讀叫停, 坐船時被停也照樣跑完. 看得出來的只有 `run.log` 結尾的 `Stop requested`, 跟 `state.json` 那筆 `idle` 紀錄上的 `stop_by`, 而下一個指令一 claim 就把 `stop_by` 蓋掉了. 所以只看剛跑完的那一步: `state.json` 的 `log` 是它的目錄, 而 `stop_by` 有值又不是你下的, 就是它被收掉了, 接著照「被借走」那三條分辨是借走還是叫停. `log` 不是剛跑完那一步的目錄, 那筆 `stop_by` 就是更早以前的 (使用者上一次叫停留下的會一直留到下一個指令 claim), 不算. **每一步各自一次呼叫, 不要把幾步寫成一支只看 exit code 的腳本** (`world --go … && attack … && walls …`): 2026-10-08 一個 subagent 這樣跑, 兩次 `ai_coc stop` 之後又刷了兩次牆、開了一場進攻.

**`ai_coc stop` 剛好下在兩步之間就什麼都不會寫**: 沒有指令在跑, 它只回 `現在沒有指令在跑`, subagent 的下一步照常開跑. 下 stop 的那一邊看到這句, 要馬上傳話給打資源的 subagent; 它一步一次呼叫的話, 下一步之前就收得到.

**這一條只管使用者叫停.** 迴圈自己跑完 (倉庫滿了, 輪數跑完) 的通知是接縫不是終點, 自己接下一步, 判斷在 `.agents/skills/farm/SKILL.md` 的「倉庫滿了」那節. 差別是誰按的停.

## 被借走

**你的 run 被一個不是你下的停止收掉, 先讀 `~/.ai_coc/state.json` 的 `loan`.** 帶 `--yield` 的 run 被別的指令要走時是借出去, 不是停掉: 那個指令叫它收工的同時寫下 `loan` (`lender` 是你, `borrower` 是誰借走、為了什麼), 你那輪的 `run.log` 最後也有一行 `Lent to …`.

- `loan` 在, `lender` 是你, `ended` 不是 `cancelled`: **馬上開回去**, 同一個世界同一組旗標, 都帶 `--yield`, **分兩步**: 先 `world --go <那個世界> --yield …` (借的那一邊可能把遊戲留在另一個村莊), 它會自己等 `giveback` 或借用逾時才真的開跑, 排隊可能超過一次前景呼叫的上限, 照常分段等; 它跑完照「中止」讀一次 `state.json`, 沒被叫停才開 `attack … --yield …`. 不要用 `&&` 串: 坐船時被叫停, `world --go` 照樣 exit 0, 串在後面的 `attack` 就打下去了. 不用等誰傳話, 最後的回報裡提一句被借走多久就好
- 開回去的指令 `result.json` 是 `借用被 ai_coc stop 取消了,這個指令沒有開跑` (exit code 不是 0): 有人在借用期間下了 `ai_coc stop`, 照「中止」處理, 不要再開
- 沒有 `loan`, 或 `ended` 是 `cancelled`: 那是真的叫停, 照「中止」處理

## 你自己要用畫面: 直接借, 用完還

**開發或驗證要動到活的遊戲, 直接下你的指令, 不用先停也不用問.** 這是使用者給過的授權, 原話是「測試時請自主停止打資源, 最後記得幫我重新開回去」. 只有一個模擬器, 沒有「小心一點同時跑」這個選項.

你的指令**不帶** `--yield`: 它會借走背景那輪, 等它打完手上那一場才開始 (跟 `ai_coc stop` 一樣, 要等多久見下一段), 那輪的 agent 讀到借用會自己排隊開回去 (「被借走」). 迴圈剛好在兩個指令之間的話, 你的指令直接開跑, 它的下一個指令會等你跑完, 你再下一個才借走它.

**借的指令通常要等一場戰鬥 (五分鐘上下) 才開始**, 對方等滿 `TAKEOVER_WAIT` (約 12 分鐘) 還不收工才會被結束: 前景跑的話那次呼叫的 timeout 給 runtime 允許的最長, 不然就背景跑. 被砍了就 `giveback`, 不然借用會留 30 分鐘, 迴圈白等.

**不 claim 的腳本自己借不到**: `spend-loot` 的 `eye.py` 跟 `build-feature` 的 `references/exploring.md` 那段腳本走 `commands._controller()`, 不碰 `state.json`, 迴圈在跑的時候照樣點得下去, 兩邊搶同一個畫面. 所以先用一個會 claim 的指令 (例如 `ai_coc stock`) 借到, 看 `state.json` 的 `loan` 在、`loan.ended` 是 null、`loan.borrower` 是你 (頂層的 `ended` 是你那個指令收工的時間, 不用看), 才跑腳本. 沒有 `loan` 是迴圈剛好在兩個指令之間, 什麼都沒借到, 再借一次. 那個指令要帶 `--session`: 沒帶的話它一跑完就把借用還回去. 借用從你最後一個會 claim 的指令跑完起算, 過了 `LOAN_TIME` (30 分鐘) 就失效, 腳本用得久就中間再跑一次續上. 用完照樣 `giveback`.

**測試要中途停**, `ai_coc stop` 只停你的指令, 借用還在, 用完一樣要還 (「中止」的兩層).

**背景那輪是你自己開的** (你就是那輪的 agent, 例如親自監督的 `watch-and-fix`), 借走的就是你自己的迴圈: 它收工時先不要開回去, 不然開回去的指令會排在你自己的借用後面. 測完、還了, 再照「被借走」開回去.

**要測的是一場戰鬥, 記得村莊多半是滿的**: `attack` 開場讀到每一項都滿過 `stop_at` 就收工, 連對手都不搜. 所以測試那一場給 `--stop-at 0`, 它只管這一次. 那是測試不是打資源, 搶回來的進不了滿倉.

**用完一定要還**: 最後一個要用模擬器的指令跑完, 下 `uv run ai_coc giveback`, 背景那輪就接著打. 這是那條授權的另一半: 忘了還, 它要等你最後一個指令跑完 30 分鐘才自己接回去, 這段時間模擬器閒著, 使用者卻以為在打. **每次測完就還, 不要攢著.** 例外有兩個, 都是不還, 改下 `ai_coc stop` 取消借用: 你的指令遇到別的裝置登入 (`另一部裝置登入了這個帳號…`), 還了或借用逾時, 打資源那一邊排著的指令就開跑, 它一開遊戲就把使用者踢下線; 還有使用者叫停, 還了的話打資源那一邊照樣開回去. **`giveback` 自己一步, 不要串在借用的指令後面** (像同一次呼叫裡的 `status; giveback`): 中途使用者叫停, 串在後面的那個照樣會跑. 2026-10-10 就是這樣, 借用被還回去之後, 下的 `ai_coc stop` 只回 `現在沒有指令在跑`, 打資源的 subagent 排著的指令就要開跑, 最後是連 subagent 一起停掉才收住.

還了之後在回覆裡講一句「已經還了, 背景那輪會接著打」, 使用者看不到背景指令的輸出.

## 遊戲卡住, 或者根本沒開

會操作畫面的指令都經過 `_controller()` 的 `ensure_coc`, 正常情況**不用自己開模擬器或遊戲** (只看的 `capture` 跟不帶 `--go` 的 `world` 例外, 模擬器沒開就報錯). 但它只處理得了「沒開」, 處理不了「開著但不理人」: 模擬器在跑、adb 也回話, 只有看畫面的人分得出來.

那種時候 `uv run ai_coc launch --restart game` 重開遊戲而不動模擬器, `--restart emulator` 連模擬器一起重開.

**`launch` 三種 scope 都會等村莊畫出來再把鏡頭放好**: 輪詢到村莊畫出來 (最久三分鐘, 等不到就放棄), pinch 回最遠, 再用 `park_camera` 滑到地圖角落, 從那裡把村莊拖到畫面中間停好 (pinch 不移動鏡頭, 所以兩步都要; 花錢買的場景拖好後會以畫面中間為準再拉近一次, 回到免費場景的大小). `LaunchReport.at_village` 說村莊有沒有出現, `outcome` 說沒出現是等到放棄 (`no_village`) 還是等的時候收到 `ai_coc stop` (`stopped`). 其他指令等得短得多: 會等村莊的 (`attack`、`stock`、`worker`、`lab`、`status`、`export` 走同一段 `_settle_game`, 走 `GameRunner._home` 的那幾個迴圈走自己的) 最多半分鐘上下, 等到了也會把鏡頭拉遠停好 (`attack` 只拉遠不停, 它開打前不點地圖, 夜世界看聖水車時自己停); `capture` 跟不帶 `--go` 的 `world` 不等也不動鏡頭, `world --go` 不等村莊, 但真的要坐船時會先 pinch 再 `park_camera`. 所以遊戲剛開而村莊還沒出來, 修法是 `launch` 而不是重跑原本的指令.

log 裡的 `stopped moving after 2 swipe(s)` 是正常的. `camera was still moving` 的 warning 是鏡頭沒停好: 坐船跟收聖水車會因此收工, `launch` 的 `_settle_game` 不會 (後面點的是固定的畫面角落), 所以看到這行而指令仍回報成功不是矛盾. 攻擊迴圈開頭只拉遠不停鏡頭, 它的 run.log 裡這行只會來自夜世界的收聖水車.

遊戲卡在載入畫面是另一回事: `launch` 一樣只等三分鐘, 放棄的那一行是 `Gave up after 180s waiting for the game: the game is on its loading screen`. 那是伺服器的事, 攻擊迴圈自己會等 (最多 45 分鐘, 見 `farm` 的警覺樣態), 不要一直重跑 `launch`.

**模擬器本身起不來**: 指令報 `模擬器尚未開放 ADB 連接埠：127.0.0.1:0`, 雷電開起來變成 1920x1080, 或者 5555 連不上. 那不是程式的問題, 照 `repair-emulator` 處理; 你是 subagent 的話停下來回報, 修是主 session 的事.

**三個都會把進行中的戰鬥打斷**, 跑之前先確認背景那輪真的停了.

## 絕對不要同時跑兩個

`attack`, `walls`, `collect`, `upgrade`, `donate` 全都在驅動同一個模擬器的同一個 display. 兩個一起跑, 一邊點開選單, 另一邊截到那張圖判讀成完全不同的東西, 而且沒有任何機制會發現. 開下一個之前先確定上一個真的結束了.

**`state.json` 是一把鎖.** 除了只看的指令 (`read`、`stop`、`giveback`、`export --last`、`capture`、不帶 `--go` 的 `world`), 每個 `ai_coc` 指令開跑時先看有沒有別的活著的程序佔著 (`cli.py` 的 `_claim_for`, `commands.py` 的 `claim` 跟 `take_over`): 有, 就照 `ai_coc stop` 的流程請它收工, 記下是你叫停的, 等它打完手上那一場退出才開始; 它是帶 `--yield` 開的 (打資源的迴圈都是) 就是借走, 用完要 `giveback`. 帶 `--yield` 的指令反過來, 誰都不請, 排隊等. 所以**迴圈在跑的時候下 `stock`、`status`、`worker` 這種要操作畫面的指令, 就是把那輪借走**, 不是你要的就不要下. 只想看畫面, 用 `capture` 或 `repair-emulator` 的 `look.py` (`uv run --no-sync python .agents/skills/repair-emulator/scripts/look.py <資料夾>`): 都只截圖, 不佔鎖也不開任何東西, 代價是跟迴圈多搶一條 ADB, 跟下面 `--shot-every` 的心跳同一類.

**而「上一個」不一定是你開的**, 使用者可能同時開著另一個 session. 所以**開第一個會碰模擬器的指令之前, 先讀 `~/.ai_coc/state.json`**:

- `status` 是 `running` 或 `stopping`: 有人在跑. `command` 是哪個指令, `log` 是它那次執行的目錄, `caller` 是誰為了什麼開的. 你下會操作畫面的指令就會停掉它 (`yields` 是 `true` 的話是借走); 不是你要的就不要下, 跟使用者說一聲
- `status` 是 `idle`: 沒人在跑, `command` 跟 `ended` 說的是上一次. 可以開
- 檔案不存在: 這台機器沒跑過, 或者有人剛手動刪掉它來叫停

**`running` 有可能是殘留**, 程序被砍掉的話沒有人把它寫回 `idle`. 不用自己查 pid: 指令開跑時會查 (pid 還在, 是 Python 或 `ai_coc`, 而且就是寫那筆紀錄的那個程序), 殘留就直接接手. **讀檔這一步每次開工都做**, 為的是知道你要停掉的是誰的 run.

**這個檔有兩件事看不到, 2026-09-21 兩件都出過事.**

- **監控**: 被交辦盯工人的 session 每隔一陣子跑一次 `ai_coc status`, 兩次之間檔案寫的是 `idle`, 從外面看不見. `ai_coc stop` 也碰不到它, 在迴圈的是 agent 不是程序, 只有殺掉那個 agent 才停得下來. 而且它每跑一次 `status` 都會借走當下在跑的迴圈, 沒 `giveback` 的話那輪要等 30 分鐘才接回去
- **使用者本人**: 他用手機玩就會把模擬器踢下線, 在另一台模擬器 (MuMu 跟雷電登同一個帳號) 開遊戲也一樣. 那天一次 `builders` 把被踢下線的畫面當成普通遮擋, 花 61 秒按 `back` 跟答 `取消`, 回報 `畫面沒辦法回到村莊`; 重跑時 `ensure_coc` 又把登入搶回來, 把使用者再踢一次. **所以在人可能正在玩的時候回不到村莊, 是停下來講一聲的理由, 不是再試一次的理由.**

`--shot-every` 的心跳是同一個程序裡的執行緒, 跟迴圈搶同一條 ADB 連線 (一張 `screencap -p` 0.6 到 0.8 秒), 有代價但可控; 兩個獨立的指令不是.

## 空檔要拿來做事

戰鬥的三分鐘, 沒搜到對手後的那六十秒, 刷牆掃描村莊的時間, 都拿來讀程式碼跟改東西. 反過來, 手上的事做完而背景還在跑, 不要每三十秒戳一次 log 假裝在做事: 跟使用者說一聲在等什麼, 然後真的等.
