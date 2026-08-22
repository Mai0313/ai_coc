# CoC AI Controller

CoC AI Controller is a Windows AI-agent platform for Clash of Clans. The V1 goal is a Gemini-powered general operator that understands account state and the current MuMu screen, performs verified navigation and battle preparation, and hands control to a future RL battle controller at Enemy Preview.

Current status: VER 0.1.0 Windows MVP built and smoke-tested.

## Run the Windows build

Open the complete `CoC_AI_Controller_VER_0.1.0` release folder and double-click `CoC_AI_Controller_VER_0.1.0.exe`. Do not move the EXE out of its folder; the adjacent `_internal` runtime is required.

On startup the Emulators page detects the installed MuMu asynchronously. Use Refresh, choose an instance, then Connect / Screenshot or Launch CoC. Gemini features require a valid API key saved under Settings; the key is protected locally with Windows DPAPI and is never committed.

## Development

Run `scripts/build.ps1` to execute tests and create the PyInstaller onedir build under `dist`.

Start with:

- `HANDOFF.md` for current project status and next work.
- `docs/reference-audit.md` for the reference-project and local MuMu findings.
- `docs/architecture.md` and `docs/requirements.md` for the approved V1 direction.
- `docs/decisions.md` and `docs/todo.md` for constraints and planned work.

Security: never commit API keys, ADB keys, private account JSON, personal screenshots or local emulator configuration.
