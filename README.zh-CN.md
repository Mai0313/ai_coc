# CoC AI Controller

[![Tests](https://github.com/Mai0313/CoC-AI-Controller/actions/workflows/test.yml/badge.svg)](https://github.com/Mai0313/CoC-AI-Controller/actions/workflows/test.yml)
[![Code Quality](https://github.com/Mai0313/CoC-AI-Controller/actions/workflows/code-quality-check.yml/badge.svg)](https://github.com/Mai0313/CoC-AI-Controller/actions/workflows/code-quality-check.yml)
[![Code Scanning](https://github.com/Mai0313/CoC-AI-Controller/actions/workflows/code_scan.yml/badge.svg)](https://github.com/Mai0313/CoC-AI-Controller/actions/workflows/code_scan.yml)

[English](README.md) | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

CoC AI Controller 是一套运行于 Windows 的 Clash of Clans AI agent 平台。V1 的目标是由 Gemini 驱动的通用操作者，能理解账号状态与当前的 MuMu 画面，执行经过验证的导航与出兵准备，并在 Enemy Preview 把控制权交给未来的 RL 战斗控制器。

当前状态：VER 0.1.0 Windows MVP 已构建并完成 smoke test。

## 运行 Windows 版本

打开完整的 `CoC_AI_Controller_VER_0.1.0` release 文件夹，双击 `CoC_AI_Controller_VER_0.1.0.exe`。不要把 EXE 移出该文件夹，旁边的 `_internal` runtime 是必需的。

启动后 Emulators 页面会异步检测已安装的 MuMu。点击 Refresh、选择一个 instance，接着 Connect / Screenshot 或 Launch CoC。Gemini 相关功能需要在 Settings 中保存有效的 API key；该 key 在本机以 Windows DPAPI 保护，不会被 commit。

## 开发

需要 Python 3.12 与 [uv](https://docs.astral.sh/uv/)。安装与测试：

```powershell
uv sync --group test --group build
uv run pytest
```

运行 `scripts/build.ps1` 会跑测试并在 `outputs` 下生成 PyInstaller onedir build。GitHub Actions 也会把解压后的 Windows 应用程序作为 build artifact 发布。

建议从这几份文档开始看：

- [docs/handoff.md](docs/handoff.md)：当前的项目状态与下一步工作。
- [docs/reference-audit.md](docs/reference-audit.md)：参考项目与本机 MuMu 的调查结果。
- [docs/architecture.md](docs/architecture.md) 与 [docs/requirements.md](docs/requirements.md)：已确认的 V1 方向。
- [docs/decisions.md](docs/decisions.md) 与 [docs/todo.md](docs/todo.md)：限制条件与规划中的工作。

安全性：绝对不要 commit API key、ADB key、私人账号 JSON、个人截图或本机模拟器配置。
