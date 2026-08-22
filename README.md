# CoC AI Controller

[![Tests](https://github.com/Mai0313/CoC-AI-Controller/actions/workflows/test.yml/badge.svg)](https://github.com/Mai0313/CoC-AI-Controller/actions/workflows/test.yml)
[![Code Quality](https://github.com/Mai0313/CoC-AI-Controller/actions/workflows/code-quality-check.yml/badge.svg)](https://github.com/Mai0313/CoC-AI-Controller/actions/workflows/code-quality-check.yml)
[![Code Scanning](https://github.com/Mai0313/CoC-AI-Controller/actions/workflows/code_scan.yml/badge.svg)](https://github.com/Mai0313/CoC-AI-Controller/actions/workflows/code_scan.yml)

[English](README.md) | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

CoC AI Controller is a Windows AI-agent platform for Clash of Clans. The V1 goal is a Gemini-powered general operator that understands account state and the current MuMu screen, performs verified navigation and battle preparation, and hands control to a future RL battle controller at Enemy Preview.

Current status: VER 0.1.0 Windows MVP built and smoke-tested.

## Run the Windows build

Open the complete `CoC_AI_Controller_VER_0.1.0` release folder and double-click `CoC_AI_Controller_VER_0.1.0.exe`. Do not move the EXE out of its folder; the adjacent `_internal` runtime is required.

On startup the Emulators page detects the installed MuMu asynchronously. Use Refresh, choose an instance, then Connect / Screenshot or Launch CoC. Gemini features require a valid API key saved under Settings; the key is protected locally with Windows DPAPI and is never committed.

## Development

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/). Install and test with:

```powershell
uv sync --group test --group build
uv run pytest
```

Run `scripts/build.ps1` to execute tests and create the PyInstaller onedir build under `outputs`. GitHub Actions also publishes the unpacked Windows application as a build artifact.

Start with:

- `HANDOFF.md` for current project status and next work.
- `docs/reference-audit.md` for the reference-project and local MuMu findings.
- `docs/architecture.md` and `docs/requirements.md` for the approved V1 direction.
- `docs/decisions.md` and `docs/todo.md` for constraints and planned work.

Security: never commit API keys, ADB keys, private account JSON, personal screenshots or local emulator configuration.
