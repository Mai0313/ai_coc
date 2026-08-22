# Changelog

## Unreleased

### Added

- 執行紀錄 panel under the tabs, mirroring every log record live, with a level selector (DEBUG shows full AI prompts and replies) and a shortcut to the rotating log file in `%LOCALAPPDATA%\CoC_AI_Controller\logs\`.
- Model picker in Settings: 測試連線並載入模型 verifies the API key and fills the dropdown with the text models the endpoint exposes.
- `AdbController` (`adb.py`), an adbutils-backed wrapper that owns every ADB call; `MuMuAdapter` keeps one per instance, so each emulator is addressed through its own ADB port.

### Changed

- Gemini now goes through the `google-genai` SDK's Interactions API instead of hand-rolled HTTP; the OpenAI-compatible code path is gone.
- Agent actions and on-screen target lookups use structured output validated into `AgentAction` / `LocatedTarget` instead of hand-parsing JSON out of markdown fences.
- Default model is `gemini-3.5-flash`; a saved OpenAI-compatible endpoint is ignored on load because it is not a valid google-genai base URL.
- Every structured value is a Pydantic model in `models.py`: village entities, account rows, knowledge, tasks, battle requirements, MuMu CLI payloads, UI elements and AI replies. `Database` takes and returns models, and saved account JSON is written with `model_dump_json`.

### Fixed

- Background failures no longer disappear: `Worker` logs the traceback before the message box, and `sys.excepthook` records what Qt used to swallow.

## VER 0.1.0 — 2026-08-22

### Added

- Runnable PyQt5 Windows desktop application with versioned About page.
- MuMu 6 CLI discovery, multi-instance list and stable instance/ADB/HWND identity display.
- Asynchronous MuMu refresh, screenshot, CoC launch/restart, Back and emulator lifecycle controls.
- Tolerant Village JSON import, per-account SQLite snapshot, ID registry join and unknown-ID queue.
- DPAPI-protected Gemini API key, connection test, semantic screenshot analysis and contextual AI chat.
- USER_CONFIRMED teaching records and versioned CoC Agent profile.
- Battle Script validation and explicit army-requirements preparation view with reserved RL handoff.
- Phase 0 audit, architecture, requirements, decisions, TODO and GitHub collaboration baseline.
- Fixed packaged MuMu CLI launch by isolating it from PyQt Qt plugin environment variables.

### Known limitations

- Automated navigation and Battle Preparation are not yet safe enough for unattended operation; V1 exposes verified primitives and requirement planning.
- Shared master level/cost/time data and verified current gem-cost rules are not populated yet; the UI shows unavailable fields instead of invented values.
- Gemini functions require the user's own valid API key.
