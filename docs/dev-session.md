# Development Session Log

## 2026-08-22 — Capturing the display the game is actually on

Task: 擷取目前畫面 failed with `ADB 截圖失敗 … screencap error`, and nothing in the log said why.

Cause: MuMu Player runs three Android displays (`mumuscreen000/001/002`) and opens the game on one of its own, leaving display 0 on the launcher. With more than one display present, `screencap -p` writes `[Warning] Multiple displays were found, but no display id was specified! …` ahead of the PNG, so the decode fails. adbutils turned that into a bare `screencap error`. The same split hit input: `input tap` without `-d` lands on the launcher, so every tap the agent loop made was going to the wrong screen.

Changed: `AdbController.display_for(package)` reads `dumpsys window displays` for the display whose focused window or focused app belongs to the package — only the topmost display carries `mCurrentFocus`, so anything else grabbing the focus would otherwise hide a game that is running perfectly well — then bridges `dumpsys display`'s two id schemes through the display name they share. `screencap -d` takes the physical id, `input -d` the logical one, so `DisplayTarget` carries both. `screenshot`, `tap`, `swipe` and `back` all take one; `MuMuAdapter` looks it up per call, which costs about 80 ms against a 900 ms capture. The screenshot no longer round-trips through Pillow: `screencap -p` already returns PNG bytes, and the PNG magic is checked so a device message ends up in the error instead of a decode failure.

Also: storage moved from `%LOCALAPPDATA%\CoC_AI_Controller\` to `~/.coc_ai\`, same layout, one line in `data_root()`.

Tests: `uv run pytest` (22 passed). Against the live instance: `display_for` returned logical 2 / physical 4619827767814508545, the capture came back as a real 1920x1080 game frame, and a tap at (580, 631) hit the reconnect button in the game's idle dialog.

Known issues: `_apply_agent_action` still converts Gemini's percentages with `x_pct * 16, y_pct * 9`, which assumes a 1600x900 screen while this instance is 1920x1080, so AI taps land about 17% short of the target. `ui_elements()` and `screen_geometry()` still read display 0.

## 2026-08-22 — Streaming replies, rendered output and a package split

Task: the AI 助手 tab printed one plain-text block after a long silence, the run log was a flat grey wall, several classes still bypassed the Pydantic rule, and the package was twelve flat modules.

Added: `ui/render.py` (Markdown to HTML through `markdown-it-py`, log records to HTML through `rich`), `ui/workers.py` with `StreamWorker`, `GeminiClient.stream`, `MainWindow.run_stream`, and the `ChatTranscript` / `ChatMessage` models behind the chat view. `rich` and `markdown-it-py` joined the runtime dependencies.

Changed: the package is now `adapters/`, `parsers/` and `ui/` with `models.py`, `constants.py`, `logging_setup.py` and a `main()`-only `app.py` at the root. The chat and log panels are `QTextBrowser`. Gemini request bodies, MuMu CLI payloads, Village JSON entries and Battle Scripts are models; `Database`, `SecretStore`, `AdbController`, `MuMuAdapter` and `GeminiClient` are Pydantic models taking keyword arguments. `BattleScript` is the parsed document itself, so `parsers/battle.py` is one `model_validate_json` call.

Removed: the hand-built Gemini request dicts, the `json.dumps` calls on their way into prompts and the database, the `.get()` chains in the Village and Battle Script parsers, and `MuMuAdapter.cli_json`.

Note: a Pydantic field's type must stay importable at runtime, so `runtime-evaluated-base-classes` was added to the Ruff config; without it `TC003` moved `Path` into `if TYPE_CHECKING` and `Database` would not build.

Tests: `uv run pytest` (18 passed, coverage 26%), `pre-commit run -a`, `ty check`. An offscreen `MainWindow` run confirmed the chat renders headings, lists and tables, that a streamed reply grows in place, and that the log panel carries colours, logger names and a rich traceback.

Known issues: the packaged build has not been re-verified since `rich` and `markdown-it-py` joined the dependency set. `bundle_root()` points at `src/` when running from source, so the Battle Script pickers are empty outside the packaged EXE.

Next: verify `scripts/build.ps1` with the new dependencies, then fix `bundle_root()` for source runs.

## 2026-08-22 — Logging, google-genai and Pydantic

Task: the AI Village-JSON import failed with nothing shown anywhere, so make the application observable, then move AI and ADB onto the libraries meant for them.

Added: `logging_setup.py` and the 執行紀錄 panel; `adb.py` with `AdbController` over adbutils; the Settings model picker fed by `GeminiClient.list_text_models`.

Changed: `ai.py` rewritten on `google-genai` (Interactions API, structured output); `mumu.py` delegates all ADB work to `adb.py`; all structured data is now Pydantic, including `Database` inputs and outputs.

Removed: the OpenAI-compatible Gemini path, the hand-rolled `urllib` request/retry code, and the manual ```` ```json ```` stripping.

Tests: `uv run pytest` (3 passed, coverage 18%); an offscreen `MainWindow` smoke run confirmed the log panel fills, adbutils picks up MuMu's own `adb.exe` and instance `127.0.0.1:16480` is enumerated.

Known issues: the packaged build has not been re-verified since google-genai, adbutils and pydantic joined the dependency set. Screenshots and taps still target the emulator's default display, so an app MuMu opened on a secondary display would be captured and tapped incorrectly.

Next: rerun `scripts/build.ps1` and confirm the EXE, then decide whether display selection needs to be explicit.

## 2026-08-22 — VER 0.1.0

Task: Produce the first runnable CoC AI Controller Windows MVP after Phase 0.

Added: PyQt5 application, SQLite account/knowledge store, MuMu CLI/ADB adapter, Gemini provider with DPAPI key storage, semantic vision/chat/teaching, Battle Script reader, tests and PyInstaller build workflow.

Removed: none.

Fixed: ensured SQLite connections close on Windows after tests exposed a locked temporary DB.

Tests: 2 core unit tests passed; live MuMu version/instance/ADB/CoC/screenshot probes passed; packaged EXE stayed running with the correct `CoC AI Controller — VER 0.1.0` main-window title.

Known issues: sourced master level data and verified gem rules are incomplete; automated navigation/preparation is not unattended-ready.

Follow-up fix: user screenshot exposed a packaged mumu-cli Qt plugin conflict. Sanitized `QT_PLUGIN_PATH`, `QT_QPA_PLATFORM`, `QT_QPA_PLATFORM_PLUGIN_PATH` and `QML2_IMPORT_PATH` before MuMu subprocesses; rebuild and source-level polluted-environment probe passed.

Next: expand source-backed master data and guarded semantic actions.
