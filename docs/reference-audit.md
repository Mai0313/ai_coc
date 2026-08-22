# CoC AI Controller — Reference Audit

Updated: 2026-08-22
Scope: Phase 0, read-only inspection

## Audit boundaries

The following projects were inspected without editing them:

- `D:\Desktop\Simplicity v3.50.0`
- `D:\Desktop\MyBot-MBR_v8.2.0` (the specification named `MyBot-MBR\_v8.2.0`; that path does not exist)
- `D:\Desktop\NB_COC` (identified as the recent NB COC project)

This audit does not approve copying code or assets. Licensing and provenance must be checked before reuse. MyBot declares GPL licensing; treat its implementation as reference-only unless the new project's license is made compatible.

## Executive findings

MuMuPlayer 6.5.2.0 is installed at `C:\Program Files\Netease\MuMuPlayer`. Its bundled `nx_main\mumu-cli.exe` is the best primary integration point discovered. It provides structured JSON instance information and commands for create/clone/delete, launch/shutdown/restart, app control, settings, ADB, window layout, and the main application.

The running instance is index `0`, Android 15, name `Android Device`, PID `33180`, state `start_finished`, with ADB `127.0.0.1:16384`. CLI output supplies `main_wnd=00030454` and `render_wnd=0004048A`; these should be parsed as hexadecimal HWND values and verified against the process before use.

The live profile is 1600×900 at 240 DPI. ADB `wm size` reports the unrotated physical panel as 900×1600, while an actual `exec-out screencap -p` PNG is 1600×900. Code must derive action coordinates from the captured frame/current rotation, not assume `wm size` ordering.

Five sequential PNG captures measured 360–411 ms, average 380.6 ms, and about 1.7 MB for the sampled frame. This is adequate for operator observations but motivates latest-frame-wins, bounded queues, cancellation, and later benchmarking of raw/streaming or MuMu-native capture paths.

## MuMu host baseline

| Item               | Observed value                                         | Evidence / note                                                                                           |
| ------------------ | ------------------------------------------------------ | --------------------------------------------------------------------------------------------------------- |
| Product/version    | MuMuPlayer 6.5.2.0                                     | Windows uninstall registry and `mumu-cli version`                                                         |
| Install root       | `C:\Program Files\Netease\MuMuPlayer`                  | Registry                                                                                                  |
| CLI                | `nx_main\mumu-cli.exe`                                 | Local executable help                                                                                     |
| Bundled ADB        | `nx_main\adb.exe`, `nx_device\15.0\shell\adb.exe`      | Install tree                                                                                              |
| Instance           | index 0, Android 15, `start_finished`                  | `mumu-cli info --vmindex all`                                                                             |
| Stable endpoint    | `127.0.0.1:16384`                                      | CLI info and `vm_config.json`                                                                             |
| Windows identity   | PID 33180; main/render HWND supplied by CLI            | CLI info                                                                                                  |
| Resolution/DPI     | 1600×900 / 240                                         | MuMu configs and captured PNG                                                                             |
| Performance preset | 6 CPU, 12 GB RAM, Vulkan                               | `customer_config.json`, `vm_config.json`, `shell_config.json`                                             |
| FPS                | actual limit 72; desired/real setting also records 144 | Do not modify until benchmarked                                                                           |
| Mouse              | system mouse enabled                                   | Current config; specification's “custom pointer enabled” wording should be reconciled with UI terminology |
| ADB mode           | local connection                                       | Current config                                                                                            |
| CoC package        | `com.supercell.clashofclans`, running PID 2604         | Live ADB `pidof`                                                                                          |

### Verified CLI capability surface

- `info --vmindex all`: enumerate instances and obtain state, PID, ADB endpoint and HWNDs.
- `control --vmindex … launch|shutdown|restart`: lifecycle operations.
- `control --vmindex … app install|uninstall|launch|close|info`: application lifecycle.
- `setting`: query/change instance settings; Phase 0 used read-only config inspection only.
- `adb` and `sh`: instance-scoped Android commands.
- `create`, `clone`, `delete`, `rename`, `import`, `export`: multi-instance management.
- `sort` and `control … layout_window`: window management.

Destructive and mutating CLI operations were intentionally not exercised. Exact exit codes, timeout behavior, app-control syntax, and behavior during failed boot remain Phase 9/10 integration tests.

## Reference classifications

### NB COC

| Area                           | Classification     | Finding                                                                                                                                                                                                         |
| ------------------------------ | ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Bounded ADB subprocess wrapper | ADAPT              | `ADBClient` has explicit timeout/retry controls, device scoping, error decoding, package lifecycle, screenshot validation, tap/swipe/back and coordinate scaling. Generalize it to per-session async execution. |
| Screenshot validation          | REUSE concept      | Validates PNG signature and retries. Add frame metadata, cancellation, latest-frame slot and latency metrics.                                                                                                   |
| Emulator launcher              | ADAPT              | LDPlayer discovery, CLI launch, Android readiness, unlock, resolution/DPI validation and CoC launch form a useful lifecycle sequence. Replace LD-specific assumptions with `MuMuAdapter`.                       |
| Recovery                       | ADAPT              | Bounded boot wait and package restart are useful. Turn recovery into explicit policies/state transitions rather than UI-thread sleeps.                                                                          |
| Workflow JSON and conditions   | REFERENCE_ONLY     | Useful examples of declarative navigation and verification, but schemas are tied to fixed coordinates/templates.                                                                                                |
| Vision/templates/OCR           | REFERENCE_ONLY     | Useful deterministic fallback and test corpus; must not become primary semantic vision.                                                                                                                         |
| GUI threading                  | REFERENCE_ONLY     | Existing PyQt design shows packaging and UI patterns, but the new app needs explicit AI/Vision/ADB/Action/DB workers.                                                                                           |
| Battle execution               | REJECT for V1 core | It mixes scripted battle actions into the bot. The new boundary hands combat to future RL.                                                                                                                      |

### MyBot-MBR v8.2.0

| Area                           | Classification    | Finding                                                                                                                                                                                              |
| ------------------------------ | ----------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Emulator abstraction           | REFERENCE_ONLY    | Central Android configuration supports emulator/instance/title/control/size/device/capability flags. Strong evidence for an adapter boundary, but it is global-state-heavy AutoIt/GPL code.          |
| Persistent ADB shell/minitouch | REFERENCE_ONLY    | Tracks long-lived processes and capture/click timing, supports ADB raw screencap and input. Benchmark similar techniques independently.                                                              |
| Window/control binding         | REFERENCE_ONLY    | Separates top-level HWND and render control, updates offsets, and supports background WinAPI input. The new system should bind instance ID + ADB endpoint + main/render HWND without global handles. |
| Capture modes                  | REFERENCE_ONLY    | WinAPI background capture plus ADB fallback and region crop are mature ideas. Preserve capability negotiation; do not copy implementation.                                                           |
| Input fallback                 | REFERENCE_ONLY    | ADB tap/swipe preferred with PostMessage/ControlClick fallbacks. New actions need pre/post verification and frame guards.                                                                            |
| Recovery/state checks          | ADAPT concept     | Boot wait, CoC close/open, obstacle checks, bounded main-screen waits, emulator restart escalation are valuable patterns.                                                                            |
| Navigation/assets              | REFERENCE_ONLY    | Extensive CoC screen/navigation knowledge is useful for test scenarios and fallback checks; assets may be stale and have licensing constraints.                                                      |
| Global mutable runtime         | REJECT            | Numerous global emulator/window/action variables are unsafe for future multi-instance operation.                                                                                                     |
| Template-first vision          | REJECT as primary | May only be a cheap deterministic check/fallback in the new design.                                                                                                                                  |

### Simplicity v3.50.0

| Area                      | Classification   | Finding                                                                                                   |
| ------------------------- | ---------------- | --------------------------------------------------------------------------------------------------------- |
| Bundled ADB               | REFERENCE_ONLY   | Confirms self-contained deployment pattern. Version/provenance must be controlled in the new build.       |
| Packaged Python/Qt layout | REFERENCE_ONLY   | The distribution appears PyInstaller-based, but most source is unavailable, limiting audit confidence.    |
| AutoHotkey window actions | REFERENCE_ONLY   | Demonstrates separate host-window tooling; not suitable as the core action path.                          |
| Attack script format      | REFERENCE_ONLY   | Human-readable attack declarations may inform Battle Script UX, but V1 does not implement battle control. |
| Templates/debug matches   | REFERENCE_ONLY   | Potential regression corpus after permission/provenance review only.                                      |
| Compiled-only behavior    | REJECT for reuse | Do not reverse engineer or depend on opaque executable internals as core infrastructure.                  |

## Cross-project conclusions

1. Use MuMu's own CLI for discovery/lifecycle and its returned ADB endpoint/HWNDs; use bundled ADB for screenshot/input and Android/package checks.
2. Represent every operation through an immutable `EmulatorSession` identity. Never select by screen position or a single global window.
3. Keep capability negotiation: CLI, ADB, and optional HWND capture/input are distinct capabilities with measured health.
4. Start with verified ADB PNG capture. Investigate faster paths later behind the same interface.
5. Treat legacy navigation/templates as behavioral references and deterministic fallback evidence, not semantic truth.
6. Recovery must be a bounded state machine: reconnect ADB → restart CoC → restart instance → require operator attention.
7. Never execute an action unless emulator ID and frame ID match the current session and observation is still valid.

## Gaps requiring later validation

- CLI app launch/close syntax and return semantics for CoC.
- HWND stability across restart, tabbed/multi-window display, hide/show and instance rename.
- Whether `render_wnd` permits reliable off-screen capture and background input in this MuMu release.
- Raw or native screenshot throughput versus PNG ADB capture, including CPU/GPU cost.
- Offline/disconnected ADB recovery, device authorization, concurrent instance throughput and port reuse.
- Window DPI awareness and coordinate conversion on mixed-scale multi-monitor setups.
- Safe read/write keys exposed by `mumu-cli setting`; no recommended profile should be applied before these are tested.
- License/provenance review for any legacy code, data or visual asset considered for inclusion.
