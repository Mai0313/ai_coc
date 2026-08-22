# CoC AI Controller — AI Handoff VER 0.1.0

Updated: 2026-08-22

Project goal: a Gemini semantic-vision general CoC operator controlling MuMu, with tactical battle reserved for future RL.

Architecture: PyQt5 non-blocking desktop UI; per-instance MuMu CLI/ADB adapter; immutable emulator/frame identities; SQLite account/master/knowledge separation; Gemini provider; DPAPI secrets.

Completed: Phase 0 audit; MuMu discovery/listing; screenshot and lifecycle controls; Village JSON import and registry join; unknown queue; AI settings/test/chat/vision/teaching; Battle Script requirements; About/version; Windows build workflow.

Important files: `coc_ai_controller/app.py`, `mumu.py`, `ai.py`, `database.py`, `village.py`, `battle.py`, `HANDOFF.md`, `docs/architecture.md`.

DB summary: metadata, ID registry, entity levels, account snapshots/entities, unknown queue and knowledge. Seed mappings exist; unverified level/cost/time fields remain empty.

Known issues: autonomous navigation/preparation and verified gem/master datasets are incomplete; Gemini needs a user API key.

Next: source/verify master data and gem rules, then implement frame-guarded semantic actions and verified navigation through Enemy Preview.

Build file: `CoC_AI_Controller_VER_0.1.0.exe` inside its onedir release folder.

Recommended Attachments: `coc_ai_controller/mumu.py`, `coc_ai_controller/app.py`, `docs/database-schema.md` for emulator/runtime review.
