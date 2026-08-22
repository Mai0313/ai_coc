# CoC AI Controller Architecture

Version target: VER 0.1.0
Updated: 2026-08-22
Status: Phase 0 architecture baseline

## Responsibility boundary

`CoCAgent` is the general operator: understanding, navigation, teaching/discovery, account-aware preparation, matchmaking, result handling and verification. `BattleControllerPort` is a reserved handoff boundary. Tactical deployment and real-time battle decisions are outside V1.

## Component model

```text
Desktop UI
  ├─ Emulator page / active-target selection
  ├─ Account progress / master-data views
  ├─ AI chat / teaching
  └─ Settings / About
          │ commands + events (never blocking UI)
Application Services
  ├─ EmulatorLifecycleManager
  ├─ AgentRuntime
  ├─ ObservationCoordinator (latest frame wins)
  ├─ ActionController (identity + freshness guard)
  ├─ DiscoveryService
  └─ AccountSyncService
          │ ports
Infrastructure
  ├─ MuMuAdapter ── mumu-cli lifecycle + Win32 HWND verification
  ├─ AdbController ── adbutils, one per instance port (capture, input, uiautomator)
  ├─ GeminiClient ── google-genai Interactions API, structured output
  ├─ SQLite repositories
  └─ SecureSecretStore (Windows protected storage)
```

Every value crossing these boundaries is a Pydantic model declared in `models.py`.

## Core identities and contracts

`EmulatorDescriptor` contains `emulator_id`, provider type, instance index/name, Android version, process state, ADB host/port, PID, main HWND and render HWND.

`EmulatorSession` is an immutable binding of `emulator_id + instance identity + adb_serial + verified HWNDs + account_tag + session_generation`. Restart or rebinding creates a new generation so delayed work cannot target a replacement process.

`Frame` contains `frame_id`, monotonic sequence, capture timestamp, emulator ID, session generation, account tag, pixel dimensions, orientation and image payload/reference.

`SemanticActionPlan` contains emulator ID, session generation, source frame ID, screen/state hypothesis, ordered typed actions, expected postconditions, expiry and confidence. The Action Controller revalidates identity, generation, freshness and preconditions before every side effect.

## Runtime concurrency

- UI thread only renders state and submits commands.
- Emulator/ADB workers are serialized per emulator session; different sessions may run concurrently.
- Capture produces into a capacity-one replaceable slot. When analysis is busy, intermediate frames are discarded.
- Vision/AI requests are asynchronous and cancellable. Results for inactive generation/frame are recorded as stale and never executed.
- Action execution is single-writer per session.
- Database writes are serialized and transactional; reads use immutable snapshots/caches.
- State Manager owns transitions and publishes events to UI and workers.

## Lifecycle state machine

```text
OFFLINE → LAUNCHING → ANDROID_WAIT → ADB_READY → COC_STARTING → COC_READY → AGENT_READY
   ↑          │             │             │             │             │
   └──────────┴──── bounded recovery / shutdown / operator-required ──┘
```

Recovery escalation is reconnect ADB, restart CoC, restart the selected instance, then `NEEDS_ATTENTION`. Every stage has timeout, retry budget and cancellation. Shutdown stops agent/actions, persists runtime state, flushes DB, then follows keep-open/close policy.

## Vision architecture

Primary recognition is semantic AI vision supplied with the current screenshot, account snapshot, relevant master data, agent profile and task context. Deterministic templates/pixels/OCR are optional capabilities for inexpensive fixed-UI checks, post-action verification and fallback. Their result is evidence, not the primary world model.

## Data boundaries

- Shared master DB: entity identity, levels, costs, time, requirements and source/version evidence.
- Account DB: player-tag-specific snapshots, levels, counts, timers, binding and sync history.
- Knowledge DB: UNKNOWN/CANDIDATE/VERIFIED/USER_CONFIRMED claims, visual samples and provenance.
- Runtime store: emulator sessions, frames, tasks, action outcomes and recovery state.
- Secret store: API keys only; never logs, profiles, handoff files or master DB.

Detailed schemas are deferred to Phase 3, but repository interfaces must preserve these boundaries.

## MuMu adapter strategy

Discovery order: uninstall registry/install paths → validate `mumu-cli.exe` → `version` → `info --vmindex all` → parse descriptors → verify PID/HWND ownership → connect the CLI-reported ADB endpoint → probe Android/package/capture capabilities.

Lifecycle uses MuMu CLI when supported. Android checks and screenshot/input use an instance-scoped ADB client. Window APIs are optional acceleration/fallback behind capabilities. The adapter never owns agent decisions.

## Packaging/versioning

The eventual Windows build name is `CoC_AI_Controller_VER_0.1.0.exe`. `VERSION`, About metadata, executable file version, CHANGELOG and handoff version must come from one build-time version source. Phase 0 produces documents only; no executable is claimed.
