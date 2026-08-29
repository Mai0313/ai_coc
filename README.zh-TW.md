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

一個 Windows 桌面應用程式, 用來玩跑在 MuMu 模擬器 12 裡的《部落衝突》。畫面判讀是它自己做的, 讀到什麼就轉成 ADB 點擊, 並在下一張截圖上確認結果。Gemini 每一場只被問一個問題: 這個村莊該怎麼打。

其他語言版本：[English](README.md) | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

## ✨ 功能

**自己打資源。** 搜尋對手, 跳過戰利品不到門檻的, 十秒左右把整支軍隊放下去, 打完回營再來下一場, 倉庫滿了自己停。畫面判讀是在本機做的而不是送出去問: 戰利品面板、兵力條、卡片列全部靠模板比對, 所以跳過一個對手不花錢, 一場戰鬥也不會卡在網路上。

**每一場只問 Gemini 一個問題**, 而且是在對手已經通過門檻之後才問: 這個村莊該怎麼打。它回答投兵線的兩端、每瓶狂暴跟冰凍的落點, 還有每張英雄卡是誰。沒有 API key 的話會改用固定戰術, 其他功能照常。

**把打到的花掉。** 城牆付錢的當下就升級完成, 不佔工人也不跑計時器, 所以倉庫滿了就是它的出口: 迴圈自己在地圖上找牆, 算出村莊買得起的最便宜批次再買。閒著的工人可以派去做買得起的最貴升級, 英雄也能一個一個往上升。

**顧著村莊不停擺。** 收採集器、讀每個工人在蓋什麼還剩多久、有人在部落聊天室要兵就捐給他。

**自己爬得回來。** 掛太久被踢掉的連線會自動重開遊戲繼續跑, 不管當下在跑哪個迴圈。AI 助手打的每一個指令都存成任務, 中途斷掉下次啟動會接著做完。

**金鑰不放在明文設定裡。** Gemini API key 走 Windows DPAPI, 不進 registry 也不進設定檔。

匯入的村莊 JSON 會保留看不懂的欄位跟 `data_id` 而不是直接失敗; 匯入的戰鬥腳本不會被播放, 只驗證兵力需求, 停在保留的交接邊界。

## 📋 環境需求

- Windows。程式要呼叫 `mumu-cli.exe`、用 `winreg` 讀登錄檔、用 `ctypes.windll` 呼叫 DPAPI, 這些在別的系統上都不存在
- [MuMu 模擬器 12](https://www.mumuplayer.com/), 裝好部落衝突, 解析度 1600x900
- 一把 Gemini API key。每場的戰術建議跟整個 AI 助手分頁都要它, 在設定分頁填。沒有它的話進攻迴圈會退回固定戰術, 而刷牆、收採集器、工人、英雄、捐兵那幾個指令本來就不會問它任何事

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
ai_coc attack --restart-every 0  # 這一次不要重開模擬器
ai_coc online                    # 留在遊戲裡保持上線,別人就打不了這個村莊
```

**沒在打的時候就掛著上線.** 部落衝突不會讓人攻擊一個主人正在線上的村莊, 所以兩輪打資源之間掛在遊戲裡比登出安全。`ai_coc online` 用最小的輸入把連線撐著 —— 在空地上輕輕拖一下, 每次方向相反所以不會把鏡頭拖走 —— 並且跟這裡所有東西一樣聽 `ai_coc stop`。多久動一次是設定檔的 `keepalive_seconds`, 視窗那邊是「保持上線」這個勾選項。

**每打幾場就重開一次模擬器**, 因為 MuMu 跑久了會掉幀, 而只有重開才清得掉。幾場寫在設定檔 (`restart_every`, 預設 50) 而不是寫死, 因為那個數字是每台機器自己的脾氣 —— `--restart-every` 蓋過這一次, 給 `0` 就是這次不重開, 跟戰利品旗標同一個規矩。它數的是真的打成的場次而不是回合, 所以一整夜大半在等兵營的跑, 不會把重開浪費在一台幾乎沒在做事的模擬器上。

`stop` 寫一個旗標檔就回來。迴圈在回合之間跟對手之間讀它, 絕不會在戰鬥中途停, 所以最壞是多打一場: 打到一半棄權會把軍隊丟在場上, 遊戲停在下一輪回不了家的畫面。

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
ai_coc attack --plan-in used.json     # 照那份再打一次, 改過的也算
```

### 把打到的花掉

```bash
ai_coc walls                          # 拿倉庫去升級城牆, 買到錢不夠為止
ai_coc walls --keep-elixir 2000000    # 留這麼多聖水下來練兵
ai_coc upgrade                        # 把閒著的工人派去做買得起最貴的升級
ai_coc hero                           # 每個英雄升下一級要多少
ai_coc hero --upgrade queen           # 真的派一個工人去升那個英雄
```

城牆自己不佔工人, 但工人全忙的時候遊戲會把整批退掉, 所以 `walls` 會停下來講。`hero` 預設只讀不花錢, 要指名哪個英雄才會真的升。哪個英雄值得一個工人是關於村莊怎麼玩的判斷, 不是價格能決定的。

### 顧著村莊

```bash
ai_coc collect                        # 把有東西的採集器全部收掉
ai_coc builders                       # 每個工人在蓋什麼、還要多久
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
ai_coc capture ./shots --count 30     # 從活著的遊戲連續抓畫面
ai_coc read shot.png                  # 每個判讀器從這張圖讀到什麼
ai_coc view --zoom out                # 把鏡頭拉回所有座標當初量測的那個視野
```

`read` 是回答「是判讀錯了, 還是那一下沒點到」最快的方法: 它把一張圖丟給每個判讀器, 印出戰利品面板、倉庫、卡片列、工人面板各自讀到什麼。

### 這次執行看到的東西放在哪

每一次執行都有自己的目錄, 不管是從哪一邊開的:

```
~/.ai_coc/logs/2026-08-29-011423-attack/
├── run.log        # 這一次的紀錄, 不摻別的
├── result.json    # 這個指令回答了什麼
└── frames/        # 只有 --record 才有
```

目錄在哪, 開跑第一行就會說, 而名字是「時間-指令」, 所以整個列出來就是一份歷史。`controller.log` 還在旁邊, 裡面是所有執行混在一起的那一份 —— 要找跨好幾天的模式看它, 要看某一次就看那一次自己的 `run.log`。

## ⚙️ 設定

`~/.ai_coc/config.json` 視窗跟終端機都會讀, 所以兩邊跑出來的結果一樣:

```json
{
  "thresholds": {
    "min_gold": 500000,
    "min_elixir": 500000,
    "min_dark": 5000
  },
  "stock": {
    "stop_gold": 15000000,
    "stop_elixir": 15000000,
    "stop_dark": 0
  },
  "timings": {
    "queen": 1,
    "warden": 30,
    "champion": 45,
    "freeze": 30
  }
}
```

- **thresholds**: 誰值得打。訂太高的話一輪會跳過幾十個對手還開不了打
- **stock**: 什麼時候收工。任何一項到頂就結束, 不是等三項都滿。`0` 代表這一項不看
- **timings**: 每個英雄**落地之後**幾秒放大招, 以及每個法術在**開打之後**幾秒丟。故意是兩個時鐘: 女皇的斗篷值得在她落地一秒後就放, 而法術要是掛在英雄身上, 就會跟著軍隊下場花了多久一起飄。是按英雄而不是按卡片位置記的, 因為升級中的英雄根本沒有卡片

其他東西都放在 `~/.ai_coc`: SQLite 資料庫、存下來的畫面、匯入的帳號 JSON、log, 還有 DPAPI 保護的金鑰檔。

## 🤝 參與貢獻

開發環境、架構、打包發布跟 CI 的說明都在 [CONTRIBUTING.md](https://github.com/Mai0313/ai_coc/blob/main/.github/CONTRIBUTING.md)。

## 📄 授權

MIT, 見 `LICENSE`。
