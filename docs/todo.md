# CoC AI Controller TODO

Updated: 2026-08-22

## Phase 0 closeout

- [x] Locate and read the three reference projects without modifying them.
- [x] Identify installed MuMu version/path and current live profile.
- [x] Verify CLI enumeration, ADB endpoint, Android readiness, CoC process and screenshot dimensions.
- [x] Establish reuse/adapt/reference-only/reject classifications.
- [ ] Capture pre/post file hashes or a source-control status snapshot where reference repositories support it; Phase 0 used read-only commands and made no writes.

## Phase 1 — Project skeleton

- [ ] Select Windows UI/runtime stack using a tested async/packaging spike.
- [ ] Create modules for domain, application, infrastructure and UI with dependency direction enforced.
- [ ] Define typed IDs/contracts for emulator session, frame, observation, semantic action and outcome.
- [ ] Add tests, lint/type checks, structured logging and CI/build entry point.
- [x] Initialize a dedicated Git repository with `.gitignore` and a source/documentation-only initial commit.
- [x] Create private `Hsien0818666/CoC-AI-Controller` and send `Mai0313` a write-access invitation.
- [ ] Confirm that `Mai0313` accepted the GitHub invitation before relying on collaborator access.
- [ ] Add automated secret scanning before application code or runtime data is introduced.

## Phase 2 — Version/logging/handoff

- [ ] Add canonical `VERSION`, updated-date metadata, CHANGELOG and dev-session log.
- [ ] Generate About/build/handoff metadata from one source.
- [ ] Add `docs/chatgpt-handoff.md` and versioned AI handoff generator.
- [ ] Add release workflow: test → metadata/handoff sync → build → verify → commit/tag → GitHub push → GitHub Release artifact.

## Phase 3 onward

- [ ] Database boundaries/schema/migrations and repositories.
- [ ] Seed ID registry with source/provenance validation.
- [ ] Research and load sourced master data and version diffs.
- [ ] Research/version GemCostCalculator rules.
- [ ] Tolerant Village JSON parser and per-account progress joins.
- [ ] Implement/test MuMuAdapter and lifecycle manager.
- [ ] Benchmark capture methods and resolution/profile options.
- [ ] Implement provider interface, Gemini, secure secret storage and async test connection.
- [ ] Versioned agent profile, chat/teaching/discovery and semantic vision.
- [ ] Verified general navigation, Battle Script preparation and Enemy Preview handoff.
- [ ] Reserve RL port only; do not implement battle tactics in V1.

## MuMu research backlog

- [ ] Validate CLI app launch/close and instance restart on a controlled test run.
- [ ] Verify HWND ownership/stability across restart, hide/show, tabs and multi-instance.
- [ ] Test ADB disconnect/offline recovery and concurrent per-instance server behavior.
- [ ] Benchmark ADB PNG, raw framebuffer/streaming and HWND/native capture for latency/resources.
- [ ] Test coordinate transforms across rotation, window resizing, DPI scaling and mixed monitors.
- [ ] Inspect read-only CLI settings schema; only later design an explicit recommended-profile action.
