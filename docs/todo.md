# CoC AI Controller TODO

Updated: 2026-08-22

## Phase 0 closeout

- [x] Locate and read the three reference projects without modifying them.
- [x] Identify installed MuMu version/path and current live profile.
- [x] Verify CLI enumeration, ADB endpoint, Android readiness, CoC process and screenshot dimensions.
- [x] Establish reuse/adapt/reference-only/reject classifications.
- [ ] Capture pre/post file hashes or a source-control status snapshot where reference repositories support it; Phase 0 used read-only commands and made no writes.

## Phase 1 — Project skeleton

- [x] Select PyQt5/Python 3.12/PyInstaller through a tested async/packaging spike.
- [x] Create initial domain, database, emulator, AI and UI modules.
- [x] Define typed emulator instance and frame identities.
- [x] Add unit tests and a reproducible Windows build entry point.
- [x] Initialize a dedicated Git repository with `.gitignore` and a source/documentation-only initial commit.
- [x] Create private `Hsien0818666/CoC-AI-Controller` and send `Mai0313` a write-access invitation.
- [ ] Confirm that `Mai0313` accepted the GitHub invitation before relying on collaborator access.
- [ ] Add automated secret scanning before application code or runtime data is introduced.

## Phase 2 — Version/logging/handoff

- [x] Add canonical `VERSION`, updated-date metadata, CHANGELOG and dev-session log.
- [x] Show synchronized version/schema/master/profile metadata in About and EXE file metadata.
- [x] Add `docs/chatgpt-handoff.md` and a versioned AI handoff document.
- [ ] Add release workflow: test → metadata/handoff sync → build → verify → commit/tag → GitHub push → GitHub Release artifact.

## Phase 3 onward

- [x] Initial SQLite boundaries/schema and repositories.
- [ ] Seed ID registry with source/provenance validation.
- [ ] Research and load sourced master data and version diffs.
- [ ] Research/version GemCostCalculator rules.
- [x] Tolerant Village JSON parser and per-account registry joins.
- [x] Implement/test initial MuMuAdapter and lifecycle controls.
- [ ] Benchmark capture methods and resolution/profile options.
- [x] Implement provider interface, Gemini, DPAPI secret storage and async test connection.
- [x] Versioned agent profile, chat/USER_CONFIRMED teaching and semantic vision MVP.
- [x] Load and validate Battle Script requirements with reserved RL handoff.
- [ ] Populate source-verified per-level master data and gem rules.
- [ ] Implement safe autonomous navigation and Battle Preparation through Enemy Preview.
- [ ] Verified general navigation, Battle Script preparation and Enemy Preview handoff.
- [ ] Reserve RL port only; do not implement battle tactics in V1.

## MuMu research backlog

- [ ] Validate CLI app launch/close and instance restart on a controlled test run.
- [ ] Verify HWND ownership/stability across restart, hide/show, tabs and multi-instance.
- [ ] Test ADB disconnect/offline recovery and concurrent per-instance server behavior.
- [ ] Benchmark ADB PNG, raw framebuffer/streaming and HWND/native capture for latency/resources.
- [ ] Test coordinate transforms across rotation, window resizing, DPI scaling and mixed monitors.
- [ ] Inspect read-only CLI settings schema; only later design an explicit recommended-profile action.
