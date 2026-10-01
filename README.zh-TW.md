<div align="center" markdown="1">

# AI CoC

[![PyPI version](https://img.shields.io/pypi/v/ai_coc.svg)](https://pypi.org/project/ai_coc/)
[![python](https://img.shields.io/badge/-Python_%7C_3.12%7C_3.13%7C_3.14-blue?logo=python&logoColor=white)](https://www.python.org/downloads/source/)
[![uv](https://img.shields.io/badge/-uv_dependency_management-2C5F2D?logo=python&logoColor=white)](https://docs.astral.sh/uv/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![ty](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ty/main/assets/badge/v0.json)](https://github.com/astral-sh/ty)
[![Pydantic v2](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/pydantic/pydantic/main/docs/badge/v2.json)](https://docs.pydantic.dev/latest/contributing/#badges)
[![tests](https://github.com/Mai0313/ai_coc/actions/workflows/test.yml/badge.svg)](https://github.com/Mai0313/ai_coc/actions/workflows/test.yml)
[![code-quality](https://github.com/Mai0313/ai_coc/actions/workflows/code-quality-check.yml/badge.svg)](https://github.com/Mai0313/ai_coc/actions/workflows/code-quality-check.yml)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/Mai0313/ai_coc)
[![license](https://img.shields.io/badge/License-MIT-green.svg?labelColor=gray)](https://github.com/Mai0313/ai_coc/tree/main?tab=License-1-ov-file)
[![PRs](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/Mai0313/ai_coc/pulls)
[![contributors](https://img.shields.io/github/contributors/Mai0313/ai_coc.svg)](https://github.com/Mai0313/ai_coc/graphs/contributors)

</div>

一個 Windows 桌面應用程式, 用來玩跑在 MuMu 模擬器 12 或雷電模擬器 14 裡的《部落衝突》。畫面判讀是它自己做的, 讀到什麼就轉成 ADB 點擊, 並在下一張截圖上確認結果。Gemini 每一場只被問一個問題: 這個村莊該怎麼打。

其他語言版本：[English](README.md) | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

## ✨ 功能

**自己打資源。** 搜尋對手, 跳過戰利品不到門檻的, 十秒左右把整支軍隊放下去, 打完回營再來下一場, 倉庫滿了自己停。畫面判讀是在本機做的而不是送出去問: 戰利品面板、兵力條、卡片列全部靠模板比對, 所以跳過一個對手不花錢, 一場戰鬥也不會卡在網路上。

**每一場只問 Gemini 一個問題**, 而且是在對手已經通過門檻之後才問: 這個村莊該怎麼打。它回答投兵線的兩端、每瓶狂暴跟冰凍的落點, 還有每張英雄卡是誰。沒有 API key 的話會改用固定戰術, 其他功能照常。

**把打到的花掉。** 城牆付錢的當下就升級完成, 不佔工人也不跑計時器, 所以倉庫滿了就是它的出口: 迴圈自己在地圖上找牆, 算出村莊買得起的最便宜批次再買。閒著的工人可以派去做買得起的最貴升級, 英雄也能一個一個往上升。

**顧著村莊不停擺。** 收採集器、讀每個工人在蓋什麼還剩多久、有人在部落聊天室要兵就捐給他。

**自己爬得回來。** 掛太久被踢掉的連線會自動重開遊戲繼續跑, 不管當下在跑哪個迴圈。伺服器沒回應的時候會等它回來, 而不是一直對著載入畫面亂點。

從遊戲取出的村莊 JSON 會保留看不懂的欄位跟 `data_id` 而不是直接失敗, 段落也是讀出來的而不是寫死一份清單。

## 📋 環境需求

- Windows。程式要呼叫 `mumu-cli.exe` 或 `ldconsole.exe`、用 `winreg` 讀登錄檔、用 `ctypes.windll` 讀寫剪貼簿, 這些在別的系統上都不存在
- [MuMu 模擬器 12](https://www.mumuplayer.com/) 或 [雷電模擬器 14](https://www.ldplayer.tw/), 裝好部落衝突, 解析度 1600x900。雷電要在設定裡打開 ADB 偵錯 (本地連接)
- 一把 Gemini API key, 在設定分頁填。它只回答三件事: 每場的戰術、城牆跟建築在地圖上的位置、以及某個選單是哪一棟建築。沒有它的話進攻迴圈會退回固定戰術, 刷牆跟升級改用掃描找目標, 而收採集器、工人、英雄、捐兵那幾個指令本來就不會問它任何事

## 🚀 安裝與執行

從 PyPI 執行, 不留任何東西在系統上:

```bash
uvx ai_coc
```

或者正常安裝:

```bash
uv tool install ai_coc
ai_coc
```

每個 [release](https://github.com/Mai0313/ai_coc/releases) 都附了編譯好的 Windows 執行檔。

## 🎮 從終端機玩

視窗是一種用法, 另一種是下 sub-command, 跑的是同一套迴圈但完全不開視窗。平常都是這樣玩的, 因為終端機空著就能盯 log。

### 打資源

```bash
ai_coc attack                    # 打一場
ai_coc attack --repeat 5         # 連打五場
ai_coc attack --repeat 0         # 一直打到某個倉庫滿為止
ai_coc stop                      # 打完當下這一場就收工
ai_coc giveback                  # 把借來的模擬器還給原本在跑的迴圈
ai_coc attack --stop-at 0        # 不管倉庫多滿都照打
ai_coc attack --repeat 0 --until-idle  # 這個村莊有工人或實驗室閒著也收工
ai_coc attack --repeat 0 --until-idle builder  # 只看工人 (`lab` 只看實驗室)
```

`stop` 在 `~/.ai_coc/state.json` 上做個記號就回來。迴圈在回合之間跟對手之間讀它, 絕不會在戰鬥中途停, 所以最壞是多打一場: 打到一半棄權會把軍隊丟在場上, 遊戲停在下一輪回不了家的畫面。沒有東西在跑的時候它會直接說沒有, 而不是留一個沒人會收的請求。

戰利品門檻讀設定檔, 也可以只蓋過這一次。**不給旗標跟給 `0` 是兩件事**: 不給是沿用設定檔的值, `0` 才是把那條門檻整個拿掉:

```bash
ai_coc attack --min-gold 800000
ai_coc attack --min-gold 0 --min-elixir 0 --min-dark 0    # 打第一個看到的對手
```

一輪可以把它看過的畫面全部留下來, 那是事後跟這場吵架的依據:

```bash
ai_coc attack --record                # 迴圈自己讀的每一張, 檔名就是它當下在問什麼
ai_coc attack --record --shot-every 4 # 另外每四秒再存一張
```

戰術是一份檔案而不是寫死的常數, 所以打得好的那場可以重來, 打得爛的那場可以改。重播的時候完全不會呼叫 Gemini:

```bash
ai_coc attack --plan-out used.json    # 把這一場實際用的計畫存下來
ai_coc attack --plan used.json     # 照那份再打一次, 改過的也算
```

### 把打到的花掉

```bash
ai_coc walls                          # 拿倉庫去升級城牆, 買到錢不夠為止
ai_coc walls --keep-elixir 2000000    # 留這麼多聖水下來練兵
ai_coc upgrade                        # 把閒著的工人派去做買得起最貴的升級
ai_coc hero                           # 每個英雄升下一級要多少
ai_coc hero --upgrade queen           # 真的派一個工人去升那個英雄
```

升級城牆要用掉一個空閒工人, 只是它付錢當下就升好、工人立刻還回來, 所以工人全忙的時候一片也買不了, `walls` 會停下來講。`hero` 預設只讀不花錢, 要指名哪個英雄才會真的升。哪個英雄值得一個工人是關於村莊怎麼玩的判斷, 不是價格能決定的。

### 顧著村莊

```bash
ai_coc collect                        # 把有東西的採集器全部收掉
ai_coc builders                       # 每個工人在蓋什麼、還要多久
ai_coc worker                         # 同樣的東西，但看當下那個世界，不切世界
ai_coc lab                            # 那個世界的實驗室在研究什麼、還要多久
ai_coc status                         # 工人、實驗室、倉庫跟護盾，一次讀完
ai_coc donate                         # 部落裡有人要兵就捐
ai_coc donate --dry-run               # 走完整個流程但停在真的捐出去之前
```

### 把遊戲弄起來

其他每個指令都假設遊戲已經在跑, 沒開的話它們自己會開。它們處理不了的是「開著但不理人」的模擬器或遊戲, 那是這幾個的用途:

```bash
ai_coc launch                         # 模擬器沒開就開, 然後把遊戲叫起來
ai_coc launch --restart game          # 只重開遊戲, 模擬器不動
ai_coc launch --restart emulator      # 連模擬器一起重開, 再把遊戲叫起來
```

### 看它看到了什麼

```bash
ai_coc capture --count 30             # 從活著的遊戲連續抓畫面,存進這次執行自己的資料夾
ai_coc <任何指令> --label baseline    # 給這次的資料夾取名字,之後找得回來
ai_coc <任何指令> --agent codex --session <id> --mission "打資源"  # 誰叫的; agent 一律要帶, 沒帶會在 log 留 warning
ai_coc read shot.png                  # 每個判讀器從這張圖讀到什麼
ai_coc export                         # 從遊戲裡取出整個村莊,對照過名稱,吐 JSON
ai_coc export --table                 # 同一份東西,畫成表格給人看
ai_coc export --last                  # 直接讀上一次的結果,完全不碰遊戲
ai_coc view --zoom out                # 把鏡頭拉回所有座標當初量測的那個視野
ai_coc world                          # 現在在日世界還是夜世界
ai_coc world --go day                 # 坐船切過去,已經在那邊就什麼都不做
ai_coc attack                         # 打遊戲當下所在的那個村莊
```

遊戲有兩個村莊,日世界 (主村) 跟夜世界 (建築大師基地),而它會開在上次離開的那一個,所以任何預設站在主村上的指令之前都值得先問一次 `world`。讀它只花一張截圖,不點任何東西也不動鏡頭。**沒有指令會自己坐船**: 換村莊只有 `world --go` 一個,而在當下的村莊沒事可做的指令 (例如在夜世界跑 `walls`) 會直接說明,遊戲留在原地。

`read` 是回答「是判讀錯了, 還是那一下沒點到」最快的方法: 它把一張圖丟給每個判讀器, 印出戰利品面板、倉庫、卡片列、工人面板各自讀到什麼。

### 這次執行看到的東西放在哪

每一次執行都有自己的目錄, 不管是從哪一邊開的:

```
~/.ai_coc/logs/2026-08-29-011423-attack/
├── run.log        # 這一次的紀錄, 不摻別的
├── result.json    # 這個指令回答了什麼
├── plans.jsonl    # 只有 ai_coc attack 有: 每打一場一行, 那一場用的戰術
└── frames/        # 只有 --record 才有
```

目錄在哪, 開跑第一行就會說, 而名字是「時間-指令」, 所以整個列出來就是一份歷史。任何指令都可以加 `--label` 再接一段上去 (`2026-08-29-011423-attack-baseline`), 那是給之後要找回來的那一次用的。沒有第二份把所有執行混在一起的檔案 —— 要跨好幾次找模式就 `grep -r ~/.ai_coc/logs/*/run.log`, 而且它會告訴你每一筆是哪一次跑出來的。

**畫面留七天, log 永久留。** 一次執行的 `frames/` 過了七天就會被刪掉, 同個目錄裡其他東西一律不動。這個差別值得講清楚, 因為它非常不對稱: 這台機器上 11 456 張畫面加起來 27.2 GB, 而所有 `run.log`、`result.json`、`plans.jsonl` 加起來只有 4.6 MB。要留久一點的東西自己複製出來。

## ⚙️ 設定

`~/.ai_coc/config.json` 視窗跟終端機都會讀, 所以兩邊跑出來的結果一樣:

```json
{
  "thresholds": {
    "min_gold": 500000,
    "min_elixir": 500000,
    "min_dark": 5000
  },
  "stop_at": 90,
  "adb_serial": "127.0.0.1:16384",
  "gemini": {
    "api_key": "",
    "main": {
      "model": "gemini-3.5-flash",
      "base_url": "",
      "thinking_level": "low"
    },
    "lite": {
      "model": "gemini-3.5-flash-lite",
      "base_url": "",
      "thinking_level": "low"
    }
  },
  "ui": {
    "jobs": {
      "collect": false,
      "donate": false,
      "upgrade": false,
      "walls": false,
      "attack": false
    },
    "cycle_minutes": 10,
    "live_view": true,
    "record_frames": false
  }
}
```

這就是整份檔案。**找不到檔案的那次執行只會為了記下 `adb_serial` 建一份** —— 其他時候直接用上面這些值, 不動硬碟 —— 所以它是第一次有指令或視窗挑好模擬器、或視窗第一次寫它的時候才出現的, 按下「儲存設定」或者動一下預覽上面那兩個開關都算。存在之後每次載入都會被校正一次, 而且是哪一邊先載入就由哪一邊校正: 程式不再讀的鍵會被刪掉, 檔案裡沒有的鍵會被補上, 所以硬碟上那份永遠是「下一次跑會怎麼跑」而不是「某一次存過什麼」。

- **thresholds**: 誰值得打。訂太高的話一輪會跳過幾十個對手還開不了打
- **stop_at**: 每一種資源都滿到幾 % 就收工。**主村跟夜世界共用這一個數字**, 因為倉庫的容量是迴圈自己去遊戲裡讀的 —— 點一下儲量條, 遊戲就把 最大儲存量 寫在旁邊。要**每一項**都滿了才停, 不是任何一項到頂就走 —— 一場仗帶回來三種資源, 一個倉庫到頂不足以放棄另外兩種還在賺的。夜世界的聖水車也算一個: 聖水倉庫滿了之後, 每一場的防守獎勵還會存進車子裡, 所以那邊要連車子也滿到同一個 % 才收工, 車子的上限寫在它自己的面板上。`0` 代表永遠不收工, 容量讀不到的那一項也不列入計算。`--stop-at` 只蓋過這一次, 給 `0` 也可以, 那是拿一個剛被打滿的村莊測一場戰鬥時要用的
- **adb_serial**: 要驅動哪一台模擬器, 寫 `adb devices` 列出來的那個名字 (`127.0.0.1:16384`、`127.0.0.1:5555`, 或者 `emulator-5554`, 它跟 `127.0.0.1:5555` 是同一台雷電)。留空的話第一次執行會挑正在跑遊戲的那一台, 兩邊都有就用 MuMu, 然後把它的名字寫回這裡; 在視窗的模擬器清單裡選一台也會寫進來。寫了一個沒有模擬器認得的名字會直接報錯, 不會去猜
- **gemini**: 哪個模型接哪一種呼叫。`main` 一整趟只問一次, 而且問的是一整張截圖 —— 進攻計畫, 找目標 —— 沒有在跟誰搶時間, 所以就用比較好的那個。`lite` 是**每一個候選問一次**, 而且只給一條裁下來的窄帶, 那是分類不是判斷, 便宜的模型的位置在這裡。`base_url` 留空就是 Google 官方端點。`api_key` 是兩個 tier 共用的那一把金鑰, 以明文存放; 設定分頁的「儲存設定」會寫進來, 它空著的時候改用環境變數或 `.env` 裡的 `GEMINI_API_KEY`
- **ui**: 只有視窗會讀的東西, 也是最後一批不在這份檔案裡的。它們本來住在 Windows 登錄檔, 那裡沒有編輯器打得開, 也沒有任何一個 `ai_coc` sub-command 讀得到 —— 理由是「終端機不會問的設定不該放進共用檔案」, 而那條規矩換來的就是使用者唯一改不到的東西剛好是視窗自己的行為。`jobs` 是視窗那一輪會輪流跑哪幾個指令, 鍵名就是 sub-command 的名字, 所以 `"walls": true` 跑的跟 `ai_coc walls` 是同一個工作, 只是輪到它的時候只跑一趟, 而不是像指令自己的預設那樣刷到倉庫撐不住為止。`cycle_minutes` 只會用在什麼都沒做的那一輪, 因為一個工作跑完會直接排下一輪。`live_view` 是主控分頁裡那個即時預覽, `record_frames` 則是 `--record` 的打勾版

大招與法術的秒數不在這裡了。以前它是一張按英雄寫死的表, 而要填那張表, 就得在沒看過村莊的情況下猜軍隊要走多久 —— 那是規劃那一步的事, 而它是看著村莊做的。現在每一個時鐘都寫在計畫裡: 形狀看 `plans/flat.json`, 要重播一份就用 `--plan`。

其他東西都放在 `~/.ai_coc`: `ai_coc export` 存下的帳號 JSON、每次執行的 log, 還有 `state.json` —— 現在是哪個指令在驅動模擬器、它的 pid、log 目錄, 還有是誰開的 (`--agent`、`--session`、`--mission`, 或 `window`)。跑完之後不會刪掉, 所以那份記錄說的是上一次跑的是什麼, 而不是變成空的。它也是一把鎖: 要操作模擬器的指令會先請佔著的那次執行收工, 等它退出; 那次執行的程序已經不在的話就直接接手。帶 `--yield` 開的是背景工作: 它不會停掉別人, 別人要用模擬器時它是被借走而不是被停掉, `ai_coc giveback` 還回來 (或借用的那一邊最後一個指令跑完半小時) 之後, 它的 agent 會把它開回去。

## 🤝 參與貢獻

開發環境、架構、打包發布跟 CI 的說明都在 [CONTRIBUTING.md](https://github.com/Mai0313/ai_coc/blob/main/.github/CONTRIBUTING.md)。

## ⚠️ 免責聲明

本專案與 Supercell 沒有任何關係, 也沒有得到 Supercell 的認可或贊助。《部落衝突》(Clash of Clans) 是 Supercell 的商標, 遊戲的名稱與美術素材都屬於 Supercell。

Supercell 的服務條款禁止使用自動化軟體、模擬器和機器人, 並允許 Supercell 停權或刪除使用這些工具的帳號。執行這個工具, 就是讓它操作的帳號承擔這個風險, 請自行斟酌使用。

## 📄 授權

MIT, 見 `LICENSE`。
