# CoC AI Controller

[![Tests](https://github.com/Mai0313/CoC-AI-Controller/actions/workflows/test.yml/badge.svg)](https://github.com/Mai0313/CoC-AI-Controller/actions/workflows/test.yml)
[![Code Quality](https://github.com/Mai0313/CoC-AI-Controller/actions/workflows/code-quality-check.yml/badge.svg)](https://github.com/Mai0313/CoC-AI-Controller/actions/workflows/code-quality-check.yml)
[![Code Scanning](https://github.com/Mai0313/CoC-AI-Controller/actions/workflows/code_scan.yml/badge.svg)](https://github.com/Mai0313/CoC-AI-Controller/actions/workflows/code_scan.yml)

[English](README.md) | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

CoC AI Controller 是一套執行於 Windows 的 Clash of Clans AI agent 平台。V1 的目標是由 Gemini 驅動的通用操作者，能理解帳號狀態與當下的 MuMu 畫面，執行經過驗證的導航與出兵準備，並在 Enemy Preview 把控制權交給未來的 RL 戰鬥控制器。

目前狀態：VER 0.1.0 Windows MVP 已建置並完成 smoke test。

## 執行 Windows 版本

打開完整的 `CoC_AI_Controller_VER_0.1.0` release 資料夾，雙擊 `CoC_AI_Controller_VER_0.1.0.exe`。不要把 EXE 搬出該資料夾，旁邊的 `_internal` runtime 是必要的。

啟動後 Emulators 頁面會非同步偵測已安裝的 MuMu。按 Refresh、選一個 instance，接著 Connect / Screenshot 或 Launch CoC。Gemini 相關功能需要在 Settings 存入有效的 API key；該 key 在本機以 Windows DPAPI 保護，不會被 commit。

## 開發

需要 Python 3.12 與 [uv](https://docs.astral.sh/uv/)。安裝與測試：

```powershell
uv sync --group test --group build
uv run pytest
```

執行 `scripts/build.ps1` 會跑測試並在 `outputs` 下產生 PyInstaller onedir build。GitHub Actions 也會把解壓後的 Windows 應用程式當成 build artifact 發布。

建議從這幾份文件開始看：

- [docs/handoff.md](docs/handoff.md)：目前的專案狀態與下一步工作。
- [docs/reference-audit.md](docs/reference-audit.md)：參考專案與本機 MuMu 的調查結果。
- [docs/architecture.md](docs/architecture.md) 與 [docs/requirements.md](docs/requirements.md)：已確認的 V1 方向。
- [docs/decisions.md](docs/decisions.md) 與 [docs/todo.md](docs/todo.md)：限制條件與規劃中的工作。

安全性：絕對不要 commit API key、ADB key、私人帳號 JSON、個人截圖或本機模擬器設定。
