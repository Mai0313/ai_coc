# 讓一輪在背景跑, 並且盯得住它

`farm`, `tune-attack`, `build-feature` 三個 skill 共用這一份. 從別的 skill 過來的話, 路徑是 `.agents/skills/farm/references/running.md`.

## 為什麼一定要背景跑

一輪進攻是搜尋加上最多三分鐘的戰鬥加上回營, 四到五分鐘起跳; `--repeat 0` 打到倉庫滿可能是好幾個小時. 前景跑的話這段時間你什麼都做不了, 使用者也插不進話. 背景跑之後你可以在戰鬥進行的同時讀程式碼, 改東西, 或者回答使用者的問題.

## 東西放哪

`.gitignore` 裡已經有 `/.runs/`, 所以 repo 根目錄下的 `.runs/` 是現成的地方, 不會弄髒 git status. 一次跑一個目錄:

```
.runs/2026-08-27-1930-farm/
├── run.log        # stderr, 也就是那串 rich 印出來的執行紀錄
├── result.json    # stdout, 跑完才會有內容
├── frames/        # 只有需要查東西的時候才開
└── played.json    # --plan-out, 只有 tune-attack 用得到
```

目錄名帶上時間跟這次在做什麼, 因為一個 session 常常會跑好幾輪, 事後要分辨哪個是哪個.

## 開跑

```bash
uv run ai_coc attack --repeat 0 > .runs/<run>/result.json 2> .runs/<run>/run.log
```

用 Bash 工具的 `run_in_background` 送出去. 重點在那兩個重導向: **stdout 是答案, stderr 是過程**. `cli.py` 的 `_run_command` 把報告的 JSON 寫到 stdout, 而 `configure_logging()` 把所有 log 送到 stderr 的 rich console. 兩個混在一起就沒辦法 parse 了.

`result.json` 要等整個 series 跑完才會有東西, 所以跑的過程中唯一能看的是 `run.log`.

## 盯

`run.log` 是純文字, 用 Grep 抓有意義的行, 不要整份 Read 進來, 一小時的跑會有幾千行:

- `Round \d+ of` 這種行說現在第幾輪
- `Attack finished:` 每輪一行, 後面接 `AttackReport.message`
- `WARNING` 跟 `ERROR` 是真的要看的

**幾分鐘看一次就好.** 一輪四五分鐘, 每三十秒去 tail 一次只是在浪費 context, 而且中間本來就沒有新東西. 背景指令跑完的時候會通知你, 那才是必須處理的時刻.

`~/.ai_coc/logs/controller.log` 是同一批 log 的另一份, 純文字, 會轉檔. 你導出來的那份不見了或者要翻更早以前的跑, 才去看它.

## 中止

`attack` 的迴圈接 `KeyboardInterrupt`, 而且接在**回合之間**: 已經開打的那一場會打完, 因為打到一半棄權會把軍隊丟在場上, 遊戲停在下一輪回不了家的畫面. 所以中止不是立刻的, 要等當下那場結束.

要停就停整個背景指令 (TaskStop 或等價的機制). 停掉之後 `result.json` 會是空的, 那時候 `run.log` 就是唯一的紀錄.

## 絕對不要同時跑兩個

`attack`, `walls`, `collect`, `upgrade`, `donate` 全部都在驅動同一個模擬器的同一個 display. 兩個一起跑就是兩隻手在搶同一個畫面: 一邊點開了選單, 另一邊截到那張圖然後判讀成完全不同的東西, 而且沒有任何機制會發現. 開下一個之前先確定上一個真的結束了.

`--shot-every` 的心跳截圖是同一個程序裡的執行緒, 它跟迴圈搶同一條 ADB 連線 (一張 `screencap -p` 要 0.6 到 0.8 秒), 所以那個是有代價但可控的, 兩個獨立的指令不是.

## 空檔要拿來做事

戰鬥的三分鐘, 軍隊沒練好的那六十秒, 刷牆掃描村莊的那段時間, 都是你可以讀程式碼跟改東西的時間. 使用者開這三個 skill 的其中一個, 一部分的意思就是「不要讓時間空著」.

反過來也成立: 手上的事做完了而背景還在跑, 不要每三十秒去戳一次 log 假裝在做事. 跟使用者說一聲現在在等什麼, 然後真的等.
