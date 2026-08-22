# Architecture Decisions

Updated: 2026-08-22

## ADR-001 — MuMu CLI is the primary host integration

Status: Accepted for implementation validation.

Use the installed `mumu-cli.exe` for instance discovery and lifecycle because it exposes structured instance state, endpoints, PID and window handles plus multi-instance operations. Direct config parsing is diagnostic fallback, not the primary control API.

## ADR-002 — Session identity is generation-scoped

Status: Accepted.

All work targets an immutable tuple of emulator ID, instance identity, ADB endpoint, verified HWNDs and session generation. A restart/rebind invalidates outstanding observations and actions. This prevents cross-instance and late-response actions.

## ADR-003 — ADB PNG is the initial capture path

Status: Accepted with benchmark gate.

It is verified on the installed MuMu and returns 1600×900. The measured ~381 ms average is a baseline, not a performance target. Faster raw/native/HWND paths may be added behind a capture interface after correctness and resource tests.

## ADR-004 — Semantic vision is primary

Status: Accepted.

Gemini receives current-frame and structured account/knowledge context. Templates, pixels and OCR are bounded deterministic evidence for fixed UI and verification only.

## ADR-005 — Latest-frame-wins with single-writer actions

Status: Accepted.

Each session has a replaceable capacity-one observation slot and a serialized action executor. This avoids stale backlogs and concurrent input races.

## ADR-006 — Do not apply an emulator profile automatically

Status: Accepted.

The current machine is already 1600×900/240 DPI, Vulkan, 6 CPU/12 GB with an observed 72 FPS limit. Preserve it until reproducible 1600×900 versus 1280×720 measurements cover UI responsiveness, capture/vision, CPU/GPU/VRAM and multi-instance behavior.

## ADR-007 — Legacy projects are references, not dependencies

Status: Accepted.

NB COC concepts may be adapted with provenance review. MyBot is GPL and global-state-heavy, and Simplicity is mostly compiled; neither is copied into the project. Visual assets require separate license and freshness review.

## ADR-008 — Build/UI technology remains open after Phase 0

Status: Deferred.

NB COC demonstrates Python/PyQt/PyInstaller feasibility, but framework choice should follow a minimal spike covering async workers, secure Windows secrets, packaging/file-version metadata and testability. Phase 1 will record the final choice.

## ADR-009 — Use a separate GitHub repository and preserve the existing collaborator

Status: Implemented for repository setup.

The project uses the dedicated private repository `Hsien0818666/CoC-AI-Controller`, separate from `Hsien0818666/nb-coc`. Collaborator `Mai0313` has been invited with write access. A release is incomplete until its reviewed commit/tag is pushed. The repository must remain private unless the user explicitly changes this decision.
