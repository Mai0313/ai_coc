---
name: repair-emulator
description: >-
  模擬器本身壞了就來修: 雷電 (LDPlayer) 或 MuMu 閃退、開不起來、一直開開關關、ADB
  連不上, `ai_coc` 報「模擬器尚未開放 ADB 連接埠：127.0.0.1:0」, 解析度變成
  1920x1080, 或者遊戲程序在卻沒出現在畫面上. 使用者說「模擬器閃退了」「雷電開不起來」
  「ADB 連不上」「launch 失敗」「模擬器怪怪的」的時候用這個 skill, farm、watch-and-fix、
  watch-upgrades 跑到一半因為模擬器掛掉而停下來的時候也用. 靠截圖跟模擬器自己的
  CLI 分出是哪一種壞法, 弄回 `ai_coc launch` 站得上村莊的狀態, **不改程式碼**; 修不好,
  或者要動模擬器設定畫面的, 停下來請人.
---

# 修模擬器

目標只有一個: `uv run --no-sync ai_coc launch` 回 `at_village: true`, 而且你自己看過那張畫面. 這個 skill 不改程式碼; 模擬器好好的而 `ai_coc` 還是失敗, 那是程式的問題, 交給 watch-and-fix.

下面的壞法全部是 2026-09-26 在雷電 14 上實際看到的. MuMu 還沒走過一遍: 方法一樣 (先看, 再動手, 修不好請人), 但雷電的指令跟檔案不適用, 它的 CLI 看 `src/ai_coc/adapters/mumu.py`.

## 先看畫面, 不要從 log 猜

這是使用者明講的規矩. 那天一個 `launch` 失敗從 logcat 推了很久推不出來, 改成每秒截一張, 馬上看到遊戲啟動時會先跳回桌面一兩秒.

- **Android 裡的畫面**用這個 skill 的 `scripts/look.py`. `ai_coc capture` 跟 spend-loot 的 `eye.py` 都會先 `ensure_coc` 把模擬器跟遊戲開起來, 修的時候正好不能那樣

    ```bash
    uv run --no-sync python .agents/skills/repair-emulator/scripts/look.py <資料夾> 15 1
    ```

    開機跟遊戲啟動是一段過程, 一張看不出來, 要連拍. ADB 連不上它會直接報錯, 那句話本身就是答案

- **模擬器視窗本身** (雷電的錯誤框、它的修復工具) 在 Android 外面, 要截桌面, 那需要使用者授權. 沒授權就請他截圖或描述, 不要猜

- 看過的每一張都發給使用者

## 動手之前

- **你是 subagent 就不要修.** 這份 skill 要跟使用者講話、請他改設定, subagent 做不到. 停手, 把 `state.json`、`list2` 那一行跟截圖交回給主 session, 由它照這份處理
- 照 `CLAUDE.md` 的 Look before driving 讀 `~/.ai_coc/state.json`. farm subagent 還在跑就照 `CLAUDE.md` 先把模擬器拿回來, 模擬器只有一台; 它的迴圈多半已經跟著模擬器掛掉了, 那就等它的回報
- **結束程序、`quit`、重開模擬器之前先問使用者, 他說好才做.** 那是他的模擬器, 他看得到那個視窗, 一直開開關關又沒人說明, 他只會以為壞得更嚴重. 跑 `ai_coc launch` 之前講一聲: 遊戲開不起來的時候 `ensure_coc` 會自己重開一次實例
- 使用者可能正在模擬器或手機上玩 (`CLAUDE.md` 的 Driving the game), 有疑慮就問

## 讀狀態 (雷電)

雷電的安裝目錄從 Windows 的解除安裝登錄找 (`adapters/ldplayer.py` 的 `detect_install_path`). **路徑不會是問題**: 設定指向雷電的時候, 每個指令 log 的前幾行都印著 `LDPlayer install root`, 有這一行就是找到了. 下面的 `ldconsole` 就是那個目錄裡的 `ldconsole.exe`.

下面寫的 `--index 0`、`leidian0.config`、`index=0|` 都是 instance 0, 那是那天那台唯一的 instance. `config.json` 的 `adb_serial` 對到哪個 index 看 `adapters/ldplayer.py` 的 `serial_for` (5555 是 0, 每多一號加 2).

- `ldconsole list2`: 一行一個 instance, 欄位順序在 `models.py` 的 `LDPlayerInstanceInfo`. 要看的是第 5 欄 Android (0 關著, 2 開機中, 1 起來了), 第 6 欄雷電視窗的 pid (-1 是沒在跑), 最後三欄寬、高、dpi, 應該是 `1600,900,240`
- `ldconsole isrunning --index 0`: `running` 或 `stop`
- 程序: `dnplayer.exe` 是雷電的視窗, 命令列 `index=0|`, 多了 `from=repairer` 的是雷電的修復工具重開的; `Ld9BoxHeadless.exe` 是 VM 本體. 命令列用 `Get-CimInstance Win32_Process` 看
- ADB: `look.py` 截得到圖就是通的
- 設定檔是安裝目錄的 `vms\config\leidian0.config`, **只讀不改** (理由見第 3 種). 看 `advancedSettings.resolution` 跟 `resolutionDpi`, `basicSettings.adbDebug` (沒有這一行就是 ADB 關著), `statusSettings.isNewPlayer`

## 四種壞法

### 1. 還在開機: 閃退之後雷電在修復

症狀: `ai_coc` 印了一串 `LDPlayer reported 1 instance(s)`, 半分多鐘後報 `模擬器尚未開放 ADB 連接埠：127.0.0.1:0`. 程式等開機只等 `adapters/emulator.py` 的 `BOOT_POLLS` 乘 `POLL_GAP`, 等完就拿還沒開好的 ADB 去開遊戲.

量到的: 正常冷開機 24 秒 ADB 就通. 雷電被強制結束之後再開, 它的修復工具會把視窗關掉再開一次 (`list2` 的 Android 欄 2 → 0 → 2 → 1, 新的 `dnplayer.exe` 帶 `from=repairer`), 82 秒才通.

處理: 不要動它. 等 Android 欄變 1 而且 `look.py` 截得到圖, 再跑 `ai_coc launch`. 修復到一半關掉或再開一次, 只會從頭再來.

等到哪裡為止: 三分鐘還沒好, 或者 Android 欄停在 2 而 VM 沒起來 (第 7 欄 -1, 也沒有 `Ld9BoxHeadless.exe`), 就不是修復了. 後面那種是 `adapters/ldplayer.py` 的 `QUIT_SETTLE` 註解寫的卡死, 做一次「不要做的事」裡的乾淨重開.

### 2. `ldconsole` 看不到還在跑的雷電

症狀: `list2` 說沒在跑 (Android 0, pid -1), `isrunning` 說 `stop`, 可是 `dnplayer.exe index=0|` 還在, 有時沒有視窗, `Ld9BoxHeadless.exe` 也常常在. `quit` 跟 `quitall` 碰不到它.

**這不是殘留, 雷電多半是活的.** 2026-09-26 遇到好幾次, ADB 每次都通: 一台剛打完一小時的資源、使用者沒有關它; 一台使用者從圖示剛打開、有視窗, 開起來之後好幾分鐘 `list2` 都是 0. VM 自己的紀錄 (`vms\leidian0\Logs\VBox.log`) 一直正常寫, `look.py` 截得到 1600x900 的 Android 桌面. `list2` 為什麼會看不到, 還沒查出來. 有個 agent 把它當殘留直接結束, 等於親手關掉一台好好的模擬器.

先排除第 1 種: 修復到一半 `list2` 也會有十幾秒是 Android 0, 那時跑任何會開模擬器的指令都會讓修復重來. 至少看一分鐘都是這樣, 而且那個 `dnplayer.exe` 的命令列沒有 `from=repairer`, 才是這一種.

**是這一種就直接跑 `ai_coc launch`.** `ai_coc` 不管 `list2` 怎麼說都會問 ADB (`adapters/ldplayer.py` 的 `enumerate_instances`), 開機完成就照常開遊戲, log 裡會有一行 `LDPlayer lists instance 0 as down, but its ADB answers`. 站上村莊就沒事了.

`ai_coc launch` 還是不行, 而且 `look.py` 也截不到圖, 才往下處理.

處理: **不要結束任何程序, 請人.** 用 `look.py` 試一次, 連同 `list2` 那一行跟程序清單交給使用者, 也說清楚強制結束的代價: 那天兩次強制結束有一次把設定洗掉了 (第 3 種). 要不要重開、怎麼重開由他決定. 他同意由你來做的話: `Stop-Process` 那個 `dnplayer.exe` 跟 `Ld9BoxHeadless.exe`, 等兩個都不見了再多等十秒 (`QUIT_SETTLE` 的理由), `ldconsole launch --index 0`, 然後照第 1 種等滿三分鐘, 中間不要 `quit`, 也不要再開一次. 那天這樣做過兩次, 一次 82 秒後正常起來; 另一次重開之後沒多久就被 `quit` 過, 重開的那台 `list2` 一樣看不到. 結束程序對雷電來說就是一次閃退, 也可能變成第 3 種.

### 3. 設定被洗掉

症狀: `list2` 最後三欄變成 `1920,1080,280`, 或者 Android 欄已經是 1 很久了 ADB 還是拒絕連線, 設定檔裡沒有 `basicSettings.adbDebug`. `ai_coc` 也會報 `127.0.0.1:0`, 但是一開始就報, 不像第 1 種要等半分多鐘: Android 已經是 1, `ensure_coc` 就不等開機. 雷電的 `reboot` 會這樣 (程式碼已經不用它), 被強制結束之後也會.

**強制結束之後, 壞掉會晚一輪才看得出來.** 2026-09-26 兩次都是這個順序: 雷電被強制結束 (一次是 agent, 一次是使用者在工作管理員); 下一次開機, 雷電把 `vms\config\leidian0.config` 整份重寫成新機器 (`isNewPlayer` 變 `true`, 手機型號換掉, 解析度跟 `adbDebug` 那幾行不見), 可是這一次開起來還是 1600x900、ADB 也通, 看起來沒事; 再下一次開機才變成 1920x1080、ADB 關著. 所以看到設定檔被重寫, 就知道下一次開機會壞. 同一段時間 `log` 資料夾沒有新的 crash 紀錄, 看不出是雷電哪個部分重寫的.

**關雷電用右上角的 X 或 `ldconsole quit`, 不要用工作管理員.** 那天兩種都在 8 秒內把視窗程式跟 VM 乾淨關掉, 沒有一次洗掉設定. X 的行為看設定畫面的「關閉設定」, 設定檔的 `statusSettings.closeOption` 是 1 的時候就是「直接關閉」; 設定檔被重寫之後這一欄會不見, 按 X 變成先跳一個提示.

處理: **請使用者在雷電的設定畫面改回來, 你不要自己修**: ADB 偵錯開本地連接, 解析度 1600x900, DPI 240. 他在設定畫面改完, 設定檔的 `basicSettings.adbDebug` 那天看過 1 跟 2, 兩次 ADB 都通.

為什麼不自己修: 那天用 `ldconsole modify` 加手改設定檔的步驟修了三次, 三次都失敗, 還把雷電弄成每次開機都把自己當成新機器重寫設定檔 (`isNewPlayer` 變 `true`, 手機型號每開一次就換一個), 又把 ADB 關了一次. 其中一次是 Git Bash 的 `sed -i` 把設定檔的 CRLF 悄悄換成 LF. `ldconsole modify` 改得了解析度, 但沒有 ADB 的選項.

改完確認: `list2` 最後三欄 `1600,900,240`, `look.py` 截得到 1600x900 的圖, `isNewPlayer` 是 `false`. 那天他改完解析度之後實例重開過 (`list2` 的 pid 換了), 還是舊解析度的話請他重開一次模擬器. `isNewPlayer` 還是 `true` 那天沒遇過, 請人. 手機識別碼那天換了好幾次, 遊戲帳號一直登著, 這個不用擔心.

### 4. 遊戲程序在, 畫面卻是桌面

症狀: `ai_coc launch` 回 `at_village: false`, log 裡是 `Gave up after 180s waiting for the game: the game is not on a display yet`. ADB 不通也會寫同一句, 所以先用 `look.py` 確認: 截得到圖, 1600x900, 畫面是雷電的桌面, 才是這一種.

先認得正常的樣子: 那天連拍到的遊戲啟動, 頭十幾秒依序是 Play 遊戲的登入橫幅、SUPERCELL 標誌, **大約第 9 秒會跳回桌面一兩秒**, 然後遊戲自己回來. 所以一張桌面不代表失敗, 連拍十幾秒再判斷.

處理: log 前面有 `The session was dropped; restarting the game` 的話, 先問使用者是不是正在手機上玩: 重跑會把登入搶回來, 把他踢掉 (`CLAUDE.md` 的 Driving the game). 不是, 就再跑一次 `ai_coc launch`. 那天出事的那一次就有這一行, 而且剛好是設定剛被洗成新機器之後; 當時沒留畫面, 原因沒查到, 之後重跑都正常. 再遇到就連拍留證據.

## 不要做的事

- 不要 `ldconsole reboot`
- 不要為了「重現閃退」去強制結束模擬器. 那天就是這樣把設定洗掉的. 結束任何程序都要使用者先同意 (第 2 種), 而且要跟他說清楚兩輪之後設定會壞 (第 3 種)
- 不要手改 `leidian0.config`, 理由在第 3 種
- 不要在 `dnplayer.exe` 或 `Ld9BoxHeadless.exe` 還在的時候 `ldconsole launch`. 那天有三台雷電開出來就沒有視窗、使用者叫不出來: 兩次開的時候舊的確定還在或剛被結束十秒, 第三次舊的 VM 已經關了一分多鐘, 舊的 `dnplayer.exe` 在不在沒記到. 等兩個都不見了才開的那次有視窗
- 不要自己下 `adb connect` / `adb disconnect`, 看 ADB 用 `look.py`: 每次 connect 都在雷電那端開一條新連線, 雷電的 port forward 曾經因此整個掛掉 (`CLAUDE.md` 的 Adapters)
- 同一招不要連試. 一次乾淨的重開 (使用者同意之後): `ldconsole quit --index 0`, 等 `isrunning` 說 `stop` 而且 `dnplayer.exe` 跟 `Ld9BoxHeadless.exe` 都不見了 (那天都是幾秒內; 一分鐘還在就請人. `Ld9BoxSVC.exe` 有時會一直留著, 不用等它), 再多等十秒 (`adapters/ldplayer.py` 的 `QUIT_SETTLE` 寫了為什麼), `ldconsole launch --index 0`, 等 Android 欄變 1 而且 `look.py` 截得到圖, 再 `ai_coc launch`. 這樣還站不上村莊, 就是該請人的時候

## 什麼時候請人

- 第 3 種, 一定; 第 2 種在 `ai_coc launch` 也救不回來的時候
- 一次乾淨的重開之後還是站不上村莊
- 看不到的東西: 模擬器視窗上的對話框, 截桌面沒被授權

請人的時候講清楚四件事: 看到什麼 (附圖), 試過什麼, 要他改哪裡、改成什麼, 他改完你怎麼確認. 他說改好了, 照下一節驗.

## 修好的標準

`uv run --no-sync ai_coc launch` 回 `at_village: true`, 而且你看過 `look.py` 截的那一張: 1600x900, 站在村莊上, 鏡頭拉到最遠. 發給使用者. 模擬器是從 farm subagent 手上拿來的, 就照 `CLAUDE.md` 還回去.
