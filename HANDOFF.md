# Project

CoC AI Controller

# Current Version

Target VER 0.1.0; Phase 0 documentation baseline. No application build exists yet.

# Updated

2026-08-22

# Current Status

Phase 0 reference and local MuMu audit is complete. Architecture, requirements, decisions and implementation backlog are documented. No RL or product source code has been started.

# What Changed

- Located the recent NB project at `D:\Desktop\NB_COC`.
- Corrected the supplied MyBot location to `D:\Desktop\MyBot-MBR_v8.2.0`.
- Audited NB COC, MyBot-MBR and Simplicity read-only.
- Verified installed MuMuPlayer 6.5.2.0, CLI surface, live instance metadata, ADB, profile and CoC process.
- Measured ADB PNG screenshot baseline: 1600×900, five captures averaging about 380.6 ms.
- Established multi-instance/session/frame/action safety architecture.
- Located the existing GitHub owner/collaborator relationship for future publishing: owner `Hsien0818666`, editor `Mai0313`.
- Created and pushed the private GitHub repository `Hsien0818666/CoC-AI-Controller`; invited `Mai0313` with write access.

# Files Changed

- `docs/reference-audit.md`
- `docs/architecture.md`
- `docs/requirements.md`
- `docs/decisions.md`
- `docs/todo.md`
- `HANDOFF.md`

# Architecture Changes

Initial architecture adopted: MuMu CLI + per-instance ADB behind `MuMuAdapter`; immutable generation-scoped `EmulatorSession`; latest-frame-wins observations; asynchronous workers; single action writer per session; semantic AI vision primary; deterministic vision fallback; reserved RL handoff boundary.

# Database Changes

None. Only logical separation of shared master, account, knowledge, runtime and secret storage is specified. Schema work begins in Phase 3.

# Emulator Changes

No emulator settings were changed. Current observed instance 0 is Android 15, 1600×900/240 DPI, Vulkan, 6 CPU/12 GB, ADB `127.0.0.1:16384`. MuMu CLI supplies PID plus main/render HWNDs and supports multi-instance lifecycle operations.

# AI Agent Changes

No agent implementation exists. Responsibilities and semantic-action safety boundary are documented. RL battle tactics remain out of scope.

# Known Problems

- ADB `wm size` reports 900×1600 while rotated screenshot is 1600×900; transform must use observed orientation/frame dimensions.
- CLI mutation/lifecycle failure behavior and HWND stability have not been integration-tested.
- ADB PNG capture averages ~381 ms on the sampled system; faster paths require benchmarking.
- Simplicity source is mostly unavailable; reuse confidence is low.
- Any MyBot code reuse has GPL implications; current plan is concepts/reference only.

# Not Finished

All implementation phases (skeleton through Enemy Preview handoff), data research, tests, executable build, CHANGELOG/version automation, dev-session log and single-file versioned AI handoff remain unfinished.

The GitHub repository exists and is private. `Mai0313` has been invited with write access but must accept the invitation before collaborator access becomes active. Automated secret scanning is not configured yet.

# Next Recommended Work

Start Phase 1 with a small Windows stack spike. Prove non-blocking UI, per-session cancellation, secure secret storage and versioned packaging. Then define contracts and tests before implementing MuMu behavior.

# Important Decisions

See `docs/decisions.md`. Most important: use MuMu CLI as primary host integration; never use a global window; bind frame/action/session identity; preserve the current MuMu profile; do not make template matching primary; do not copy legacy code/assets without provenance/license review.

# Test Status

Passed manual read-only probes: MuMu version, `info --vmindex all`, ADB connect/get-state, live CoC PID, resolution/DPI and five valid screenshots. No automated project tests exist yet. Destructive lifecycle commands were not tested in Phase 0.

# Build

No EXE built. Reserved target name: `CoC_AI_Controller_VER_0.1.0.exe`.
