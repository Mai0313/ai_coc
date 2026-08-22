# Project

CoC AI Controller

# Current Version

VER 0.1.0

# Updated

2026-08-22

# Current Status

Phase 0 and the first runnable Windows MVP are complete. The PyQt5 application is built and smoke-tested. It detects MuMu, captures the active instance, controls basic lifecycle actions, imports Village JSON, stores account/knowledge state, and provides Gemini semantic vision/chat/teaching plus Battle Script requirement inspection. Tactical RL and unattended navigation remain incomplete.

# What Changed

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

# Files Changed

- `docs/reference-audit.md`
- `docs/architecture.md`
- `docs/requirements.md`
- `docs/decisions.md`
- `docs/todo.md`
- `HANDOFF.md`
- `app.py`, `coc_ai_controller/`, `battle_scripts/`, `tests/`, `scripts/build.ps1`
- `VERSION`, `CHANGELOG.md`, `AI_HANDOFF_VER_0.1.0.md`

# Architecture Changes

Implemented initial architecture: responsive PyQt5 UI with background workers; MuMu CLI + per-instance ADB behind `MuMuAdapter`; typed emulator/frame identity; SQLite repositories; Gemini provider; semantic AI vision primary; reserved RL handoff boundary.

# Database Changes

SQLite schema 1 added for metadata, shared ID registry/entity levels, account snapshots/entities, unknown queue and knowledge. Seed ID mappings are present. Unverified per-level cost/time data remains empty.

# Emulator Changes

No emulator settings were changed. `MuMuAdapter` now enumerates instance identity, ADB, PID/HWND, resolution/DPI and CoC state and supports connect/capture, CoC launch/restart, Back and instance launch/restart/close.

# AI Agent Changes

Versioned Agent Profile, Gemini provider, semantic screenshot analysis, contextual chat and USER_CONFIRMED teaching storage are implemented. AI-proposed autonomous actions are not executed yet; RL battle tactics remain out of scope.

# Known Problems

- ADB `wm size` reports 900×1600 while rotated screenshot is 1600×900; transform must use observed orientation/frame dimensions.
- CLI mutation/lifecycle failure behavior and HWND stability have not been integration-tested.
- ADB PNG capture averages ~381 ms on the sampled system; faster paths require benchmarking.
- The first package exposed a Qt plugin conflict when mumu-cli inherited PyQt variables; source and rebuilt package now remove those variables before each CLI subprocess.
- Simplicity source is mostly unavailable; reuse confidence is low.
- Any MyBot code reuse has GPL implications; current plan is concepts/reference only.
- Verified source-backed master level/cost/time data and GemCostCalculator rules are incomplete; the UI does not invent values.
- Autonomous Battle Preparation/navigation through Enemy Preview is not unattended-ready.

# Not Finished

Master-data research, verified gem calculation, frame-guarded autonomous action execution, full navigation/Battle Preparation and Enemy Preview handoff remain unfinished.

The GitHub repository exists and is private. `Mai0313` has been invited with write access but must accept the invitation before collaborator access becomes active. Automated secret scanning is not configured yet.

# Next Recommended Work

Populate sourced master data/gem rules, then implement guarded semantic actions and post-action verification one navigation flow at a time.

# Important Decisions

See `docs/decisions.md`. Most important: use MuMu CLI as primary host integration; never use a global window; bind frame/action/session identity; preserve the current MuMu profile; do not make template matching primary; do not copy legacy code/assets without provenance/license review.

# Test Status

Passed: 2 core unit tests; Python compile; MuMu version/enumeration/ADB/CoC/screenshot live probe; PyInstaller build; packaged EXE smoke test with correct main-window title. Gemini connection was not tested because no user API key was supplied. Destructive lifecycle buttons were not invoked during automated tests.

# Build

Built: `CoC_AI_Controller_VER_0.1.0.exe` in the complete PyInstaller onedir folder. EXE FileVersion is 0.1.0.0 and ProductVersion is 0.1.0.
