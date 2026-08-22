# Handoff

Version: VER 0.1.0 — Updated 2026-08-22

## Project goal

A Gemini semantic-vision general CoC operator controlling MuMu Player, with tactical battle reserved for a future RL controller.

## Current status

Phase 0 and the first runnable Windows MVP are complete. The PyQt5 application is built and smoke-tested. It detects MuMu, captures the active instance, controls basic lifecycle actions, imports Village JSON, stores account/knowledge state, and provides Gemini semantic vision/chat/teaching plus Battle Script requirement inspection. Tactical RL and unattended navigation remain incomplete.

## What changed

- Located the recent NB project at `D:\Desktop\NB_COC`.
- Corrected the supplied MyBot location to `D:\Desktop\MyBot-MBR_v8.2.0`.
- Audited NB COC, MyBot-MBR and Simplicity read-only.
- Verified installed MuMuPlayer 6.5.2.0, CLI surface, live instance metadata, ADB, profile and CoC process.
- Measured ADB PNG screenshot baseline: 1600×900, five captures averaging about 380.6 ms.
- Established multi-instance/session/frame/action safety architecture.
- Located the existing GitHub owner/collaborator relationship for future publishing: owner `Hsien0818666`, editor `Mai0313`.
- Created and pushed the private GitHub repository `Hsien0818666/CoC-AI-Controller`; invited `Mai0313` with write access.
- Added runnable desktop UI, MuMu CLI/ADB adapter, SQLite database, Village JSON import, Gemini/DPAPI support, semantic vision/chat/teaching and Battle Script reader.
- Built and smoke-tested `CoC_AI_Controller_VER_0.1.0.exe`.
- Fixed and rebuilt the MuMu CLI Qt-plugin conflict found in user testing; packaged CLI now receives a sanitized environment.
- Made the application observable: rotating log file, live 執行紀錄 panel with a level selector, tracebacks for background failures.
- Moved Gemini onto the `google-genai` SDK with structured output, and every ADB call onto `adbutils` in `adb.py`.
- Made every structured value a Pydantic model in `models.py`, including the database's inputs and outputs.
- Split the package into `adapters/`, `parsers/` and `ui/`, matching the three layers the architecture already described.
- Streamed the AI replies, rendered them as Markdown, and put the run log through `rich` in both the panel and stderr.
- Pointed capture and input at the display MuMu opens the game on, instead of display 0, which holds the emulator's own launcher.
- Moved storage from `%LOCALAPPDATA%\CoC_AI_Controller\` to `~/.coc_ai\`.
- Walked the AI Village JSON import through 設定 → 更多設定 to the export row, and filled `id_registry` from the community `cocMapping.json` gist so imported entities carry names.

## Important files

`src/coc_ai_controller/ui/main_window.py`, `ui/render.py`, `ui/workers.py`, `adapters/mumu.py`, `adapters/adb.py`, `adapters/ai.py`, `adapters/database.py`, `parsers/village.py`, `parsers/battle.py`, `models.py`, `logging_setup.py`, `battle_scripts/`, `tests/`, `scripts/build.ps1` and [Architecture](architecture.md).

For an emulator or runtime review, start from `src/coc_ai_controller/adapters/mumu.py`, `src/coc_ai_controller/ui/main_window.py` and [Database Schema](database-schema.md).

## Architecture

Responsive PyQt5 UI with background workers; MuMu CLI plus per-instance ADB behind `MuMuAdapter`; typed emulator/frame identity; SQLite repositories; Gemini provider; semantic AI vision primary; reserved RL handoff boundary.

## Database

SQLite schema 1 covers metadata, shared ID registry/entity levels, account snapshots/entities, unknown queue and knowledge. Seed ID mappings are present. Unverified per-level cost/time data remains empty.

## Emulator

No emulator settings were changed. `MuMuAdapter` enumerates instance identity, ADB, PID/HWND, resolution/DPI and CoC state, and supports connect/capture, CoC launch/restart, Back and instance launch/restart/close.

## AI agent

Versioned Agent Profile, Gemini provider, semantic screenshot analysis, contextual chat and USER_CONFIRMED teaching storage are implemented. AI-proposed autonomous actions are not executed yet; RL battle tactics remain out of scope.

## Known problems

- `_apply_agent_action` converts Gemini's percentages with `x_pct * 16, y_pct * 9`, assuming a 1600×900 screen; the sampled instance captures at 1920×1080, so AI taps land short of their target.
- `ui_elements()` and `screen_geometry()` still read display 0, which under MuMu is the launcher rather than the game.
- ADB `wm size` reports 900×1600 while the rotated screenshot is 1600×900; the transform must use observed orientation/frame dimensions.
- CLI mutation/lifecycle failure behavior and HWND stability have not been integration-tested.
- ADB PNG capture averages about 381 ms on the sampled system; faster paths require benchmarking.
- The first package exposed a Qt plugin conflict when mumu-cli inherited PyQt variables; source and rebuilt package now remove those variables before each CLI subprocess.
- Simplicity source is mostly unavailable; reuse confidence is low.
- Any MyBot code reuse has GPL implications; the current plan is concepts and reference only.
- `bundle_root()` resolves to `src/` when the application runs from source, so 載入範例 and the automation script picker find no Battle Script until the EXE is packaged, where PyInstaller copies `battle_scripts/` into the bundle. Not yet fixed.
- Verified source-backed master level/cost/time data and GemCostCalculator rules are incomplete; the UI does not invent values.
- Autonomous Battle Preparation and navigation through Enemy Preview are not unattended-ready.
- Gemini features require the user's own valid API key.

## Not finished

Master-data research, verified gem calculation, frame-guarded autonomous action execution, full navigation/Battle Preparation and Enemy Preview handoff remain unfinished.

The GitHub repository exists and is private. `Mai0313` has been invited with write access but must accept the invitation before collaborator access becomes active.

## Next recommended work

Populate sourced master data and gem rules, then implement guarded semantic actions and post-action verification one navigation flow at a time.

## Important decisions

See [Decisions](decisions.md). Most important: use MuMu CLI as the primary host integration; never use a global window; bind frame/action/session identity; preserve the current MuMu profile; do not make template matching primary; do not copy legacy code or assets without provenance and license review.

## Test status

Passed: 2 core unit tests; Python compile; MuMu version/enumeration/ADB/CoC/screenshot live probe; PyInstaller build; packaged EXE smoke test with the correct main-window title. Gemini connection was not tested because no user API key was supplied. Destructive lifecycle buttons were not invoked during automated tests.

## Build

Built `CoC_AI_Controller_VER_0.1.0.exe` in the complete PyInstaller onedir folder. EXE FileVersion is 0.1.0.0 and ProductVersion is 0.1.0.
