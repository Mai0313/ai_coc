---
name: share-emulator
description: >-
  在這台唯一的模擬器上下 `ai_coc` 指令之前要知道的事, 背景常常有別人的打資源迴圈在跑: `~/.ai_coc/state.json` 這把鎖怎麼讀, 哪些指令會佔模擬器, 怎麼借走帶 `--yield` 的迴圈、用完怎麼 `giveback`, `ai_coc stop` 停得到什麼、停不到什麼, 哪些指令要背景跑、背景的 run 怎麼等跟怎麼盯, 遊戲卡住或沒開時怎麼用 `launch`, merge 進來的修正怎麼讓迴圈用上. 做 live test、點升級面板、讀 `status`、修模擬器, 或者任何要在別人的迴圈旁邊或在背景下指令的時候用.
user-invocable: false
---

# share-emulator

只有一台模擬器, 而背景常常有一個打資源的迴圈在跑. 這份寫的是 agent 要怎麼做; 鎖在程式那一側的語意 (怎麼 claim, 借用怎麼記, 活著怎麼判) 在 `.agents/rules/runtime.md` 的 Stopping and the state file, 這裡不重抄. 每個 `ai_coc` 指令都帶 `--agent`、`--session`、`--mission` (`AGENTS.md` 的 Say who is driving), 從主 checkout 下的一律寫成 `uv run --no-sync ai_coc …` (`AGENTS.md` 的 The loop runs the main checkout); 下面的例子為了好讀省略了.

## 開工先讀 `state.json`

**`~/.ai_coc/state.json` 分三塊**: `emulator` 是佔著模擬器的那個指令, `watch` 是睡著等升級的 `ai_coc watch`, `notes` 是使用者對每個村莊的常設交代. 這份跟其他 skill 講到 `status`、`pid`、`log`、`command`、`caller`、`stop_by`、`yields`、`loan` 而沒說是哪一塊的, 都是 `emulator` 那一塊.

`attack`, `collect`, `donate`, `confirm` 全都在驅動同一個模擬器的同一個 display. 兩個一起跑, 一邊點開選單, 另一邊截到那張圖判讀成完全不同的東西, 而且沒有任何機制會發現. 開下一個之前先確定上一個真的結束了.

**`state.json` 是一把鎖.** 除了 `read`、`stop`、`giveback`、`watch`、`note`、`growth`、`turnaround`、`capture`、`export --last` 跟不帶 `--go` 的 `world`, 每個 `ai_coc` 指令開跑時先看有沒有別的活著的程序佔著: 有, 就照 `ai_coc stop` 的流程請它收工, 記下是你叫停的, 等它打完手上那一場退出才開始; 它是帶 `--yield` 開的 (打資源的迴圈都是) 就是借走, 用完要 `giveback`. 帶 `--yield` 的指令反過來, 誰都不請, 排隊等. 所以**迴圈在跑的時候下 `stock`、`status` 這種要操作畫面的指令, 就是把那輪借走**, 不是你要的就不要下. 只想看畫面, 用 `capture` 或 `repair-emulator` 的 `look.py` (`uv run --no-sync python .agents/skills/repair-emulator/scripts/look.py <資料夾>`): 都只截圖, 不佔鎖也不開任何東西, 代價是跟迴圈多搶一條 ADB.

**而「上一個」不一定是你開的**, 使用者可能同時開著另一個 session. 所以**開第一個會碰模擬器的指令之前, 先讀 `~/.ai_coc/state.json`**:

- `status` 是 `running` 或 `stopping`: 有人在跑. `command` 是哪個指令, `log` 是它那次執行的目錄, `caller` 是誰為了什麼開的. 你下會操作畫面的指令就會停掉它 (`yields` 是 `true` 的話是借走); 不是你要的就不要下, 跟使用者說一聲
- `status` 是 `idle`: 沒人在跑, `command` 跟 `ended` 說的是上一次. 可以開
- 檔案不存在: 這台機器沒跑過, 或者有人剛手動刪掉它來叫停

`running` 有可能是殘留, 那由指令開跑時自己查 (`AGENTS.md` 的 Look before driving), 不用你查 pid.

## 背景跑

一輪進攻四到五分鐘起跳, `--repeat 0` 打到倉庫滿可能是好幾個小時. **主 session 的角色是監督, 不是執行**: 迴圈在背景跑, 你在前景讀 log、改東西、回答使用者, 他隨時插得了話. 打資源的迴圈交給一個 farm subagent 跑, 它照 `.agents/skills/takeover/references/runner.md` 在前景分段阻塞; 主 session 自己送背景的, 是 `watch-and-fix` 親自盯的迴圈跟其他會跑很久的指令.

**會跑很久的都用背景送出去, 這裡是唯一一份清單**: `attack`; `donate --rounds 0`. `watch` 睡到 `--next` 才結束, 一定背景跑. 幾秒就回來的前景跑: `world`, `read`, `collect`, `capture`, `view`, `tap`, `swipe`, `confirm`; `world --go` 真的坐船約一分鐘, `status` 要走設定頁拿村莊匯出, 也約一分鐘, 都前景跑; 被借走之後開回去的那一次例外, 它可能要排隊很久. 迴圈在跑的時候, 其中會 claim 的要先等它打完那一場 (見「借, 用完還」).

**送到背景只能用 runtime 自己管的背景執行** (跑完會通知你的那一種): 它是唯一跑完會把你叫醒的開法, harness 送進對話的那個通知就是接下一步的時刻. `Start-Process`, `nohup … &`, PowerShell 的 job, 任何 shell 層的 detach 都不行: 程序照跑, 但結束時沒有東西叫醒你, 輸出跟 exit code 也回不來 (量過: 夜世界打到 `stock_full` 自己收工之後, session 就停在原地).

**背景指令跑完叫不醒你的 runtime, 改用它自己的排程叫醒自己.** 有的 runtime 背景指令結束不會通知你, 只有排程叫得醒對話 (量過: 一個 `ai_coc watch` 在 `--next` 那一刻結束, 對話一直到排程觸發才醒). 在那種 runtime 上要睡到某個時間 (`takeover` 的第 2 步、派 farm subagent 時的心跳):

- `takeover` 的 `ai_coc watch` 照開: 別的 session 靠它知道有人在顧, `ai_coc stop` 也停得到它. 心跳不開 watch, 只設排程: 新開的 watch 會把別的 session 睡著的那個請下來
- 另外設一個排程叫醒你, 用「每天 HH:MM」這種固定時刻, 取 `--next` 那一分鐘的下一分鐘 (排程只到分鐘, 準時響的話會比 watch 早幾十秒). 不要用「每隔 N 小時」: 固定間隔照它自己的節奏走, 對不準升級好的時間. 固定時刻的排程哪次忘了改, 隔天同一時間還會叫醒你, 圈不會就此斷掉
- 整件事只留一個排程: 每次醒來排好下一次, 就把同一個排程改到新的時間, 不另外建
- 醒來時 watch 還在睡 (`state.json` 的 `watch` 還是你的, `next_at` 還沒到), 是排程先響了 (要等超過一天的話, 每天那個時刻都會先響一次): 什麼指令都不跑, 直接結束回合
- `ai_coc stop` 停掉的 watch 叫不醒你, 要等下一次排程才讀得到 `stopped`
- 收工就刪掉: 使用者叫停、`session_taken`、或不再需要醒. `ai_coc stop` 停不到 runtime 的排程, 留著它會一直叫醒你, 每次都花一輪用量
- 你自己在背景跑的 `attack` 跑完也叫不醒你, 它什麼時候結束事先不知道, 排程也排不準: 照 `runner.md` 的方式在前景分段等

排程晚個兩三分鐘觸發是常態, 不用為它提早.

**盯背景的 run**, `run.log` 用搜尋抓有意義的行, 不要整份讀進來 (一小時幾千行): `Round \d+ of` 是現在第幾輪; `Attack finished:` 每輪一行, 句子是照 `AttackReport.outcome` 組的, 下判斷讀 `result.json` 的 `outcome`, 不要比對這裡的字; `WARNING` 跟 `ERROR` 是真的要看的. **幾分鐘看一次就好**, 背景指令跑完會通知你, 那才是必須處理的時刻. **每一次看都要讀完就回來.** 不要掛在 `run.log` 上等新行進來: 在 Windows 上那種讀法會一直握著檔案, 活得比開它的回合久, 那次執行的目錄就再也刪不掉, 要使用者自己開工作管理員去殺 (量過五個 session 留下的殘留壓著 11.2 GB). 要看新的就再讀一次. 手上的事做完而背景還在跑, 不要每三十秒戳一次 log 假裝在做事: 跟使用者說一聲在等什麼, 然後真的等.

**任何一個等待的迴圈都必須自己結束得了.** 只等 `result.json` 出現的寫法, 在 run 被打死時永遠等不到, 而那正是最該有人接手的時候 (量過: 一個 subagent 留了四個只等 `result.json` 的迴圈在背景, 等的那一輪被 bug 打死, 八個 process 空轉了三十五個小時, 是使用者發現的). 所以一個等待要同時:

- **不要只看 `result.json`**: 也看 `~/.ai_coc/state.json` 的 `status` 有沒有回到 `idle`, 或那個 pid 還在不在. 死掉的 run 不寫 `result.json`, `state.json` 也會一直停在 `running` (或 `stopping`) 到下一個指令接手, 會變的只有那個 pid 不在了
- **sleep 拉長**: 一輪三到五分鐘, 幾分鐘一次就夠
- **給一個上限**: 到了就停下來回報「等不到」, 那是一個有用的結論. 等一個 `ai_coc` run 的時候, 上限是它的 pid 不在了, 不是一個固定的時數

## 借, 用完還

**開發或驗證要動到活的遊戲, 直接下你的指令, 不用先停也不用問**: 使用者授權過, 測試時自己把打資源停下來, 最後記得開回去. 只有一個模擬器, 沒有「小心一點同時跑」這個選項.

你的指令**不帶** `--yield`: 它會借走背景那輪, 等它打完手上那一場才開始 (跟 `ai_coc stop` 一樣), 那輪的 agent 讀到借用會自己排隊開回去. 迴圈剛好在兩個指令之間的話, 你的指令直接開跑, 它的下一個指令會等你跑完, 你再下一個才借走它. `--yield` 只給打資源這種背景工作; 測試、驗證、花資源這些前景的事不帶.

**託管在顧升級, 就避開它到點的那幾分鐘.** `state.json` 的 `watch` 是 `running` (`takeover` 睡著在等升級) 的話, 開測試之前先看 `watch.next_at`: 離現在不到十分鐘就先不開, 等 `watch` 重新掛上一個離現在超過十分鐘的 `next_at` 再照上面借; 測試光等打資源那場打完就要五分鐘上下, 測得久的 (好幾個指令、一場測試戰鬥) 要離得更遠. 託管醒在到點前五分鐘, `status` 借到的模擬器不還, 開好建造面板或升級視窗、或選好那一棟放著, 一直到按下確認 (`takeover` 的「到點前五分鐘備好」); 這中間 `next_at` 會先改成到點那一刻, `watch` 也會有幾分鐘是 `idle`, 都還沒做完. 這時下一個不帶 `--yield` 的指令, 託管有指令在跑就請它收工, 它開好的畫面被你的指令換掉, 那個工人就一直閒著; 你測完的 `giveback` 還會連它的借用一起還掉, 打資源跟著插進來; 它接下來的指令又會反過來請你的測試收工. `watch` 停在 `idle` 就看它的 `stop_by`: 有人, 是託管被叫停了, 不用再等它; 空的是它醒來在做這一步, 繼續等, `ended` 過了二十分鐘還沒重掛才當它收了 (醒來之後還要按確認、`giveback`、重讀 `status`, 碰上打資源正在打仗, `status` 要先等那一場打完; 量過 138 次到點後重掛, 最久 19 分鐘). `next_at` 過了還停在 `running` 的, 是 watch 程序被砍掉留下的殘留 (活著的 watch 到 `next_at` 幾秒內就收), 不算託管在顧.

**每個指令都帶同一個 `--session`**: 借用是照 session 認借的人. 借到的那個指令沒帶的話, 它一跑完就把借用還回去, 打資源的迴圈會在你兩步之間插進來; 之後的指令沒帶, 就不算在續借用.

**借的指令通常要等一場戰鬥 (五分鐘上下) 才開始**, 對方等滿 `TAKEOVER_WAIT` (約 12 分鐘) 還不收工才會被結束: 前景跑的話那次呼叫的 timeout 給 runtime 允許的最長, 不然就背景跑. 被砍了就 `giveback`, 不然借用會留 30 分鐘, 迴圈白等.

**不 claim 的腳本自己借不到**: 任何自己寫、走 `commands._controller()` 的腳本 (點畫面用會 claim 的 `ai_coc tap` / `swipe` 就沒有這個問題), 都不碰 `state.json`, 迴圈在跑的時候照樣點得下去, 兩邊搶同一個畫面. 所以先用一個會 claim 的指令 (例如 `ai_coc stock`) 借到, 看 `state.json` 的 `loan` 在、`loan.ended` 是 null、`loan.borrower` 是你 (`emulator.ended` 是你那個指令收工的時間, 不用看), 才跑腳本. 沒有 `loan` 是迴圈剛好在兩個指令之間, 什麼都沒借到, 再借一次. 那個指令要帶 `--session`: 沒帶的話它一跑完就把借用還回去. 借用從你最後一個會 claim 的指令跑完起算, 過了 `LOAN_TIME` (30 分鐘) 就失效, 腳本用得久就中間再跑一次續上. 用完照樣 `giveback`.

**你自己決定測試要中途停**, 下 `ai_coc stop emulator`: 只停你的指令, 盯升級不動, 借用還在, 用完一樣要還 (「停」的兩層). 是使用者叫停就不還, 見下面「用完一定要還」的例外.

**背景那輪是你自己開的** (你就是那輪的 agent, 例如親自監督的 `watch-and-fix`), 借走的就是你自己的迴圈: 它收工時先不要開回去, 不然開回去的指令會排在你自己的借用後面. 測完、還了, 再照 `runner.md` 的「被叫停, 被借走」開回去.

**要測的是一場戰鬥, 記得村莊多半是滿的**: `attack` 開場讀到每一項都滿過 `stop_at` 就收工, 連對手都不搜. 所以測試那一場給 `--stop-at 0`, 它只管這一次. 那是測試不是打資源, 搶回來的進不了滿倉.

**用完一定要還**: 最後一個要用模擬器的指令跑完, 下 `ai_coc giveback`, 背景那輪就接著打. 這是那條授權的另一半: 忘了還, 它要等你最後一個指令跑完 30 分鐘才自己接回去, 這段時間模擬器閒著, 使用者卻以為在打. **每次測完就還, 不要攢著.** 例外有兩個, 都是不還, 改下 `ai_coc stop` 取消借用 (你的指令還在跑的話, 第一次只停它, 等它結束再下一次才取消借用, 「停」的兩層): 你的指令遇到別的裝置登入 (`另一部裝置登入了這個帳號…`), 還了或借用逾時, 打資源那一邊排著的指令就開跑, 它一開遊戲就把使用者踢下線; 還有使用者叫停, 還了的話打資源那一邊照樣開回去. **`giveback` 自己一步, 不要串在借用的指令後面** (像同一次呼叫裡的 `status; giveback`): 中途使用者叫停, 串在後面的那個照樣會跑, 借用被還回去之後, 下的 `ai_coc stop` 只回 `現在沒有指令在跑`, 打資源的 subagent 排著的指令就要開跑.

還了之後在回覆裡講一句「已經還了, 背景那輪會接著打」, 使用者看不到背景指令的輸出.

## 停

使用者叫停時誰去下, 見 `AGENTS.md` 的 A stop the user orders is run by whoever hears it. 這一節是怎麼停.

**跑 `ai_coc stop`, 這是停止的唯一方式.** 它把 `~/.ai_coc/state.json` 的 `emulator` 跟 `watch` 兩塊的 `status` 改成 `stopping` 就秒回, 不碰遊戲也不找程序. 每個會碰模擬器的指令都讀同一份檔案, 所以不必先查在跑哪一個 (會中途讀叫停的只有 `attack` (回合之間、對手之間)、`launch` (等村莊的時候) 跟睡著的 `ai_coc watch`; 其他指令都跑完自己那一趟才停); 它會回你叫停的是什麼、pid 多少, 沒有人在跑就說沒有. 你帶的 `--agent`、`--session`、`--mission` 會記成 `stop_by`, 那一輪收工後的 `idle` 紀錄還留著, 被停的那一邊讀得到是誰、為什麼.

**`stop` 一次停兩塊**: 模擬器上在跑的指令, 跟睡著的 `ai_coc watch` (它幾秒內醒來, 叫醒盯升級的那個 agent, 那一邊讀到 `stop_by` 就收工; 背景指令跑完叫不醒的 runtime 上, 那一邊要等它下一次排程才醒, 見「背景跑」). 只停一塊就帶目標: `ai_coc stop emulator` 只停指令, skill 自己為了換世界、拉修正、中途停測試而停迴圈都用這個, 不然會把盯升級一起停掉; `ai_coc stop watch` 只停盯升級. **借用照舊分兩層**: 借的那一邊的指令還在跑, 第一次只停那個指令, 借用留著, 出借方之後照樣開回去; 沒有指令在跑時再下一次, 才取消借用, 出借方不會開回去, 排隊中的那個也不會開跑. 所以使用者叫停而有人借著模擬器, 等指令停了再下一次. 例外是借的指令還在等對方打完那一場、還沒開始的時候: 這時的 stop 直接取消借用, 那個指令卻不讀叫停, 之後照樣開跑, 所以看 `state.json`, 它開跑了就再下一次停它.

**不用再手動刪那份檔案.** 程序死掉留下的 `running` 不會卡住任何人: 下一個要用模擬器的指令查得出那個程序已經不在, 直接接手.

停在每個迴圈最安全的接縫: `attack` 在回合之間跟對手之間. 所以**停不是立刻的**, 最久要等當下那一場戰鬥打完 (量過一次 107 秒). 夜世界在配對中被停, 而按 取消 的那一刻剛好配到對手, 那一場也照樣打完才停. 打到一半棄權會把軍隊丟在場上、遊戲停在回不了家的畫面, 比多等兩分鐘貴得多.

**怎麼知道停好了.** 背景指令自己結束: exit code 0, `result.json` 有內容 (排隊中被這個 stop 取消的指令例外: exit code 不是 0, `result.json` 是取消的那一行). `state.json` 的 `status` 是 `stopping` 表示收到了、還在打完手上那一場; 變成 `idle` (連同 `ended`) 才是真的收工.

**不要去砍背景程序.** 砍掉的話迴圈的 `KeyboardInterrupt` 出口執行不到 (只有前景的 Ctrl-C 走得到), 軍隊留在場上, 遊戲停在回不了家的畫面, 沒有 `result.json`, 只剩 `run.log` 能數. **卡死的也不用你砍**: 別的**不帶 `--yield`** 的指令等滿 `TAKEOVER_WAIT` (約 12 分鐘) 還等不到它收工, 會自己結束它並在自己的 `run.log` 記下是誰的 run; 下一個指令照常把遊戲帶回村莊. 沒有 `pid_created` 的舊紀錄例外, 不會被結束, 那個指令只會報錯. 帶 `--yield` 的指令不會結束任何人, 排在卡死的 run 後面會一直等.

**使用者說停, 停的是整件事而不是一個指令.** `state.json` 只停得下當下在跑的那個迴圈, 不知道你打算接著做什麼. 所以收到停止之後**不要再開下一個指令**, 只讀倉庫的 `stock`、`status` 也算, 除非使用者又說要繼續: 叫停就是他要接手, 回報的數字從 `run.log` 讀. 這一條沒有機制在管, 只有你. 程式上的「黏住的停止」(停了就拒絕所有 claim, 直到一個 `ai_coc resume`) 提過, 使用者否掉了, 要的是把 skill 修好; 照 skill 走的迴圈又衝過一次停止, 才重新提.

**每一步各自一次呼叫, 不要把幾步寫成一支只看 exit code 的腳本** (`world --go … && attack … && confirm …`): 在回合之間被停掉的 `attack` 照樣 exit 0, `world --go` 根本不讀叫停, 串在後面的那一步照樣開跑. 一個 subagent 這樣跑, 兩次 `ai_coc stop` 之後又刷了兩次牆、開了一場進攻.

**`ai_coc stop` 剛好下在兩步之間就不會叫停任何指令**: 沒有指令在跑, 它只回 `現在沒有指令在跑` (有人在盯升級的話, 盯升級照樣停), subagent 的下一步照常開跑. 下 stop 的那一邊看到這句, 要馬上傳話給打資源的 subagent; 它一步一次呼叫的話, 下一步之前就收得到. **排在別人指令後面的 `--yield` 指令也停不到**: 它停在 `holds the emulator; waiting for it to finish`, 前面那個不是借用, stop 收掉前面那個之後它就開跑 (只有被取消的借用會讓排隊的不開跑). 所以佔著的不是借用時, 一樣要傳話給排在後面那個迴圈的 agent.

## 遊戲卡住, 或者根本沒開

會操作畫面的指令都經過 `_controller()` 的 `ensure_coc`, 正常情況**不用自己開模擬器或遊戲** (只看的 `capture`、不帶 `--go` 的 `world`, 跟點面板的 `tap`、`swipe`、`confirm`、`boost` 例外, 模擬器沒開就報錯). 但它只處理得了「沒開」, 處理不了「開著但不理人」: 模擬器在跑、adb 也回話, 只有看畫面的人分得出來.

那種時候 `ai_coc launch --restart game` 重開遊戲而不動模擬器, `--restart emulator` 連模擬器一起重開.

**`launch` 三種 scope 都會等村莊畫出來再把鏡頭放好**: 輪詢到村莊畫出來 (最久三分鐘, 等不到就放棄), pinch 回最遠, 再用 `park_camera` 滑到地圖角落, 從那裡把村莊拖到畫面中間停好 (花錢買的場景拖好後會以畫面中間為準再拉近一次, 回到免費場景的大小). `LaunchReport.at_village` 說村莊有沒有出現, `outcome` 說沒出現是等到放棄 (`no_village`) 還是等的時候收到 `ai_coc stop` (`stopped`). 其他指令等得短得多, 會等村莊的最多半分鐘上下, `capture` 跟不帶 `--go` 的 `world` 不等, `world --go` 不等村莊. 所以遊戲剛開而村莊還沒出來, 修法是 `launch` 而不是重跑原本的指令.

log 裡的 `stopped moving after 2 swipe(s)` 是正常的. `camera was still moving` 的 warning 是鏡頭沒停好: 坐船跟收聖水車會因此收工, `launch` 的 `_settle_game` 不會 (後面點的是固定的畫面角落), 所以看到這行而指令仍回報成功不是矛盾. 攻擊迴圈開頭只拉遠不停鏡頭, 它的 run.log 裡這行只會來自夜世界的收聖水車.

遊戲卡在載入畫面是另一回事: `launch` 一樣只等三分鐘, 放棄的那一行是 `Gave up after 180s waiting for the game: the game is on its loading screen`. 那是伺服器的事, 攻擊迴圈自己會等 (最多 45 分鐘), 不要一直重跑 `launch`.

**模擬器本身起不來**: 指令報 `模擬器尚未開放 ADB 連接埠：127.0.0.1:0`, 雷電開起來變成 1920x1080, 或者 5555 連不上. 那不是程式的問題, 照 `repair-emulator` 處理.

**`launch` 三種 scope 都會佔用模擬器**, 跟其他會操作畫面的指令一樣先請背景那輪收工 (帶 `--yield` 的是借走, 用完要 `giveback`), 等它打完手上那一場才開始, 不會打斷戰鬥.

## merge 進來的修正, 迴圈怎麼用上

**迴圈不會自己用上 merge 進來的修正**: 它跑的是主 checkout 開跑時的程式碼. merge 之後傳話叫打資源的 subagent 拉, 它在兩個指令之間 `git -C <主 checkout> pull --ff-only`, 下一個指令起就是新的 (`runner.md` 的「被叫停, 被借走」). 拉進來的 `pyproject.toml` 或 `uv.lock` 有變才要 `uv sync`, 而 sync 要等沒有任何 `ai_coc` 程序在跑, 睡著的 `ai_coc watch` 也算 (先 `ai_coc stop watch`, sync 完再重掛), 不然撞 os error 32 (`AGENTS.md` 的 The loop runs the main checkout).

**修正要馬上驗證**: 在接縫停掉 (`ai_coc stop emulator`, 等它真的退出), 主 checkout `git pull --ff-only` (帶進的 `pyproject.toml` 或 `uv.lock` 有變就趁停著 `uv sync`), 同一個世界同一組旗標開回去, 剩下的場次就是驗證. 迴圈是 farm subagent 在跑的話, 它的回報進來才算退出, 開回去是傳訊息給同一個 subagent: `ai_coc stop` 是真的停, 它不會自己開回去.
