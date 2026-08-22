# Development Session Log

## 2026-08-22 — VER 0.1.0

Task: Produce the first runnable CoC AI Controller Windows MVP after Phase 0.

Added: PyQt5 application, SQLite account/knowledge store, MuMu CLI/ADB adapter, Gemini provider with DPAPI key storage, semantic vision/chat/teaching, Battle Script reader, tests and PyInstaller build workflow.

Removed: none.

Fixed: ensured SQLite connections close on Windows after tests exposed a locked temporary DB.

Tests: 2 core unit tests passed; live MuMu version/instance/ADB/CoC/screenshot probes passed; packaged EXE stayed running with the correct `CoC AI Controller — VER 0.1.0` main-window title.

Known issues: sourced master level data and verified gem rules are incomplete; automated navigation/preparation is not unattended-ready.

Next: build/smoke-test EXE, then expand source-backed master data and guarded semantic actions.
