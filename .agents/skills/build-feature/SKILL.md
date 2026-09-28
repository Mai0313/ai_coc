---
name: build-feature
description: >-
  探索 Clash of Clans 這個遊戲, 找出還沒被自動化的東西, 直接做進 ai_coc 裡面, 中間
  用打資源的迴圈把等待的時間填滿. 使用者說「看看這遊戲還有什麼可以做」「探索一下遊戲直接
  幫我做出來」「幫我加一個 X 功能」「順便測試升級牆壁, 不浪費時間」的時候用這個
  skill. 探索一定要靠截圖, 因為沒被自動化的東西按定義就是還沒有人寫過判讀器的畫面.
---

# build-feature

兩件事同時進行: **探索遊戲並把找到的東西做出來**, 以及**讓打資源的迴圈在背景一直跑** (「打資源要一直在背景跑」那節). 打資源的決策照 `.agents/skills/farm/SKILL.md`: 倉庫滿了預設收工通知, 使用者開口才花, 牆連他開口要花都還要再指名. 背景怎麼跑, 停, 什麼不能同時跑, 收工前查什麼, 全在 `.agents/skills/farm/references/running.md`.

## 做完的樣子

使用者最後拿到的是**一個已經 merge 的功能, 加一則兩三行的回覆** (寫法在「任務報告」). 交之前這幾件都要成立:

- 新功能從 `commands.py` 叫得到, 有 `cli.py` 的 sub-command, 對著活的遊戲跑過順利跟失敗兩條路 (「做的流程」)
- PR 照 `AGENTS.md` 的 `## Development flow` 走到 merge
- 你拿來下判斷的截圖都已經另外發給使用者
- 背景打資源的那輪已經還回去, 回覆講它跑到哪; 刻意留著在跑的講是哪一個

探索找到候選、還沒動手的時候, 交的是候選跟畫面, 每一個講代價跟風險, 用選擇題讓他挑 (「什麼算好題目」). 其他還在半路的情況見「任務報告」開頭.

## 三份文件的分工

- **程式碼**說現在做得到什麼. 完整清單是 `src/ai_coc/cli.py` 的 `_parser()` 跟 `src/ai_coc/commands.py`, 開工前先讀, 不在上面的就是題目
- **`AGENTS.md`** 說架構跟規則, **常數旁邊的註解**說每個數字怎麼量的
- **這份 skill** 說怎麼探索, 什麼算好題目, 做出來放哪

## 探索一定要看圖

還沒人寫判讀器的畫面, 讀程式碼跟 log 都找不到, 只能進遊戲看. 畫面要先從背景那輪拿過來 (「打資源要一直在背景跑」).

```bash
uv run ai_coc capture --count 20 --gap 1.5 --label explore   # 從活著的遊戲連拍
uv run ai_coc read <png>                                          # 現有 parser 對這張圖的說法
```

這份 skill 裡的指令都省略了 `--agent`、`--session`、`--mission`, 真的跑的時候每一個都要帶 (`AGENTS.md` 的 Say who is driving). 從你的 worktree 跑; 人在主 checkout 下的話一律寫成 `uv run --no-sync`, `stop` 也一樣, 理由在 `AGENTS.md` 的 The loop runs the main checkout. 分支動到 `AppConfig` 的話, 從 worktree 跑會讀設定檔的 `ai_coc` 指令 (碰模擬器的都會) 都會把共用的 `~/.ai_coc/config.json` 改寫成新的形狀 (`adapters/config.py`), 而主 checkout 那輪讀的是同一個檔案, 所以那種分支要等迴圈停著才跑.

- **先確認在哪個村莊**: 遊戲開在上次離開的那一個, 所以第一步是 `uv run ai_coc world`, 不然會拿夜世界的畫面量日世界的常數
- **不要下裸的 `adb shell screencap` 或 `adb shell input`**: 在 MuMu 上, 沒指定 display 的截圖會解碼失敗, 點擊會安靜地落在 launcher 上. 用 `AdbController`, 腳本跟切村莊的寫法在 `references/exploring.md`
- `ai_coc read` 每個欄位都是 `None`、空的或 `false` 的畫面, 就是沒開發過的地方
- **`uv run ai_coc export --last --table`** 列出這個帳號兩個世界的每棟建築、兵種跟英雄, 不碰遊戲: 用來認「這棟是什麼」, 以及確認功能在這個帳號上有東西可操作 —— 還沒蓋的建築不值得先寫判讀器
- **看到值得做的畫面就把圖發給使用者**, 不要只留路徑: 他看不到你的工具輸出, 要不要做是他決定

## 什麼算好題目

**好的**: 每天重複; 成敗讀得出來 (不然沒辦法驗證, 出錯也停不下來); 花會再生的資源 (金幣, 聖水, 黑水) 而不是寶石或現金; 失敗代價小, 可以重來.

**不好的**:

- 判讀器要**每天跑幾百次**都認得遊戲美術, 而美術每一級、每次改版都會變. 判讀器只讀遊戲畫在上面的 UI, `AGENTS.md` 搜 `read the UI the game paints on top`; 一次性的辨認用看的就好, 那是 `spend-loot` 的事
- 不可逆: 拆建築, 換陣型, 動帳號設定
- 會花寶石或現金. 加速所有同類項目跟城牆戒指的圖示會被讀成資源, 現有程式碼在判讀器裡就把它們擋掉 (藍色底板), `AGENTS.md` 搜 `blue plate`
- 跟別的玩家互動而對方會受影響
- **限時活動本身不是做的理由**: 它的畫面幾週內就消失. 看起來值得做就講出來, 讓使用者決定, 他要才做 (`AGENTS.md` 搜 `limited-time event`)

探索找到的候選, 先跟使用者確認再做.

## 做出來要放哪

完整規則在 `AGENTS.md` 的 Architecture 跟 Project rules, 一定會撞到的是:

- **headless 先行**: 新功能要能被 `commands.py` 叫到, 才能在開發中對著活的遊戲跑; 視窗最後才接, 有時不接. 要接進主控 tab 的話, job 的名字要同時是 `UiJobs` 的欄位、一個 checkbox 跟一個 sub-command, 少一處它就安靜地不跑 (`AGENTS.md` 搜 `three places at once`)
- **三層目錄**: `ui/` 編排, `adapters/` 外面的世界, `parsers/` 純函式. 判讀畫面進 `parsers/`, 才能拿真實截圖當 fixture 測
- **每個結構化的值都是 Pydantic model**, 放 `models.py`. 不要 dataclass, TypedDict, 或在函式之間傳裸 dict
- **報告是欄位加一個具名的 outcome, 不是句子**: 欄位分不出走的是哪條路, 就加一個必填的 `Literal` outcome; 中文在 `commands.py` 裡那個指令旁邊的 `dict[XOutcome, str]` 組, 那張表要有測試 (`AGENTS.md` 搜 `A report carries fields`)
- **只做遊戲當下所在的村莊, 不自己坐船**: 在沒有它的版本的村莊上什麼都不做, 回具名的 outcome (`builder_base`, `other_village`). 使用者 2026-09-25 交代新指令也照這樣設計 (`AGENTS.md` 搜 `none of them sails`)
- **座標寫死 1600x900**: 新的 parser 一律用 `parsers/frame.py` 的 `open_frame` 解碼, 別的尺寸它會 raise
- **每次截圖跟每次輸入都要指定 display**
- **會花資源的能力要有程式層的 guard**: 做得到就靠結構擋, 像城牆迴圈只點從聖水圖示往左數出來的位置, 城牆戒指跟寶石按鈕根本點不到; 做不到就在判讀器裡擋, 讓呼叫端拿不到那顆按鈕的價格, 像英雄殿堂的 `GEM_GREEN`. 新的花錢能力也要有自己的一道, prompt 或 skill 裡的一句話不算, `AGENTS.md` 搜 `Spending is guarded`

## 做的流程

1. **先在活著的遊戲上量**: 哪些像素穩定, 哪些隨等級或主題變. 數字寫成常數, 旁邊註明怎麼量的 (怎麼量在 `references/exploring.md`)
2. **寫 parser 加測試**, 用真實截圖當 fixture, 放 `tests/frames/`; 錯的畫面也要有一張, 判讀器在上面要答不是. 放進去之前先照 `AGENTS.md` 把名字、玩家標籤、部落名跟聊天內容塗掉
3. **接上 runner**, 放在 `ui/`, 保持 Qt-free
4. **接上 `commands.py` 跟 `cli.py` 的 sub-command**
5. **對著活的遊戲驗證**, 失敗的路徑也要驗. 在你的 worktree 裡跑, 畫面先從背景那輪拿過來
6. `uv run pytest` 跟 `make fmt`
7. **PR 之前, 把新能力寫進會用到它的 skill**, 在回覆裡講改了什麼 (`AGENTS.md` 搜 `outdates a project skill`)
8. **流程照 `AGENTS.md` 的 `## Development flow`**, 不用另外問使用者

**量測寫進常數旁邊的註解, 不寫進 `AGENTS.md`**, 它每個 session 都會被讀, 只收規則, 不變式跟未修的已知缺陷. 要加規則就直接寫, 不用先給使用者看.

## 打資源要一直在背景跑

改碼跟等測試的時間拿去打資源, 這是使用者明講的要求. 誰跑, 在哪跑, 照 `AGENTS.md` 的 Driving the game: 一個 `farm` subagent 在主 checkout 上跑迴圈, 你的改動在另一個 worktree 做, 那裡自己 `uv sync` (理由在 The loop runs the main checkout).

- **絕對不能同時跑兩個指令**, 它們搶同一個模擬器畫面, `references/exploring.md` 的腳本也算一個. 要探索或驗證就直接下你的指令 (不帶 `--yield`), 不用問: 它會借走背景那輪, 等它打完手上那一場才開始, farm subagent 讀到借用會自己排隊開回去. **用完一定要 `uv run ai_coc giveback`**, 背景那輪才會接著打, 並在回覆裡講一聲, 使用者看不到它的輸出. `references/exploring.md` 的腳本不經過 claim, 自己借不到, 要先用會 claim 的指令借到再跑. 細節在 `AGENTS.md` 的 Taking the game for a live test 跟 `.agents/skills/farm/references/running.md` 的「你自己要用畫面」
- **不要 kill 背景程序**: 背景送不進 Ctrl-C, 被 kill 會把軍隊留在戰場上, 遊戲停在下一輪回不了家的畫面
- `walls` 是會花錢的迴圈裡最少被跑過的, 跑它就是在測它, 但**要使用者開口** (`farm` 的「倉庫滿了」那節). 他交辦裡點名要測牆 (像「順便測試升級牆壁」) 就算開口, 交辦 `farm` subagent 時把這句轉給它, 它看不到使用者的原話; 沒點名的話, 想拿它測就把牆列進倉庫滿了那則通知的選項, 不要自己跑
- **城牆等級被大本營卡住時, `walls` 一批都買不成**: 約兩分鐘後回報 `WallReport.outcome` 是 `nothing_bought`, 不是 bug, 是牆在這個大本營等級到頂了 (`AGENTS.md` 的 Wall loop). 要等大本營升上去, 那是使用者的決定. 反過來 `nothing_bought` 不一定是大本營: 工人數讀不到也會落到這裡, 兩種的下一步相反, 看 `run.log` 每個位置停在哪一步 (`commands.py` 的 `WALL_LINES` 上面的註解)

## 任務報告

功能做完並且 merge 之後才交. 還在半路就講一句話說到哪, 不要包裝成完成品. 使用者 2026-09-27 的交代: 回報要短, 從他的角度講.

```
做好了: 新增 `ai_coc xxx`, 會 <一句話, 講它替使用者做什麼>.
成功跟失敗的情況都對著遊戲跑過 [圖], PR <連結> 已 merge.
背景那輪: <`farm` 的樣板; 還沒打滿就一句跑到哪>
```

探索途中看到但這次沒做的題目, 用選擇題問他下次要不要做, 各附代價跟風險, 不寫進回覆.

看過的圖用檔案傳送機制發給使用者, 不要只留路徑. 迴圈開 `--record` 才留畫面, 跟你 `capture` 的一樣進那次執行的 `~/.ai_coc/logs/<when>-<what>/frames/`, 路徑不能指定, 用 `--label` 取名才找得回來. **畫面只留七天, log 永久留**, 要長期留的證據自己複製出來 (當 fixture 的進 `tests/frames/`). 證據用完就照 `AGENTS.md` 的 Frames are the only thing worth culling 把這次的 `frames/` 刪掉, log 留著; 之後還要用的那幾張先複製出來.
