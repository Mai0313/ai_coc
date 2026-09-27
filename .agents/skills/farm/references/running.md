# 讓一輪在背景跑, 並且盯得住它

`farm`, `watch-and-fix`, `build-feature`, `spend-loot` 四個 skill 共用這一份, 路徑是 `.agents/skills/farm/references/running.md`.

## 為什麼一定要背景跑

一輪進攻四到五分鐘起跳, `--repeat 0` 打到倉庫滿可能是好幾個小時. **主 session 的角色是監督, 不是執行**: 迴圈在背景跑, 你在前景讀 log、改東西、回答使用者, 他隨時插得了話.

**會跑很久的都用背景送出去, 這裡是唯一一份清單**: `attack`; `walls` (掃描加購買好幾分鐘); `upgrade` 跟 `hero` (找不到目標會退回 `_sweep`, 兩分半起跳); `donate --rounds 0`. 幾秒就回來的前景跑: `world`, `read`, `collect`, `capture`, `view`, `builders`; `world --go` 真的坐船約一分鐘, 也前景跑.

**你是 subagent 的話反過來, 在前景阻塞.** 背景指令跑完的通知只送到主對話, 也沒有人在跟你講話, 所以 subagent 把指令 (或等它的迴圈) 丟到背景再結束回合, 就會停在那裡不動. **subagent 要在前景阻塞**, 而一次阻塞的呼叫有 runtime 自己的 timeout 上限 (常見是十分鐘, 預設值往往更短), 所以分段等: 一段比那個上限短, **那次呼叫的 timeout 要設得比一段長** (沒設的話一段還沒等完就被砍), 等不到就再下一段. 每一段照「等待要有出口, 收工要清乾淨」寫, 不要只看 `result.json`:

```bash
RUN=~/.ai_coc/logs/<這次的目錄>
for i in 1 2 3; do   # 一段最多九分鐘
    [ -s "$RUN/result.json" ] && break
    grep -q '"status": "idle"' ~/.ai_coc/state.json && break
    sleep 180
done
```

`RUN` 從這次執行 log 的第一行拿 (`This run is being kept in ...`), 不要拿 `ls -t` 最上面那個: 新目錄還沒建好之前, 最上面的是上一次, 它的 `result.json` 已經有內容, 等待會立刻結束, 接著你就在還在跑的那一輪上面再開一輪.

一輪四五分鐘, 所以 `--repeat 3` 要兩段左右, `--repeat 0` 要很多段. **每一段等完還沒結束, 就看 `state.json` 的 `pid` 還在不在**: 還在就是 run 還活著, 接著等下一段 (順便看 `run.log` 有沒有往前走); 不在了就是被砍掉 (這種 run 會一直停在 `running`), 停下來回報. 這個 pid 檢查就是整段等待的出口. 前景卡住對 subagent 沒有代價.

**pid 還在但 `run.log` 超過一場戰鬥的時間 (五分鐘上下; 夜世界配對最久量過五分半) 沒有新的一行, 是卡死了**, 只有一個例外: 最後一行是 `waiting for the server rather than tapping` 的話, 迴圈在等伺服器, 最久 45 分鐘不寫 log, 那是正常的, 照常等. 卡死的樣子是這樣: 模擬器重開之後停在 `Launching com.supercell.clashofclans` 九分鐘以上就出過, 那次模擬器已經掛了而等待一直沒結束. 不要再等下一段: 用 `repair-emulator` 的 `look.py` 看畫面 (它不佔用模擬器), 回報 pid, log 最後一行跟那張畫面.

## 開跑

```bash
uv run ai_coc world --go day --agent <名字> --session <session id> --mission "打日世界資源"
uv run ai_coc attack --repeat 0 --record --agent <名字> --session <session id> --mission "打日世界資源"
```

**你是跟開發同時跑的 farm subagent, 就在主 checkout (預設分支) 上跑, 每個指令都寫成 `uv run --no-sync ai_coc …`**, `stop` 也一樣: 開發在另一個 worktree 做, 理由在 `AGENTS.md` 的 The loop runs the main checkout. 真的要同步 (merge 帶進新的依賴, 指令 import 失敗) 就等手上的程序結束再 `uv sync`.

**每一個 `ai_coc` 指令都帶 `--agent`、`--session`、`--mission`**, 其他 skill 裡寫的指令也一樣: 你自己的名字 (`claude-code`、`antigravity`、`codex`), 你自己的 session id, 這一趟在做什麼 (一句話). 每個指令都把它們寫進自己 `run.log` 的第二行; 會佔用模擬器的指令還會寫進 `state.json` 的 `caller`, 所以那裡記的永遠是開始這一輪的人, 而 `stop` 是誰下的要去翻 `*-stop` 那幾個執行目錄. 別的 session 就是靠這些查出是誰在開模擬器. 旗標是選填的, 那是留給使用者手動打指令; agent 一律要帶, 沒帶的話 log 會留一行 warning.

**先切到要打的村莊, 再開.** `attack` 打的是遊戲當下停著的村莊, 遊戲會開在上次離開的那一個, 而沒有指令會自己坐船. `world --go` 沒切成就不要開 `attack`. 夜世界是 `world --go night`, 前景跑.

**送到背景只能用 runtime 自己管的背景執行** (跑完會通知你的那一種): 它是唯一跑完會把你叫醒的開法, harness 送進對話的那個通知就是接下一步的時刻. `Start-Process`, `nohup … &`, PowerShell 的 job, 任何 shell 層的 detach 都不行: 程序照跑, 但結束時沒有東西叫醒你, 輸出跟 exit code 也回不來 (量過: 夜世界打到 `stock_full` 自己收工之後, session 就停在原地). **不必自己重導向**, 程式一次執行寫一個目錄:

```
~/.ai_coc/logs/2026-08-29-011423-attack/
├── run.log        # 這次執行的完整紀錄, 純文字
├── result.json    # 這次的答案, 跑完才會有內容
├── plans.jsonl    # 只有 attack 有: 一場一行, 那一場的整份戰術
└── frames/        # 迴圈自己讀的畫面要開 --record; capture 一定會有; world 跟 launch 截得到畫面卻找不到村莊時留 no_village.png (log 會寫路徑)
```

**目錄在開跑第一行 log** (`This run is being kept in ...`). 名字是「時間-指令」, `ls -t ~/.ai_coc/logs` 最上面是最近已經建好的一次 (剛開跑的那一次可能還沒建好). 任何指令加 `--label <名字>` 會接在後面 (`…-attack-baseline`), 給之後要找回來的那一次用.

**畫面只留七天, log 永久留.** 過期的 `frames/` 在下一次有指令開跑時刪掉, 同目錄的 `run.log`、`result.json`、`plans.jsonl` 不動, 刪了幾個會寫進 log. 要留久的證據自己複製一份.

**`result.json` 是一個陣列, 不是一個物件.** `attack` 寫的是 `AttackSeries`, 一輪一個 `AttackReport`, 頂層沒有 `stock_full`. 「這個世界打完了沒」看**最後一個元素**:

```bash
jq '.[-1] | {world, outcome, stock_full, attacked}' ~/.ai_coc/logs/<run>/result.json
```

`jq '.stock_full'` 對陣列直接報錯, 別的讀法多半拿到不是 `true` 的東西, 讀起來像「還沒滿」. 其他指令 (`walls`, `collect`, `world` ...) 寫的是單一個物件.

`--record` 留下迴圈讀的每一張 (每一張都是模擬器的一次 PNG 編碼, 平均兩 MB 多: `AGENTS.md` 量過 11 456 張佔 27.2 GB), 這批會有東西要查就開. `--shot-every` 另加一條固定心跳 (`tick_00012.3s.png`), 要搭配 `--record`, 補兩張之間看不到的那段.

## 盯

`run.log` 用搜尋抓有意義的行, 不要整份讀進來 (一小時幾千行):

- `Round \d+ of`: 現在第幾輪
- `Attack finished:`: 每輪一行, 句子是照 `AttackReport.outcome` 組的; 下判斷讀 `result.json` 的 `outcome`, 不要比對這裡的字
- `WARNING` 跟 `ERROR`: 真的要看的

**幾分鐘看一次就好.** 背景指令跑完會通知你, 那才是必須處理的時刻.

**每一次看都要讀完就回來.** 不要掛在 `run.log` 上等新行進來: 在 Windows 上那種讀法會一直握著檔案, 活得比開它的回合久, 那次執行的目錄就再也刪不掉, 要使用者自己開工作管理員去殺 (量過五個 session 留下的殘留壓著 11.2 GB). 要看新的就再讀一次.

**通知進來, 先讀 `result.json` 的最後一個元素, 再決定下一步.** 檔案是空的, 或 exit code 不是 0: 程序被砍掉或崩掉, `run.log` 最後幾十行是唯一的線索, 遊戲多半停在回不了家的畫面, 先看畫面再決定. 檔案完整的話, 最後一個元素加上 `run.log` 最後幾行說它怎麼結束:

- `stock_full` 是 `true`: 這個世界打滿了, **是接縫不是終點**. 兩個世界都要打就換另一個; 日世界要花的話照 `farm` 的「倉庫滿了」那節. 不要回頭問使用者要不要繼續
- `run.log` 結尾有 `Stop requested`: 有人下了 `ai_coc stop` (停在回合之間或兩輪中間的等待都會留這一行). 誰停的看 log, 現在還有沒有人在跑看 `~/.ai_coc/state.json`. 不是你下的就不要自己開回去, 見「中止」
- 使用者指定的 `--repeat N` 跑完: 照他的交辦接下去或收工
- 有幾輪 `outcome` 是 `emulator_silent`: 模擬器當下不理人, 夾在中間一兩輪不是事, 照常打. 但結尾是 `The emulator has not answered for 3 rounds; ending the series` 就是連續三輪, 整個 series 收工: 先看模擬器還活著沒有, 不要直接開下一個. 這種有完整的 `result.json`, 不要跟被砍掉的搞混
- 都不是就是迴圈自己放棄了, `outcome` 跟最後一條 WARNING / ERROR 說原因, 例如打到一半遊戲跑到另一個村莊 (最後一個元素是 `other_village`, 多半是有人切過村莊, 例如使用者在手機上玩). 這幾種歸 `farm` 的「其他停手的理由」, 先把遊戲弄回村莊, 不要直接開下一個

一個 run 結束而你什麼都沒接, 模擬器就閒著, 使用者卻以為還在打.

`plans.jsonl` 在同一個目錄, 一場一行, 是那一輪 AI 回答的整份戰術. `jq -c 'select(.round==3)' plans.jsonl` 拿一輪出來看, `jq -r '"\(.round) \(.plan.deploy_from)"' plans.jsonl` 一眼看完整批打了哪些側邊. **一輪打壞先讀它**: 它說打算怎麼打, `run.log` 說實際做了什麼, 對照才知道問題在指揮還是在執行.

跨好幾次跑找同一個症狀用 `grep -r ~/.ai_coc/logs/*/run.log`, 它會說每一筆出自哪一次; 這台機器最近做過什麼看 `ls -t ~/.ai_coc/logs`.

## 等待要有出口, 收工要清乾淨

**任何一個等待的迴圈都必須自己結束得了.** 只等 `result.json` 出現的寫法, 在 run 被打死時永遠等不到, 而那正是最該有人接手的時候 (量過 2026-09-16: 一個 subagent 留了四個 `until [ -s "$RUN_DIR/result.json" ]; do sleep 15; done` 在背景, 等的那一輪被 bug 打死, 八個 process 空轉了三十五個小時, 是使用者發現的). 所以一個等待要同時:

- **不要只看 `result.json`**: 也看 `~/.ai_coc/state.json` 的 `status` 有沒有回到 `idle`, 或那個 pid 還在不在. 死掉的 run 不寫檔案, 但也不會一直是 `running`
- **sleep 拉長**: 一輪三到五分鐘, 幾分鐘一次就夠
- **給一個上限**: 到了就停下來回報「等不到」, 那是一個有用的結論. 等一個 `ai_coc` run 的時候, 上限是它的 pid 不在了 (見上面「你是 subagent 的話反過來」那段), 不是一個固定的時數

**收工之前自己看一遍有沒有東西留在背景**, 真的去看而不是回想: 沒有 `ai_coc` 程序, 沒有 shell 還在輪詢, `state.json` 回到 `idle`. 刻意留著的就留著, 但回報裡要講是哪一個、為什麼. 主 session 不會採信「已經清掉了」這句話 (上面那個 subagent 被問兩次都這樣答, 八個都還在跑), 它會自己查一遍.

## 中止

**跑 `uv run ai_coc stop`, 這是停止的唯一方式.** 它把 `~/.ai_coc/state.json` 的 `status` 改成 `stopping` 就秒回, 不碰遊戲也不找程序. 每個會碰模擬器的指令都讀同一份檔案, 所以不必先查在跑哪一個; 它會回你叫停的是什麼、pid 多少, 沒有人在跑就說沒有.

手動刪掉那份檔案也會停, 那是留給叫得動遊戲的東西已經不在的情況 (agent 掛了, 終端機沒了); 平常用 `ai_coc stop`, 它留得下記錄.

停在每個迴圈最安全的接縫: `attack` 在回合之間跟對手之間, `walls` 在批次之間跟開場掃描村莊時. 所以**停不是立刻的**, 最久要等當下那一場戰鬥打完或那一批城牆買完 (量過一次 107 秒). 打到一半棄權會把軍隊丟在場上、遊戲停在回不了家的畫面, 比多等兩分鐘貴得多.

**怎麼知道停好了.** 背景指令自己結束: exit code 0, `result.json` 有內容. `state.json` 的 `status` 是 `stopping` 表示收到了、還在打完手上那一場; 變成 `idle` (連同 `ended`) 才是真的收工.

**不要去砍背景程序.** 砍掉的話迴圈的 `KeyboardInterrupt` 出口執行不到 (只有前景的 Ctrl-C 走得到), 軍隊留在場上, 遊戲停在回不了家的畫面, `result.json` 是空的, 只剩 `run.log` 能數. 真的非硬停不可才砍, 砍完自己確認遊戲回到村莊.

**使用者說停, 停的是整件事而不是一個指令.** `state.json` 只停得下當下在跑的那個迴圈, 不知道你打算接著做什麼. 所以收到停止之後**不要再開下一個指令**, 除非使用者又說要繼續. 這一條沒有機制在管, 只有你.

**你是 farm subagent 而那個停不是你下的**, 多半是主 session 要借模擬器去測 (`AGENTS.md` 的 Taking the game for a live test). 不要自己開回去, 馬上回報: 你的回報送到, 就是它知道畫面空出來的訊號. 回報寫明停在哪 (打的世界跟旗標, 執行目錄, 累計進帳), 基準留在你手上; 它之後傳話叫你接回去, 就是上面說的「又說要繼續」, 同一個世界同一組旗標開回去.

**這一條只管使用者叫停.** 迴圈自己跑完 (倉庫滿了, 輪數跑完) 的通知是接縫不是終點, 自己接下一步, 判斷在 `.agents/skills/farm/SKILL.md` 的「倉庫滿了」那節. 差別是誰按的停.

## 你自己要用畫面: 自己停, 用完自己開回去

**開發或驗證要動到活的遊戲, 就自己把背景那輪停掉, 不用問.** 這是使用者給過的授權, 原話是「測試時請自主停止打資源, 最後記得幫我重新開回去」. 只有一個模擬器, 沒有「小心一點同時跑」這個選項.

停之前**先記下三件事**, 等一下要接回同一件工作:

- 打的是哪個世界跟原本那組旗標. 世界從那次的 `run.log` 讀 (每輪開頭 `Attack run starts` 是主村, `Builder base attack run starts` 是夜世界), **不要趁迴圈還在跑下 `ai_coc world`** (見「絕對不要同時跑兩個」). 開回去之前先 `world --go` 切回那一邊, 你的測試可能把遊戲留在另一邊
- 那次執行的目錄 (迴圈在跑時 `state.json` 的 `log`; `ls -t ~/.ai_coc/logs` 最上面的不一定是它), 累計進帳從它的 `result.json` 數
- 停下來當下的倉庫水位 (迴圈退出之後跑 `ai_coc stock`), 那是下一段的起點

然後 `uv run ai_coc stop`, **等它真的退出**再動遊戲 (見上一節).

**要測的是一場戰鬥, 記得村莊多半是滿的**: `attack` 開場讀到每一項都滿過 `stop_at` 就收工, 連對手都不搜. 所以測試那一場給 `--stop-at 0`, 它只管這一次. 那是測試不是打資源, 搶回來的進不了滿倉.

**用完一定要開回去**, 同一個世界同一組旗標. 這是那條授權的另一半: 忘了開回去, 模擬器從你測試之後就一直閒著, 使用者卻以為整晚在打, 沒有東西會提醒他. **每次測完就開回去, 不要攢著.**

開回去之後在回覆裡講一句「已經接回去了, 現在是第幾輪」, 使用者看不到背景指令的輸出.

## 遊戲卡住, 或者根本沒開

每個指令都經過 `_controller()` 的 `ensure_coc`, 正常情況**不用自己開模擬器或遊戲**. 但它只處理得了「沒開」, 處理不了「開著但不理人」: 模擬器在跑、adb 也回話, 只有看畫面的人分得出來.

那種時候 `uv run ai_coc launch --restart game` 重開遊戲而不動模擬器, `--restart emulator` 連模擬器一起重開.

**`launch` 三種 scope 都會等村莊畫出來再把鏡頭放好**: 輪詢到村莊畫出來 (最久三分鐘, 等不到就放棄), pinch 回最遠, 再用 `park_camera` 滑到地圖角落停好 (pinch 不移動鏡頭, 所以兩步都要). `LaunchReport.at_village` 說村莊有沒有出現, `outcome` 說沒出現是等到放棄 (`no_village`) 還是等的時候收到 `ai_coc stop` (`stopped`). 其他指令等得短得多: 會等村莊的 (`attack`、`stock`、`worker`、`lab`、`status`、`export` 走同一段 `_settle_game`, 走 `GameRunner._home` 的那幾個迴圈走自己的) 最多半分鐘上下, 等到了也會把鏡頭拉遠停好; `world` 跟 `capture` 不等也不動鏡頭. 所以遊戲剛開而村莊還沒出來, 修法是 `launch` 而不是重跑原本的指令.

log 裡的 `stopped moving after 2 swipe(s)` 是正常的. `camera was still moving` 的 warning 是鏡頭沒停好: 坐船跟收聖水車會因此收工, `launch` 跟攻擊迴圈的 `_settle_game` 不會 (後面點的是固定的畫面角落), 所以看到這行而指令仍回報成功不是矛盾.

遊戲卡在載入畫面是另一回事: `launch` 一樣只等三分鐘, 放棄的那一行是 `Gave up after 180s waiting for the game: the game is on its loading screen`. 那是伺服器的事, 攻擊迴圈自己會等 (最多 45 分鐘, 見 `farm` 的警覺樣態), 不要一直重跑 `launch`.

**模擬器本身起不來**: 指令報 `模擬器尚未開放 ADB 連接埠：127.0.0.1:0`, 雷電開起來變成 1920x1080, 或者 5555 連不上. 那不是程式的問題, 照 `repair-emulator` 處理; 你是 subagent 的話停下來回報, 修是主 session 的事.

**三個都會把進行中的戰鬥打斷**, 跑之前先確認背景那輪真的停了.

## 絕對不要同時跑兩個

`attack`, `walls`, `collect`, `upgrade`, `donate` 全都在驅動同一個模擬器的同一個 display. 兩個一起跑, 一邊點開選單, 另一邊截到那張圖判讀成完全不同的東西, 而且沒有任何機制會發現. 開下一個之前先確定上一個真的結束了.

**只讀的指令也算.** 除了 `read`、`stop` 跟 `export --last`, 每個 `ai_coc` 指令 (`world`、`stock`、`worker`、`status`、`capture` 都在內) 開頭把 `state.json` claim 成自己的, 結束時寫回 `idle` (`cli.py` 的 `WITHOUT_CLAIM` 跟 `_claim_for`, `commands.py` 的 `claim`). 迴圈在跑時插一個進去, 還沒生效的 `stopping` 被它的 claim 蓋掉, 結束後檔案說沒人在跑, `ai_coc stop` 回「沒有東西要停」, 背景那輪再也 `stop` 不到, 要停只剩手動刪掉 `state.json` (見「中止」). 迴圈在跑時要看畫面, 用 `repair-emulator` 的 `look.py` (`uv run --no-sync python .agents/skills/repair-emulator/scripts/look.py <資料夾>`): 它只截圖, 不 claim, 也不點任何東西, 代價是跟迴圈多搶一條 ADB, 跟下面 `--shot-every` 的心跳同一類.

**而「上一個」不一定是你開的**, 使用者可能同時開著另一個 session. 所以**開第一個會碰模擬器的指令之前, 先讀 `~/.ai_coc/state.json`**:

- `status` 是 `running` 或 `stopping`: 有人在跑. `command` 是哪個指令, `log` 是它那次執行的目錄. 不要開第二個, 跟使用者說一聲
- `status` 是 `idle`: 沒人在跑, `command` 跟 `ended` 說的是上一次. 可以開
- 檔案不存在: 這台機器沒跑過, 或者有人剛手動刪掉它來叫停

**`running` 有可能是殘留**, 程序被砍掉的話沒有人把它寫回 `idle`. 拿裡面的 `pid` 去 `Get-Process -Id <pid>`, 查不到就是殘留, 直接開你的, 你的 claim 會蓋掉它 (193 ms). **這一步每次開工都做**, 不是只在看起來不對的時候.

**這個檔有兩件事看不到, 2026-09-21 兩件都出過事.**

- **監控**: 被交辦盯工人的 session 每隔一陣子跑一次 `ai_coc worker`, 兩次之間檔案寫的是 `idle`, 從外面看不見. `ai_coc stop` 也碰不到它, 在迴圈的是 agent 不是程序, 只有殺掉那個 agent 才停得下來
- **使用者本人**: 他用手機玩就會把模擬器踢下線, 在另一台模擬器 (MuMu 跟雷電登同一個帳號) 開遊戲也一樣. 那天一次 `builders` 把被踢下線的畫面當成普通遮擋, 花 61 秒按 `back` 跟答 `取消`, 回報 `畫面沒辦法回到村莊`; 重跑時 `ensure_coc` 又把登入搶回來, 把使用者再踢一次. **所以在人可能正在玩的時候回不到村莊, 是停下來講一聲的理由, 不是再試一次的理由.**

`--shot-every` 的心跳是同一個程序裡的執行緒, 跟迴圈搶同一條 ADB 連線 (一張 `screencap -p` 0.6 到 0.8 秒), 有代價但可控; 兩個獨立的指令不是.

## 空檔要拿來做事

戰鬥的三分鐘, 沒搜到對手後的那六十秒, 刷牆掃描村莊的時間, 都拿來讀程式碼跟改東西. 反過來, 手上的事做完而背景還在跑, 不要每三十秒戳一次 log 假裝在做事: 跟使用者說一聲在等什麼, 然後真的等.
