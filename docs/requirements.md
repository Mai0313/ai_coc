# CoC AI Controller Requirements

Target: VER 0.1.0
Updated: 2026-08-22

## Product requirements

- Provide a Windows desktop AI-agent platform for general Clash of Clans operation.
- Use Google Gemini semantic vision/understanding through a provider interface in V1.
- Keep real-time battle tactics outside the AI operator and reserve an RL handoff interface.
- Import tolerant Village JSON and join account state to a sourced shared master database.
- Support chat, teaching, discovery and provenance-aware knowledge states.
- Prepare declared Battle Script requirements before attack/matchmaking and hand off at Enemy Preview.

## Emulator requirements

- MuMu is the V1 primary emulator; adapter and lifecycle logic must not leak into Agent Core.
- Enumerate instances through stable instance identity and show running/offline, account binding, ADB, resolution, DPI, CoC and current screen.
- Bind every session by instance ID, ADB serial/endpoint and verified HWND(s); never by desktop position.
- Launch/wait/connect/start CoC without requiring the user to pre-open MuMu.
- Support screenshot/region, tap, swipe, Back, resolution/DPI/window inspection, CoC restart, instance restart/close and bounded recovery.
- Read current profile first; do not alter resolution, DPI, FPS, renderer, CPU or RAM automatically.
- Design all contracts for multiple simultaneous sessions even when V1 tests one instance.

## Safety and correctness requirements

- Every frame has frame ID, timestamp, emulator ID and account tag/session generation.
- Every AI action response echoes emulator ID and frame ID/session generation.
- Before each action, verify target binding, frame freshness, current state/precondition and active account.
- Only one action writer operates per emulator session; cancellation stops queued side effects.
- Latest frame wins; observation queues cannot grow without bound.
- Unknown JSON fields cannot crash import. Unknown IDs enter `UnknownEntityQueue`.
- AI guesses cannot become VERIFIED without independent verification; user confirmation becomes USER_CONFIRMED.
- API keys are never hardcoded, committed, logged or stored in domain databases/documents.

## Performance requirements

- UI never blocks on Gemini, capture, ADB or database I/O.
- Workers have explicit timeouts, cancellation and bounded queues.
- Capture/vision rates are demand-driven and measured per instance.
- Cache immutable master data and relevant account snapshots; invalidate by version/sync.
- Record latency/error metrics without screenshot contents or secrets by default.

## Data quality requirements

- Important master-data facts retain source, URL, game version, retrieval time and verification status.
- Current CoC values and gem conversion rules must be researched from reliable sources, never filled from model memory.
- Account data and shared master data remain separate.
- Known entity changes support version diff as well as unknown-ID discovery.

## GitHub collaboration requirements

- Maintain this as a repository separate from the existing `Hsien0818666/nb-coc` project.
- At each completed release, run tests, update handoff/version artifacts, commit the reviewed source, and push the release branch/tag to GitHub.
- Grant edit access to the previously identified collaborator `Mai0313` after repository creation.
- Keep API keys, ADB keys, private account JSON, personal screenshots, logs containing identifiers, build caches and local emulator configuration out of Git history through `.gitignore`, redaction checks and pre-push review.
- Do not publish binaries or large visual datasets directly in Git history; use a GitHub Release or approved artifact storage.
- Repository visibility must be explicitly chosen before creation. Default recommendation is private while account-linked screenshots/data may exist.

## V1 acceptance baseline

The first usable build must meet the success criteria in the master specification: versioned EXE/About; secure Gemini settings/test; MuMu discovery/selection/ADB/capture/CoC launch; Village JSON/account progress/master join/gem calculation; chat/teaching/discovery/semantic screen and action with verification; Battle Script preparation through Enemy Preview; and current handoff/version documents.

Phase 0 acceptance is narrower: complete the reference/MuMu audit, record constraints and unknowns, and establish requirements/architecture/decisions/todo/handoff without modifying reference projects.
