# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Windows-only PyQt5 desktop application that drives Clash of Clans running inside MuMu Player 12. Gemini does the semantic screen reading; the app turns its answers into ADB taps and verifies the outcome on the next screenshot. Live battle tactics are deliberately out of scope: `battle.py` validates army requirements and stops at a reserved `RESERVED_RL` handoff boundary.

## Development flow

Read `gh-dev-flow` before starting; it owns the path from a task landing to the change being merged. In this repo that path always ends the same way: open the PR as a draft, run `code-review` over the branch and fix what holds up, then merge once CI is green. Merging on green needs no further approval.

## Commands

```bash
uv sync --group test                 # the suite needs the `test` group
uv run pytest                        # full suite (xdist, coverage gate, JUnit/XML into .github/reports)
uv run pytest tests/test_core.py::CoreTests::test_battle_script_requires_army   # single test
make fmt                             # pre-commit: ruff, mdformat, codespell, ty, gitleaks, uv-sync/lock
make gen-docs                        # rebuild docs/ from the READMEs and the source
```

Run the app with `uv run ai_coc`. Two CLI hooks exist for smoke tests: `--live-test` captures a frame and asks Gemini to describe it, and `--agent-command=<text>` types a command into the AI tab and executes it. Both save a proof screenshot of the window when `COC_LIVE_TEST_SCREENSHOT` / `COC_AGENT_SCREENSHOT` point at a path.

Distributables are built in CI only: pushing a `v*` tag runs `build_release.yml`, which publishes the wheel to PyPI and attaches a PyInstaller Windows build to the release. There is no local build script.

That build defaults to `--onedir`, and the dispatch form's `package_mode` switches it to `--onefile`. Measured here, onefile costs 6.6-7.0 s to reach the first log line against onedir's 1.4-3.1 s, because it unpacks the whole bundle into a temp directory on every launch. Onefile is one 76 MB file where onedir is a 174 MB folder, so it stays available, just not the default.

## Architecture

`src/ai_coc/cli.py` is the entry point behind both console scripts (`ai_coc` and `cli`) and the PyInstaller build. PyInstaller runs it as `__main__`, so its imports stay absolute even for same-layer modules and it keeps an `if __name__ == "__main__"` block.

The three layers are directories, so an import that crosses them is visible in the import line:

- **UI and orchestration** — `ui/main_window.py` (`MainWindow`), all seven tabs plus every workflow; `ui/workers.py` (thread-pool workers and the log handler); `ui/render.py` (Markdown and log records to HTML). `cli.py` is only `main()`.
- **Adapters** — `adapters/mumu.py` (emulator lifecycle), `adapters/adb.py` (every ADB call), `adapters/ai.py` (Gemini), `adapters/secrets.py` (DPAPI), `adapters/database.py` (SQLite).
- **Pure parsers** — `parsers/village.py`, `parsers/battle.py`.
- **Data shapes** — `models.py` at the package root holds every Pydantic model in the project; see the Pydantic rule below.

Imports across layers are absolute (`from ai_coc.models import …`) and within a layer relative (`from .adb import …`); Ruff's `TID252` enforces it and rewrites the rest.

**Threading.** Every blocking call goes through `MainWindow.run_async`, which wraps the callable in a `Worker` (`QRunnable`) on the global `QThreadPool` and delivers the result back to the UI thread via `pyqtSignal`. A call that produces text as it goes uses `MainWindow.run_stream` and a `StreamWorker` instead: it drains a generator and emits each chunk. Never call `MuMuAdapter` or `GeminiClient` directly from a slot.

**The AI 助手 transcript is a model, not a text buffer.** `MainWindow.chat` is a `ChatTranscript`; `_say` appends a `ChatMessage` and `_paint_chat` re-renders the whole transcript into the `QTextBrowser` through `ui/render.py`. Bodies are Markdown, rendered by `markdown-it-py` with raw HTML disabled, and styled by `CHAT_STYLESHEET` (Qt honours only a subset of CSS 2.1). A streamed reply grows the last message in place, and repaints are throttled by the `chat_repaint` timer rather than fired per token.

**Logging.** `configure_logging()` in `main()` sets up a rotating plain-text file at `~/.ai_coc\logs\controller.log` plus a `rich` stderr console; `MainWindow._attach_log_panel` adds a handler that renders every record through `rich` and mirrors the resulting HTML into the 執行紀錄 panel under the tabs, and the panel's level selector retargets the root logger at runtime (DEBUG adds full prompts and replies). The file handler stays plain text so the log can still be grepped. `Worker.run` logs the traceback before the message box, and `sys.excepthook` catches what Qt would otherwise swallow. New adapter code is expected to log its own decisions; a feature that fails silently is the bug being fixed here.

**Agent loop** (`execute_agent_command`): ensure CoC is running, then loop up to `max_steps` (8 normally, 25 when the command mentions 進攻/戰鬥/搜尋資源村). Each step captures a screenshot plus a `uiautomator dump`, sends both to Gemini and reads back an `AgentAction` through structured output, applies it, sleeps 2 s and re-observes. Anything outside `tap|back|swipe_up|swipe_down` ends the task, so a new action verb needs a `Literal` member on `AgentAction`, a branch in `_apply_agent_action` and a mention in the prompt.

**Authorization is prompt-level only.** The automation-tab checkboxes are interpolated into the prompt as 自主升級／刷牆／自主進攻 flags, and the prompt forbids gems, cash, deletion and account operations. Nothing below the prompt enforces this. Treat any new capability that spends resources as needing its own guard in code.

**Task durability.** Every agent command becomes a row in `tasks`. A task left `PENDING` is picked up again by `resume_pending_tasks` shortly after startup and after each completion, and `automation_cycle` refuses to queue new work while one is running.

**Coordinates are hard-coded to 1600x900.** Gemini returns percentages, and `_apply_agent_action` converts them with `x_pct * 16, y_pct * 9`. Any other instance resolution taps the wrong place. Related known quirk: ADB `wm size` reports 900x1600 while the screenshot arrives as 1600x900.

**`MuMuAdapter`** uses `mumu-cli.exe` (JSON output) for discovery and lifecycle only; every capture and input goes through `adb.py`. Two rules were paid for in bugs, do not undo them:

- `_clean_environment` strips `QT_PLUGIN_PATH` and friends from every subprocess, because the PyInstaller bundle's Qt plugin paths break mumu-cli, which is itself a Qt application.
- `ensure_coc` escalates launch → poll → restart instance, because MuMu reports Android ready before a `monkey` launch will reliably stick.

**`adb.py` owns ADB.** `AdbController` wraps `adbutils`, one instance per serial, and `MuMuAdapter.controller()` caches them — that is how several emulators coexist, since each MuMu instance publishes its own port (`AdbEndpoint`, port 0 meaning not listening yet). `use_adb_executable` points `ADBUTILS_ADB_PATH` at MuMu's own `nx_main\adb.exe` so the adb server matches the emulator. Do not shell out to `adb.exe` from anywhere else.

**Every capture and every input names a display.** MuMu runs several Android displays and opens the game on one of its own, leaving display 0 on the emulator's launcher. Unqualified, `screencap -p` prefixes the PNG with a multi-display warning and the decode fails, while `input tap` silently lands on the launcher. `AdbController.display_for(package)` finds the right one and returns a `DisplayTarget`; `screencap -d` takes its physical id and `input -d` its logical id, because MuMu numbers the two schemes apart. Do not drop the `-d` flags.

**`GeminiClient`** is the only place the app talks to Gemini, through the `google-genai` SDK's Interactions API. Every request body is a `GeminiRequest`, never a hand-built dict. `generate` returns the whole text; `stream` sets `stream=True` and yields the `step.delta` text events, which is what the chat and the screen analysis use; `generate_structured(prompt, SomeModel, png)` sets `response_format` from the model's JSON schema and returns the validated instance, so no code strips ```` ```json ```` fences by hand. Structured output cannot stream, so the agent loop stays on `generate_structured`. `list_text_models` feeds the Model picker in Settings and only appears after 測試連線 succeeds. The default model is `DEFAULT_GEMINI_MODEL` in `constants.py`.

**Storage** lives in `~/.ai_coc\`: `controller.sqlite3`, `frames/`, `account_json/` and the DPAPI-protected `gemini.key.dpapi`. The schema is created idempotently in `Database._initialize` with no migration tooling, so a changed table means bumping `SCHEMA_VERSION` and handling existing databases yourself. The API key never goes into `QSettings`, only into the DPAPI file.

**Village and Battle Script parsing is deliberately tolerant.** Unknown sections and fields are preserved verbatim, unknown `data_id`s are queued into `unknown_entities` instead of failing the import. Game updates add IDs; keep it tolerant.

## Project rules

- **Every structured value is a Pydantic `BaseModel`, no exceptions.** New shapes go in `models.py`: emulator and CLI payloads, database rows, parsed files, AI replies, requests, settings and the chat transcript. The adapters are models too (`Database`, `SecretStore`, `AdbController`, `MuMuAdapter`, `GeminiClient`), so they take keyword arguments and put their non-field state in `PrivateAttr`. Only Qt subclasses and the ctypes `DATA_BLOB` stay plain classes, because their base class rules it out. Do not introduce a `dataclass`, a `TypedDict`, or a bare `dict[str, Any]` that travels between functions, and do not read a value out with `.get("key")` when a model could have declared the field — historical key names belong in `AliasChoices`, not in an `or` chain. Parse at the boundary with `model_validate` / `model_validate_json`, and write JSON out with `model_dump_json()` / `model_dump()` rather than `json.dumps` over a hand-built dict; a bare list on its way into a prompt gets a `RootModel` (`UiElementList`, `AccountRowList`). Ask Gemini for structure through `GeminiClient.generate_structured` and a model, never by parsing the text yourself. Models mirroring an external format that gains fields between releases carry `model_config = TOLERANT` so unknown keys survive.
- **A model field's type must be importable at runtime.** `[tool.ruff.lint.flake8-type-checking] runtime-evaluated-base-classes` keeps `TC003` from moving those imports into `if TYPE_CHECKING`, which would leave the model unbuildable. If a new model base class appears, add it there.
- **Never invent master data.** `entity_levels` (costs, times, requirements) stays empty until a value is source-backed, and the UI shows `—` rather than a guess. This is a stated product decision, not an oversight.
- **`docs/` is generated and gitignored**, rebuilt from the three READMEs and the source by `make gen-docs`. Edit the READMEs and the docstrings, never the generated output.
- **UI strings, prompts and user-facing messages are Traditional Chinese**; code, comments, commit messages and anything published to GitHub are English.
- **Deliberate deviations from the repo template**, documented in `pyproject.toml` comments: coverage gate is `--cov-fail-under=12` because almost everything is the untested PyQt shell; `[tool.ty.environment] python-platform = "win32"` is required or `winreg`/`ctypes.windll` fail to resolve on Linux CI runners; ty excludes `cli.py`, `ui/main_window.py` and `ui/workers.py` because PyQt5 ships inaccurate stubs; `allowed-confusables` carries `／` and `？` for the Chinese UI strings; the `build_release.yml` matrix is Windows-only because nothing here runs elsewhere.
- **The version is never written down.** `constants.py` reads it from the installed package metadata, and CI derives that from the git tag through `dunamai`. The `0.1.0` in `pyproject.toml` is a placeholder CI overwrites; do not hand-edit a version anywhere else.
- **Package data lives inside the package.** `battle_scripts/` sits at `src/ai_coc/battle_scripts/` and is reached through `BATTLE_SCRIPT_DIR`, so a wheel install resolves it the same way a source checkout does. The PyInstaller step needs `--add-data` for it and `--copy-metadata` for the version lookup; both are in `build_release.yml`.
- CodeQL and dependency-review jobs are gated on `github.event.repository.visibility == 'public'` because this private repo has no GitHub Advanced Security.
