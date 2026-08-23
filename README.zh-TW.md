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

一個 Windows 桌面應用程式，用來操作跑在 MuMu 模擬器 12 裡的《部落衝突》。Gemini 負責讀畫面，這個程式把它的回答轉成 ADB 點擊，並在下一張截圖上確認結果。

其他語言版本：[English](README.md) | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

## ✨ 功能

- 偵測 MuMu 模擬器 12 的執行個體，啟動模擬器並開啟遊戲
- 透過 ADB 擷取畫面並詢問 Gemini 畫面上有什麼，結構化回覆一律經過 Pydantic 驗證
- 執行 agent 迴圈，可以點擊、滑動與返回，每一步之後重新觀察畫面
- 匯入村莊 JSON 與戰鬥腳本，遇到未知欄位與未知的 `data_id` 會保留而不是讓匯入失敗
- 每個 agent 指令都會存成一筆任務，中斷的執行會在下次啟動時繼續
- Gemini API key 透過 Windows DPAPI 保存，不會以明文寫進設定

即時戰術操作刻意不在範圍內：戰鬥腳本只驗證兵種需求，到保留的交接邊界為止。

## 📋 環境需求

- Windows。程式會呼叫 `mumu-cli.exe`、透過 `winreg` 讀登錄檔、用 `ctypes.windll` 呼叫 DPAPI，這些在其他平台上都不存在
- [MuMu 模擬器 12](https://www.mumuplayer.com/)，已安裝《部落衝突》，解析度設為 1600x900
- 一組 Gemini API key，在程式的設定分頁輸入

## 🚀 安裝與執行

從 PyPI 直接跑，不留下任何安裝：

```bash
uvx ai_coc
```

或是正式安裝：

```bash
uv tool install ai_coc
ai_coc
```

每個 [release](https://github.com/Mai0313/ai_coc/releases) 也都附上預先打包好的 Windows 執行檔。

## 🛠️ 本地開發

```bash
git clone https://github.com/Mai0313/ai_coc.git
cd ai_coc
uv sync --group test          # 安裝相依套件
uvx pre-commit install        # 安裝 git hooks
uv run ai_coc                 # 啟動程式
```

另外有兩個命令列開關可以做煙霧測試。`--live-test` 會擷取一張畫面並請 Gemini 描述它，`--agent-command=<text>` 會把指令打進 AI 分頁並執行。兩者在 `COC_LIVE_TEST_SCREENSHOT` / `COC_AGENT_SCREENSHOT` 指向某個路徑時會存下截圖存證。

## 🧰 指令參考

```bash
# 開發
make help               # 列出可用的 make 目標
make clean              # 清除快取、產出物與產生的文件
make fmt                # 執行所有 pre-commit hooks
make test               # 對整個 repo 執行 pytest
make gen-docs           # 從 src/ 與 scripts/ 產生文件

# 相依套件（透過 uv）
make uv-install         # 在系統上安裝 uv
uv add <pkg>            # 新增正式相依套件
uv add <pkg> --dev      # 新增開發相依套件
# 安裝選用的群組
uv sync --group dev     # 只裝開發用相依（pre-commit、poe、notebook）
uv sync --group test    # 只裝測試用相依
uv sync --group docs    # 只裝文件用相依
```

## 🧱 架構

三個層次就是三個目錄，所以跨層的 import 在 import 那一行就看得出來：

- **UI 與流程調度**：`ui/main_window.py` 放主視窗與所有工作流程，`ui/workers.py` 放執行緒池的 worker，`ui/render.py` 負責 Markdown 與日誌的呈現。`cli.py` 只有 `main()`
- **轉接層**：`adapters/mumu.py`（模擬器生命週期）、`adapters/adb.py`（所有 ADB 呼叫）、`adapters/ai.py`（Gemini）、`adapters/secrets.py`（DPAPI）、`adapters/database.py`（SQLite）
- **純解析器**：`parsers/village.py`、`parsers/battle.py`

每一個結構化的值都是 Pydantic model，全部集中在 `models.py`。會阻塞的呼叫一律走 `QThreadPool` 的 worker，再用 signal 回到 UI 執行緒。

程式狀態放在 `~/.ai_coc`：SQLite 資料庫、擷取的畫面、匯入的帳號 JSON，以及 DPAPI 保護的 key 檔。

## 📚 文件

文件用 [Zensical](https://zensical.org/) 建置，並由 `scripts/gen_docs.py` 從原始碼自動產生。

```bash
uv sync --group docs
make gen-docs                  # 從原始碼產生 markdown
uv run zensical serve          # http://0.0.0.0:9987
```

`make gen-docs` 會重建 `docs/`，把三份 README 複製進去，再對 `./src` 與 `./scripts` 執行 `gen_docs.py`。

## 📦 打包與發布

用 uv 建置產出物，wheel 與 sdist 會放到 `dist/`：

```bash
uv build
```

發布到 PyPI（需要 `UV_PUBLISH_TOKEN`）：

```bash
UV_PUBLISH_TOKEN=... uv publish
```

推一個 `v*` tag 會觸發 `build_release.yml`，它用 `dunamai` 從 git 推導版本號，建置 wheel 與 sdist、發布到 PyPI，再用 PyInstaller 打包 Windows 版本，最後把全部產出物掛到 GitHub Release 上。

打包預設走 `--onedir`：zip 裡是執行檔加上一個 `_internal/` 資料夾，啟動比單檔版本快好幾秒，因為單檔版本每次啟動都要先把自己解壓開。手動觸發這個 workflow 時有一個 `package_mode` 選項，需要單一 `.exe` 的話從那裡選。

## 🧭 選用的任務執行器（Poe the Poet）

方便用的任務定義在 `pyproject.toml` 的 `[tool.poe.tasks]`，安裝 dev 群組（`uv sync --group dev`）之後或透過 `uvx` 就可以用：

```bash
uv run poe docs        # 產生並啟動文件伺服器（需要 dev 群組）
uv run poe gen         # 產生並部署文件（gh-deploy）（需要 dev 群組）
uv run poe main        # 啟動程式（等同 uv run ai_coc）

# 或是用 uvx 臨時執行，不安裝到本地
uvx poe docs
```

## 🔁 CI/CD 流程總覽

所有的 workflow 都放在 `.github/workflows/`。

- 測試（`test.yml`）

    - 觸發時機：推送與 pull request 到 `main` 或 `release/*`（忽略 md 檔）
    - 在 Python 3.12/3.13/3.14 上跑 pytest 與覆蓋率，並留下摘要留言

- 程式碼品質檢查（`code-quality-check.yml`）

    - 觸發時機：pull request
    - 執行 ruff 與其餘的 pre-commit 檢查

- 文件部署（`deploy.yml`）

    - 觸發時機：推送到 `main` 以及 `v*` tag
    - 建置 `zensical` 網站並發布到 GitHub Pages
    - 需要設定：在 repo 開啟 GitHub Pages（Settings → Pages → Source: GitHub Actions）

- 建置與發布（`build_release.yml`）

    - 觸發時機：推送 `v*` tag 或手動觸發
    - 用 PyInstaller 建置 Windows x64 執行檔，另外建置 wheel 與 sdist
    - 發布到 PyPI（需要 `UV_PUBLISH_TOKEN` secret），並把所有產出物上傳到 GitHub Release

- 發布 Docker 映像檔（`build_image.yml`）

    - 觸發時機：推送到 `main` 以及 `v*` tag
    - 建置映像檔並推送到 GHCR：`ghcr.io/<owner>/<repo>`

- Release Drafter（`release_drafter.yml`）

    - 觸發時機：推送到 `main` 以及 PR 事件
    - 依照 Conventional Commits 維護一份草稿 release

- 程式碼掃描（`code_scan.yml`）

    - 觸發時機：推送與 PR
    - 執行 gitleaks；CodeQL 那個 job 需要 GitHub Advanced Security，在 repo 還是 private 的期間會被略過

- 語意化 Pull Request（`semantic-pull-request.yml`）

    - 觸發時機：PR 開啟、編輯、同步
    - 強制 PR 標題符合 Conventional Commit 格式

### CI/CD 設定檢查清單

- PR 標題要用 conventional commits（由 workflow 強制）
- 設定 `UV_PUBLISH_TOKEN` secret 才能發布到 PyPI（Settings → Secrets and variables → Actions）
- 選用：開啟 GitHub Pages 以部署文件（Settings → Pages → Source: GitHub Actions）
- Container Registry 的權限由 `GITHUB_TOKEN` 自動處理

## 🤝 參與貢獻

- 歡迎開 issue 或 PR
- 遵循既有的程式碼風格（ruff、type hints）
- 使用 Conventional Commit 訊息與清楚的 PR 標題

## 📄 授權

MIT，詳見 `LICENSE`。
