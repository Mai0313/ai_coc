# Contributing Guide

Thank you for your interest in contributing to this Python project. This document describes how to set up the development environment, the conventions used by the project, and the workflow expected for issues and pull requests.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Ways to Contribute](#ways-to-contribute)
- [Reporting Issues](#reporting-issues)
- [Development Setup](#development-setup)
- [Local Workflow](#local-workflow)
- [Architecture](#architecture)
- [Testing](#testing)
- [Documentation](#documentation)
- [Branching Model](#branching-model)
- [Commit Convention](#commit-convention)
- [Pull Request Process](#pull-request-process)
- [Code Review](#code-review)
- [Coding Standards](#coding-standards)
- [Deliberate deviations from the template](#deliberate-deviations-from-the-template)
- [Packaging and Distribution](#packaging-and-distribution)
- [Optional task runner (Poe the Poet)](#optional-task-runner-poe-the-poet)
- [CI/CD Overview](#cicd-overview)
- [Security Reports](#security-reports)
- [Licensing](#licensing)

## Code of Conduct

All contributors are expected to behave professionally and respectfully. Personal attacks, harassment, and discriminatory language are not tolerated. By participating, you agree to uphold a welcoming environment for everyone.

## Ways to Contribute

- Reporting bugs and reproducible issues
- Proposing or implementing new features
- Improving documentation, examples, and tutorials
- Reviewing pull requests and providing constructive feedback
- Suggesting tooling, performance, or security improvements

## Reporting Issues

Before opening a new issue:

1. Search existing issues to avoid duplicates.
2. Confirm the problem reproduces on the latest release or `main`.
3. Use the appropriate issue template.

Please include:

- A clear, descriptive title
- The Python version, OS, and project version or commit
- Minimal reproduction steps and a code snippet when applicable
- Expected vs. actual behavior
- Full stack traces, logs, or screenshots

## Development Setup

This project uses [`uv`](https://docs.astral.sh/uv/) for Python and dependency management.

```bash
# Install uv (one-time setup)
make uv-install

# Clone your fork (on Windows add `-c core.symlinks=true`, which needs Developer Mode,
# or CLAUDE.md and .claude/skills arrive as text files instead of links)
git clone https://github.com/<your-username>/<repo>.git
cd <repo>

# Install dependencies and create the virtual environment
uv sync --group test

# Install pre-commit hooks
uv run pre-commit install

# Start the app
uv run ai_coc
```

The `test` group is what the suite needs; a plain `uv sync` installs enough to run the app but not enough to test it.

Every loop has a headless sub-command (`uv run ai_coc attack`, `walls`, `collect`, and the rest; see the README), and those are how the game is worked on: a feature reachable only through a widget cannot be driven against the live game while it is being written. The window runs the same functions, so a sub-command is also the smoke test for what the window does.

Supported Python versions are declared in `pyproject.toml`. Use `uv` to manage interpreters when needed:

```bash
uv python install 3.12
```

## Local Workflow

Common tasks are exposed via the `Makefile`. Run `make help` to list all targets. Frequently used ones:

```bash
make fmt       # Run pre-commit hooks (ruff, mdformat, codespell, ty, ...)
make test      # Run the test suite
make gen-docs  # Generate documentation
make clean     # Remove caches and build artifacts
```

Always run `make fmt` and `make test` before opening a pull request.

## Architecture

The three layers are directories, so an import that crosses them is visible in the import line:

- **UI and orchestration**: `ui/main_window.py` holds the window and every tab; `ui/workers.py` the thread-pool workers; `ui/render.py` the Markdown and log rendering. The loops themselves are kept Qt-free so they can be driven and tested without a window: `ui/attack.py` (the attack loop), `ui/world.py` (crossing between the two villages), and `ui/walls.py`, `ui/upkeep.py`, `ui/clan.py`, `ui/hero.py` on top of the shared `ui/runner.py`
- **Adapters**: `adapters/emulator.py` (what every emulator does the same way), `adapters/mumu.py` and `adapters/ldplayer.py` (each emulator's own CLI and lifecycle), `adapters/adb.py` (every ADB call), `adapters/ai.py` (Gemini), `adapters/config.py` (the shared settings file, the API key included), `adapters/mapping.py` (the community `data_id` table), `adapters/clipboard.py` (the Windows clipboard, where the game's own village export arrives)
- **Pure parsers**: `parsers/frame.py` (the 1600x900 frame every reader decodes through), `parsers/scout.py` (loot panel, card row, storage bars, and which screen is up), `parsers/building.py` (building menus and their prices), `parsers/home.py` (collector markers, builder panel), `parsers/world.py` (which of the two villages is on screen), plus `parsers/village.py`, `parsers/boundary.py`, `parsers/field.py`, `parsers/clan.py` and `parsers/hero.py`. Two of them are shared machinery rather than screens and every other reader is built out of one or both: `parsers/glyphs.py` reads a number by matching each digit to a template, and `parsers/regions.py` finds the connected patches of one colour

`cli.py` is argument parsing and `main()`; `commands.py` is the headless side it dispatches to. New work belongs somewhere `commands.py` can call it.

Prompts are one Markdown file each under `prompts/`, loaded once and filled in by `render`, because a prompt is reworded far more often than the code around it.

Every structured value is a Pydantic model, collected in `models.py`: no `dataclass`, no `TypedDict`, and no bare `dict[str, Any]` travelling between functions. Blocking calls go through a `QThreadPool` worker and come back to the UI thread as a signal; never call an adapter directly from a slot.

The comment beside each measured constant in these loops carries the reasoning behind its value, and `AGENTS.md` at the repo root carries the rules that span files. Read both before changing a constant, because most of them were paid for by a battle that went wrong.

## Testing

- Tests are written with **pytest** and live under `tests/`.
- Coverage is gated at **75%**, against a suite that measures about 81%: `ui/main_window.py` is the untested PyQt shell and a seventh of the statements, and everything under it sits around 90%. The parsers read recorded frames, the loops and commands run against a patched emulator, and new logic in either is expected to come with tests of the same shape. The gate sits in `addopts`, so a partial run cannot meet it: pass `--no-cov` when running a single file or test.
- Use `pytest-xdist` for parallel execution where helpful.
- Recorded frames under `tests/frames/` are the fixtures for the readers. Reduce a real capture to the region being read and fill the rest with a flat colour rather than re-encoding it: these readers turn on exact pixels, and requantising a frame makes a bad one pass and a good one fail.
- After writing a test, take the fix back out and check it actually fails. A parser test that passes either way is the easiest kind to write by accident.

Useful commands:

```bash
uv run pytest                                # Run all tests, with coverage and the gate
uv run pytest --no-cov tests/test_foo.py     # Run a single file
uv run pytest --no-cov -k "expression"       # Run tests matching an expression
```

Add tests for every behavioral change. Bug fixes should include a regression test.

## Documentation

Documentation uses **Zensical** with `mkdocstrings` and lives under `docs/`. To preview locally:

```bash
make gen-docs
uv run zensical serve  # http://0.0.0.0:9987
```

`make gen-docs` recreates `docs/` from scratch: it copies the three READMEs in, then runs `gen_docs.py` over `./src` and `./scripts`. **`docs/` is generated and gitignored**, so edit the READMEs and the docstrings, never the output.

The three READMEs are user-facing and stay that way: what the app does, how to install it, what each command is for. Anything a contributor needs and a user does not belongs in this file. Keep the three in step when you change one, and mind that `README.md` is the English original while the other two are translations of it.

Documentation contributions are first-class and very welcome.

## Branching Model

- `main` is the default branch and must always be releasable.
- Feature branches: `feat/<short-description>`
- Bug fix branches: `fix/<short-description>`
- Documentation branches: `docs/<short-description>`

## Commit Convention

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/) and **must be written in English**.

Format:

```
<type>(<optional scope>): <short summary>

<optional body>

<optional footer>
```

Allowed types:

| Type       | Purpose                                                 |
| ---------- | ------------------------------------------------------- |
| `feat`     | A new feature                                           |
| `fix`      | A bug fix                                               |
| `refactor` | Code change that neither fixes a bug nor adds a feature |
| `doc`      | Documentation-only changes                              |
| `perf`     | Performance improvement                                 |
| `style`    | Formatting or stylistic changes                         |
| `test`     | Adding or correcting tests                              |
| `chore`    | Build, tooling, or auxiliary changes                    |
| `ci`       | Continuous integration changes                          |
| `revert`   | Reverting a previous commit                             |

Append `!` after the type or include `BREAKING CHANGE:` in the footer to indicate a breaking change. Reference issues with `Closes #123` or `Refs #123`.

## Pull Request Process

1. Ensure your branch is up to date with the target branch.
2. Run `make fmt` and `make test` locally; both must pass.
3. Ensure CI checks pass on the pull request.
4. Use a descriptive title following the commit convention; it is validated by **semantic-pull-request**.
5. Fill out the pull request template, including motivation, summary, and testing notes.
6. Link related issues and design documents.
7. Mark the PR as **draft** while still in progress.
8. Request review only after self-review and a green CI.

Pull requests are typically merged via **squash merge** to keep history linear.

## Code Review

- Address all review comments or explain why a change is not needed.
- Keep discussions technical, focused, and respectful.
- Resolve conversations only after the concern has been addressed.

## Coding Standards

- **Formatting and linting**: handled by `ruff` (configured in `pyproject.toml`)
- **Typing**: `ty` with strict rules, running against the project environment so first-party imports resolve; new code should be type-annotated
- **Spelling**: enforced by `codespell` via pre-commit
- **Notebooks**: stripped of outputs by `nbstripout`

Prefer clarity over cleverness, and avoid unrelated refactors in feature or fix pull requests.

## Deliberate deviations from the template

These are documented in `pyproject.toml` comments and are not oversights:

- Coverage is gated at 75% rather than 80%, because `ui/main_window.py` is the untested PyQt shell and a seventh of the statements
- `[tool.ty.environment] python-platform = "win32"` is required, or `winreg` and `ctypes.windll` fail to resolve when the linters run on Linux
- ty excludes `cli.py`, `ui/main_window.py` and `ui/workers.py`, because PyQt5 ships inaccurate stubs
- `allowed-confusables` carries `／` and `？` for the Chinese UI strings
- The `build_release.yml` matrix is Windows-only, because nothing here runs elsewhere
- **`test.yml`'s matrix is Windows-only for a harder reason**: `adapters/emulator.py` imports `winreg` at module scope, so on Linux `test_core.py` cannot be imported and its tests are silently skipped. That went unnoticed because `uv run pytest | tee` reported `tee`'s exit code rather than pytest's, so the job passed either way. Keep `set -o pipefail` in that step
- Every other workflow still runs on Linux, and can: none of them imports `ai_coc`. ruff and ty read the source, and `gen_docs.py` parses it with `ast` while mkdocstrings goes through griffe. A new job that needs to *import* the package belongs on Windows
- The CodeQL and dependency-review jobs are gated on the repository being public, because on a private one both need GitHub Advanced Security

UI strings, prompts and user-facing messages are Traditional Chinese; code, comments, commit messages and anything published to GitHub are English.

The version is never written down by hand. `constants.py` reads it from the installed package metadata and CI derives that from the git tag through `dunamai`, so the `0.1.0` in `pyproject.toml` is a placeholder CI overwrites.

## Packaging and Distribution

Build artifacts with uv (wheel and sdist go to `dist/`):

```bash
uv build
```

Publish to PyPI (requires `UV_PUBLISH_TOKEN`):

```bash
UV_PUBLISH_TOKEN=... uv publish
```

Pushing a `v*` tag runs `build_release.yml`, which derives the version from git, builds the wheel and sdist, publishes to PyPI, packages a Windows build with PyInstaller and attaches everything to the GitHub Release. There is no local build script.

That build is `--onedir` by default: the zip holds the executable next to an `_internal/` folder. Measured here, onefile takes 6.6-7.0 s to reach the first log line against onedir's 1.4-3.1 s, because it unpacks the whole bundle into a temp directory on every launch. Running the workflow by hand offers a `package_mode` choice if you want the single `.exe` anyway.

The PyInstaller step needs `--copy-metadata` for that version lookup and one `--add-data` line each for `prompts/` and `plans/`. Dropping a data line leaves a build that raises the first time it loads what is missing, which for `prompts/` is every AI call in the app.

## Optional task runner (Poe the Poet)

Convenience tasks are defined under `[tool.poe.tasks]` in `pyproject.toml` and available after installing the dev group (`uv sync --group dev`) or via `uvx`:

```bash
uv run poe docs        # generate + serve docs (requires dev group)
uv run poe gen         # generate + deploy docs (gh-deploy) (requires dev group)
uv run poe main        # run the app (same as uv run ai_coc)

# or ephemeral via uvx (no local install)
uvx poe docs
```

## CI/CD Overview

All workflows live in `.github/workflows/`.

- **Tests** (`test.yml`): pushes and pull requests to `main` or `release/*`, ignoring md files. Runs pytest on Python 3.12 on a Windows runner with coverage and comments a summary
- **Code Quality Check** (`code-quality-check.yml`): pull requests. Runs ruff and the rest of the pre-commit suite
- **Docs Deploy** (`deploy.yml`): push to `main`. Builds the Zensical site and publishes to GitHub Pages. There is deliberately no `v*` tag trigger, because the `github-pages` environment only accepts deployments from `main` and a tag push fails before any step runs. Needs GitHub Pages enabled (Settings → Pages → Source: GitHub Actions)
- **Build and Release** (`build_release.yml`): tags `v*` or manual dispatch. Builds a Windows x64 executable with PyInstaller plus the wheel and sdist, publishes to PyPI (needs the `UV_PUBLISH_TOKEN` secret) and uploads everything to the GitHub Release
- **Publish Docker Image** (`build_image.yml`): push to `main` and tags `v*`. Pushes to GHCR: `ghcr.io/<owner>/<repo>`
- **Release Drafter** (`release_drafter.yml`): push to `main` and PR events. Maintains a draft release from Conventional Commits
- **Code Scanning** (`code_scan.yml`): push and PR. Runs gitleaks and trufflehog; the CodeQL job in the same file needs GitHub Advanced Security on a private repository, so it runs only while the repo is public
- **Dependabot Auto Merge** (`auto_review_merge.yml`): pull requests. Auto-merges Dependabot PRs, and carries the dependency-review job, which is gated on the same visibility check as CodeQL
- **Semantic Pull Request** (`semantic-pull-request.yml`): PR open/edit/sync. Enforces Conventional Commit style PR titles

### Configuration checklist

- Conventional commits for PR titles (enforced by the workflow)
- Set the `UV_PUBLISH_TOKEN` secret to publish to PyPI (Settings → Secrets and variables → Actions)
- Optional: enable GitHub Pages for docs deployment (Settings → Pages → Source: GitHub Actions)
- Container Registry permissions are handled automatically via `GITHUB_TOKEN`

## Security Reports

Please **do not** report security vulnerabilities through public issues. Refer to [`SECURITY.md`](./SECURITY.md) for the responsible disclosure process.

## Licensing

By contributing, you agree that your contributions will be licensed under the project's license (see [`LICENSE`](../LICENSE)). Ensure that you have the right to submit any code, content, or assets you contribute.
