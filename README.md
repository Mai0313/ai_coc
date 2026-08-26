<div align="center" markdown="1">

# AI CoC

[![PyPI version](https://img.shields.io/pypi/v/ai_coc.svg)](https://pypi.org/project/ai_coc/)
[![python](https://img.shields.io/badge/-Python_%7C_3.12%7C_3.13%7C_3.14-blue?logo=python&logoColor=white)](https://www.python.org/downloads/source/)
[![uv](https://img.shields.io/badge/-uv_dependency_management-2C5F2D?logo=python&logoColor=white)](https://docs.astral.sh/uv/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![ty](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ty/main/assets/badge/v0.json)](https://github.com/astral-sh/ty)
[![Pydantic v2](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/pydantic/pydantic/main/docs/badge/v2.json)](https://docs.pydantic.dev/latest/contributing/#badges)
[![tests](https://github.com/Mai0313/ai_coc/actions/workflows/test.yml/badge.svg)](https://github.com/Mai0313/ai_coc/actions/workflows/test.yml)
[![code-quality](https://github.com/Mai0313/ai_coc/actions/workflows/code-quality-check.yml/badge.svg)](https://github.com/Mai0313/ai_coc/actions/workflows/code-quality-check.yml)
[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/Mai0313/ai_coc)
[![license](https://img.shields.io/badge/License-MIT-green.svg?labelColor=gray)](https://github.com/Mai0313/ai_coc/tree/main?tab=License-1-ov-file)
[![PRs](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](https://github.com/Mai0313/ai_coc/pulls)
[![contributors](https://img.shields.io/github/contributors/Mai0313/ai_coc.svg)](https://github.com/Mai0313/ai_coc/graphs/contributors)

</div>

A Windows desktop application that drives Clash of Clans running inside MuMu Player 12. Gemini reads the screen; the app turns its answers into ADB taps and verifies the outcome on the next screenshot.

Other Languages: [English](README.md) | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

## ✨ What it does

- Detects MuMu Player 12 instances, starts the emulator and launches the game
- Captures the screen over ADB and asks Gemini what is on it, with structured replies validated through Pydantic
- Runs an agent loop that taps, swipes and goes back, re-observing after every step
- Imports village JSON exports and battle scripts, keeping unknown fields and unknown `data_id`s instead of failing on them
- Farms resource opponents on its own, reading the screen with template matching rather than a vision call
- Spends the storages on wall upgrades, sizing each batch to what the village can afford
- Persists every agent command as a task, so an interrupted run is picked up again on the next start
- Stores the Gemini API key through Windows DPAPI, never in plain settings

Imported battle scripts are not played back: they are validated for army requirements and stop at a reserved handoff boundary.

## 📋 Requirements

- Windows. The app talks to `mumu-cli.exe`, reads the registry through `winreg` and calls DPAPI through `ctypes.windll`, none of which exist elsewhere
- [MuMu Player 12](https://www.mumuplayer.com/) with Clash of Clans installed, running at 1600x900
- A Gemini API key, entered in the app's settings tab

## 🚀 Install and run

From PyPI, without installing anything permanently:

```bash
uvx ai_coc
```

Or as a regular install:

```bash
uv tool install ai_coc
ai_coc
```

Prebuilt Windows executables are attached to every [release](https://github.com/Mai0313/ai_coc/releases).

## 🛠️ Local development

```bash
git clone https://github.com/Mai0313/ai_coc.git
cd ai_coc
uv sync --group test          # install dependencies
uvx pre-commit install        # install git hooks
uv run ai_coc                 # start the app
```

Two command-line hooks exist for smoke tests. `--live-test` captures a frame and asks Gemini to describe it; `--agent-command=<text>` types a command into the AI tab and runs it. Both save a proof screenshot when `COC_LIVE_TEST_SCREENSHOT` / `COC_AGENT_SCREENSHOT` point at a path.

## 🧰 Commands Reference

```bash
# Development
make help               # List available make targets
make clean              # Clean caches, artifacts and generated docs
make fmt                # Run all pre-commit hooks
make test               # Run pytest across the repository
make gen-docs           # Generate docs from src/ and scripts/

# Dependencies (via uv)
make uv-install         # Install uv on your system
uv add <pkg>            # Add production dependency
uv add <pkg> --dev      # Add development dependency
# Sync optional groups
uv sync --group dev     # Install dev-only deps (pre-commit, poe, notebook)
uv sync --group test    # Install test-only deps
uv sync --group docs    # Install docs-only deps
```

## 🧱 Architecture

The three layers are directories, so an import that crosses them is visible in the import line:

- **UI and orchestration** — `ui/main_window.py` holds the window and every workflow, `ui/workers.py` the thread-pool workers, `ui/render.py` the Markdown and log rendering. `cli.py` is only `main()`
- **Adapters** — `adapters/mumu.py` (emulator lifecycle), `adapters/adb.py` (every ADB call), `adapters/ai.py` (Gemini), `adapters/secrets.py` (DPAPI), `adapters/config.py` (the shared settings file), `adapters/database.py` (SQLite)
- **Pure parsers** — `parsers/village.py`, `parsers/battle.py`

Every structured value is a Pydantic model, collected in `models.py`. Blocking calls go through a `QThreadPool` worker and come back to the UI thread as a signal.

Application state lives in `~/.ai_coc`: the SQLite database, captured frames, imported account JSON, the DPAPI-protected key file and `config.json`, which is the one settings file the window and the terminal both read.

## 📚 Documentation

Documentation is built with [Zensical](https://zensical.org/) and auto-generated from source code via `scripts/gen_docs.py`.

```bash
uv sync --group docs
make gen-docs                  # generate markdown from source
uv run zensical serve          # http://0.0.0.0:9987
```

`make gen-docs` recreates `docs/`, copies the three READMEs in, then runs `gen_docs.py` over `./src` and `./scripts`.

## 📦 Packaging and Distribution

Build artifacts with uv (wheel and sdist go to `dist/`):

```bash
uv build
```

Publish to PyPI (requires `UV_PUBLISH_TOKEN`):

```bash
UV_PUBLISH_TOKEN=... uv publish
```

Pushing a `v*` tag runs `build_release.yml`, which derives the version from git via `dunamai`, builds the wheel and sdist, publishes to PyPI, packages a Windows build with PyInstaller and attaches everything to the GitHub Release.

That build is `--onedir` by default: the zip holds the executable next to an `_internal/` folder, which starts several seconds faster than a single-file build that has to unpack itself on every launch. Running the workflow by hand offers a `package_mode` choice if you want the single `.exe` instead.

## 🧭 Optional task runner (Poe the Poet)

Convenience tasks are defined under `[tool.poe.tasks]` in `pyproject.toml` and available after installing the dev group (`uv sync --group dev`) or via `uvx`:

```bash
uv run poe docs        # generate + serve docs (requires dev group)
uv run poe gen         # generate + deploy docs (gh-deploy) (requires dev group)
uv run poe main        # run the app (same as uv run ai_coc)

# or ephemeral via uvx (no local install)
uvx poe docs
```

## 🔁 CI/CD Actions Overview

All workflows live in `.github/workflows/`.

- Tests (`test.yml`)

    - Trigger: pushes and pull requests to `main` or `release/*` (ignores md files)
    - Runs pytest on Python 3.12/3.13/3.14 with coverage and comments a summary

- Code Quality Check (`code-quality-check.yml`)

    - Trigger: pull requests
    - Runs ruff and the rest of the pre-commit suite

- Docs Deploy (`deploy.yml`)

    - Trigger: push to `main` and tags `v*`
    - Builds the `zensical` site and publishes to GitHub Pages
    - Setup needed: enable GitHub Pages for the repo (Settings → Pages → Source: GitHub Actions)

- Build and Release (`build_release.yml`)

    - Trigger: tags `v*` push or manual workflow dispatch
    - Builds a Windows x64 executable with PyInstaller, plus the wheel and sdist
    - Publishes to PyPI (requires the `UV_PUBLISH_TOKEN` secret) and uploads every artifact to the GitHub Release

- Publish Docker Image (`build_image.yml`)

    - Trigger: push to `main` and tags `v*`
    - Builds and pushes an image to GHCR: `ghcr.io/<owner>/<repo>`

- Release Drafter (`release_drafter.yml`)

    - Trigger: push to `main` and PR events
    - Maintains a draft release based on Conventional Commits

- Code Scanning (`code_scan.yml`)

    - Trigger: push and PR
    - Runs gitleaks; the CodeQL job needs GitHub Advanced Security and stays skipped while the repo is private

- Semantic Pull Request (`semantic-pull-request.yml`)

    - Trigger: PR open/edit/sync
    - Enforces Conventional Commit style PR titles

### CI/CD Configuration Checklist

- Conventional commits for PR titles (enforced by the workflow)
- Set the `UV_PUBLISH_TOKEN` secret to publish to PyPI (Settings → Secrets and variables → Actions)
- Optional: enable GitHub Pages for docs deployment (Settings → Pages → Source: GitHub Actions)
- Container Registry permissions are handled automatically via `GITHUB_TOKEN`

## 🤝 Contributing

- Open issues/PRs
- Follow the coding style (ruff, type hints)
- Use Conventional Commit messages and descriptive PR titles

## 📄 License

MIT — see `LICENSE`.
