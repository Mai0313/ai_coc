# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Windows-only PyQt5 desktop application that drives Clash of Clans running inside MuMu Player 12. Gemini does the semantic screen reading; the app turns its answers into ADB taps and verifies the outcome on the next screenshot. Live battle tactics are deliberately out of scope: `battle.py` validates army requirements and stops at a reserved `RESERVED_RL` handoff boundary.

## Commands

```bash
uv sync --group test --group build   # test needs the `test` group; scripts/build.ps1 needs `build`
uv run pytest                        # full suite (xdist, coverage gate, JUnit/XML into .github/reports)
uv run pytest tests/test_core.py::CoreTests::test_battle_script_requires_army   # single test
make fmt                             # pre-commit: ruff, mdformat, codespell, ty, gitleaks, uv-sync/lock
make gen-docs                        # regenerate docs/Reference and docs/Scripts only
```

Run the app with `uv run python app.py`. Two CLI hooks exist for smoke tests: `--live-test` captures a frame and asks Gemini to describe it, and `--agent-command=<text>` types a command into the AI tab and executes it. Both save a proof screenshot of the window when `COC_LIVE_TEST_SCREENSHOT` / `COC_AGENT_SCREENSHOT` point at a path.

Build the distributable with PowerShell:

```powershell
scripts/build.ps1   # uv sync + pytest + PyInstaller onedir into outputs/
```

## Architecture

`app.py` at the repo root is the PyInstaller entry point; it puts `src/` on `sys.path` and calls `coc_ai_controller.app.main`.

Three layers, loosely enforced by imports:

- **UI and orchestration** — `app.py` (`MainWindow`), all seven tabs plus every workflow.
- **Adapters** — `mumu.py` (emulator), `ai.py` (Gemini), `secrets.py` (DPAPI), `database.py` (SQLite).
- **Pure parsers** — `village.py`, `battle.py`. These and `Database` are the only tested parts.

**Threading.** Every blocking call goes through `MainWindow.run_async`, which wraps the callable in a `Worker` (`QRunnable`) on the global `QThreadPool` and delivers the result back to the UI thread via `pyqtSignal`. Never call `MuMuAdapter` or a provider directly from a slot.

**Agent loop** (`execute_agent_command`): ensure CoC is running, then loop up to `max_steps` (8 normally, 25 when the command mentions 進攻/戰鬥/搜尋資源村). Each step captures a screenshot plus a `uiautomator dump`, sends both to Gemini, parses one JSON object `{done, action, x_pct, y_pct, message}`, applies it, sleeps 2 s and re-observes. Anything outside `tap|back|swipe_up|swipe_down` ends the task, so a new action verb needs a branch in `_apply_agent_action` as well as a mention in the prompt.

**Authorization is prompt-level only.** The automation-tab checkboxes are interpolated into the prompt as 自主升級／刷牆／自主進攻 flags, and the prompt forbids gems, cash, deletion and account operations. Nothing below the prompt enforces this. Treat any new capability that spends resources as needing its own guard in code.

**Task durability.** Every agent command becomes a row in `tasks`. A task left `PENDING` is picked up again by `resume_pending_tasks` shortly after startup and after each completion, and `automation_cycle` refuses to queue new work while one is running.

**Coordinates are hard-coded to 1600x900.** Gemini returns percentages, and `_apply_agent_action` converts them with `x_pct * 16, y_pct * 9`. Any other instance resolution taps the wrong place. Related known quirk: ADB `wm size` reports 900x1600 while the screenshot arrives as 1600x900.

**`MuMuAdapter`** uses `mumu-cli.exe` (JSON output) for discovery and lifecycle, and the bundled `nx_main\adb.exe` for capture and input. Two rules were paid for in bugs, do not undo them:

- `_clean_environment` strips `QT_PLUGIN_PATH` and friends from every subprocess, because the PyInstaller bundle's Qt plugin paths break mumu-cli, which is itself a Qt application.
- `ensure_coc` escalates launch → poll → restart instance, because MuMu reports Android ready before a `monkey` launch will reliably stick.

**`GoogleGeminiProvider`** speaks two protocols in one class: an endpoint containing `/openai` uses OpenAI-compatible `/chat/completions`, otherwise it calls Google's native `v1beta …:generateContent`. Both paths list models and retry on a different model when the configured one 404s.

**Storage** lives in `%LOCALAPPDATA%\CoC_AI_Controller\`: `controller.sqlite3`, `frames/`, `account_json/` and the DPAPI-protected `gemini.key.dpapi`. The schema is created idempotently in `Database._initialize` with no migration tooling, so a changed table means bumping `SCHEMA_VERSION` and handling existing databases yourself. The API key never goes into `QSettings`, only into the DPAPI file.

**Village and Battle Script parsing is deliberately tolerant.** Unknown sections and fields are preserved verbatim, unknown `data_id`s are queued into `unknown_entities` instead of failing the import. Game updates add IDs; keep it tolerant.

## Project rules

- **Never invent master data.** `entity_levels` (costs, times, requirements) stays empty until a value is source-backed, and the UI shows `—` rather than a guess. This is a stated product decision, not an oversight.
- **`docs/` is hand-written and committed**, unlike the upstream template it was synced from. That is why `make gen-docs` and `make clean` only touch `docs/Reference` and `docs/Scripts`. When behaviour changes, update `docs/handoff.md`, `docs/changelog.md` and `docs/dev-session.md`.
- **UI strings, prompts and user-facing messages are Traditional Chinese**; code, comments, commit messages and anything published to GitHub are English.
- **`.agents/skills/mumu-control/` is vendored upstream** (pinned in `skills-lock.json`) and excluded from ruff. Do not restyle it.
- **Deliberate deviations from the repo template**, documented in `pyproject.toml` comments: coverage gate is `--cov-fail-under=12` because almost everything is the untested PyQt shell; `[tool.ty.environment] python-platform = "win32"` is required or `winreg`/`ctypes.windll` fail to resolve on Linux CI runners; ty excludes `app.py` because PyQt5 ships inaccurate stubs; `allowed-confusables` carries `／` and `？` for the Chinese UI strings.
- **The version appears in six places** and they move together: `VERSION`, `pyproject.toml`, `src/coc_ai_controller/__init__.py`, `constants.py`, `version_info.txt` and the `--name` in `scripts/build.ps1`.
- CodeQL and dependency-review jobs are gated on `github.event.repository.visibility == 'public'` because this private repo has no GitHub Advanced Security.
