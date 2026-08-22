# Development Session Log

## 2026-08-22 — Logging, google-genai and Pydantic

Task: the AI Village-JSON import failed with nothing shown anywhere, so make the application observable, then move AI and ADB onto the libraries meant for them.

Added: `logging_setup.py` and the 執行紀錄 panel; `adb.py` with `AdbController` over adbutils; the Settings model picker fed by `GeminiClient.list_text_models`.

Changed: `ai.py` rewritten on `google-genai` (Interactions API, structured output); `mumu.py` delegates all ADB work to `adb.py`; all structured data is now Pydantic, including `Database` inputs and outputs.

Removed: the OpenAI-compatible Gemini path, the hand-rolled `urllib` request/retry code, and the manual ```` ```json ```` stripping.

Tests: `uv run pytest` (3 passed, coverage 18%); an offscreen `MainWindow` smoke run confirmed the log panel fills, adbutils picks up MuMu's own `adb.exe` and instance `127.0.0.1:16480` is enumerated.

Known issues: the packaged build has not been re-verified since google-genai, adbutils and pydantic joined the dependency set. Screenshots and taps still target the emulator's default display, so an app MuMu opened on a secondary display would be captured and tapped incorrectly.

Next: rerun `scripts/build.ps1` and confirm the EXE, then decide whether display selection needs to be explicit.

## 2026-08-22 — VER 0.1.0

Task: Produce the first runnable CoC AI Controller Windows MVP after Phase 0.

Added: PyQt5 application, SQLite account/knowledge store, MuMu CLI/ADB adapter, Gemini provider with DPAPI key storage, semantic vision/chat/teaching, Battle Script reader, tests and PyInstaller build workflow.

Removed: none.

Fixed: ensured SQLite connections close on Windows after tests exposed a locked temporary DB.

Tests: 2 core unit tests passed; live MuMu version/instance/ADB/CoC/screenshot probes passed; packaged EXE stayed running with the correct `CoC AI Controller — VER 0.1.0` main-window title.

Known issues: sourced master level data and verified gem rules are incomplete; automated navigation/preparation is not unattended-ready.

Follow-up fix: user screenshot exposed a packaged mumu-cli Qt plugin conflict. Sanitized `QT_PLUGIN_PATH`, `QT_QPA_PLATFORM`, `QT_QPA_PLATFORM_PLUGIN_PATH` and `QML2_IMPORT_PATH` before MuMu subprocesses; rebuild and source-level polluted-environment probe passed.

Next: expand source-backed master data and guarded semantic actions.
