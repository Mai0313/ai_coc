---
trigger: glob
globs: "src/**, tests/**, pyproject.toml, uv.lock, .pre-commit-config.yaml, .github/**, Makefile, README.md, README.zh-TW.md, README.zh-CN.md, scripts/**, mkdocs.yml"
paths:
  - "src/**"
  - "tests/**"
  - "pyproject.toml"
  - "uv.lock"
  - ".pre-commit-config.yaml"
  - ".github/**"
  - "Makefile"
  - "README.md"
  - "README.zh-TW.md"
  - "README.zh-CN.md"
  - "scripts/**"
  - "mkdocs.yml"
---

# The code: commands, map and rules

## Commands

```bash
uv sync --group test                 # the suite needs the `test` group
uv run pytest                        # full suite (xdist, coverage gate, JUnit/XML into .github/reports)
uv run pytest --no-cov tests/test_core.py::ScoutTests::test_loot_is_read_over_grass   # single test; the gate is in addopts and a partial run cannot meet it
make fmt                             # pre-commit: ruff, mdformat, codespell, ty, gitleaks, uv-sync/lock
make gen-docs                        # rebuild docs/ from the READMEs and the source
```

**Real battles are cheap evidence, and one battle is not evidence.** Training is instant and free and the search fee is negligible, so a battle that goes badly or deploys nothing costs only its wall-clock time, and a run spent entirely on probes is normal. Run five rounds back to back rather than reasoning from one sample: the opponent varies more than anything being changed.

`--debug` fills `frames/` and lowers the log to DEBUG (each raw ADB call, Gemini's whole prompt and reply). Each capture is named for what the loop was asking (`0006_scout`, `0010_pass`, `0018_dropped`), and `before-drop` / `dropped` are a pair for settling whether a hero went down. `--shot-every` adds `tick_00012.3s.png` frames between the loop's own captures. Every saved frame gets a `Saved frame <name>` line as it is written, so the lines around it say what was read off it.

**What is cheap and only ever needed afterwards is logged on every run, `--debug` or not.** Every input sent to the emulator is in `run.log` with where it landed (a tap, each point of a burst, a swipe, a pinch, the back key), because `plans.jsonl` holds only where the plan aimed and a battle cannot be replayed. `--debug` is for what is too bulky to keep every time.

## Architecture

`src/ai_coc/cli.py` is the entry point behind both console scripts (`ai_coc` and `cli`) and the PyInstaller build. PyInstaller runs it as `__main__`, so its imports stay absolute and it keeps an `if __name__ == "__main__"` block.

The layers are directories. Imports across layers are absolute (`from ai_coc.models import …`), within a layer relative (`from .adb import …`); Ruff's `TID252` enforces it.

- **Loops and orchestration** — `ui/attack.py` (`AttackRunner`), `ui/world.py` (crossing, the loot cart), `ui/plates.py` (the top-row plates), and the `GameRunner` subclasses in `ui/upkeep.py` and `ui/clan.py`, over `ui/runner.py`. `cli.py` is argument parsing and `main()`; `commands.py` is the headless side it dispatches to. **New work belongs somewhere `commands.py` can call it**: work no sub-command reaches cannot be run against the live game.
- **Adapters** — `adapters/emulator.py` (what every emulator does the same way, over ADB), `mumu.py` and `ldplayer.py` (each one's own CLI: its instances and their lifecycle), `adb.py` (every ADB call), `ai.py` (Gemini), `config.py` (the settings file, the API key included), `mapping.py` (the community `data_id` table), `clipboard.py` (the Windows clipboard).
- **Pure parsers** — `parsers/frame.py` decodes the 1600x900 frame and raises on any other size. `village.py` parses the village export's JSON rather than a frame. Screens: `scout.py` (loot panel, card row, storage bars, which screen is up), `building.py` (building menus, prices, dialogs), `home.py` (collector markers, the builder and laboratory counters, the build panel, the shield bar), `boundary.py`, `field.py`, `clan.py`, `world.py` (which village), `settings.py` (the pages the village export sits behind). Two are machinery that every other reader is built from: `glyphs.py` reads numbers against ten bit-pattern digit templates, and `regions.py` finds and measures connected patches of one colour. A screen module keeps only its own constants.
- **Data shapes** — `models.py` holds every data-shape model in the project; the adapters and runners, which are models too, live in their own modules.
- **Prompts** — `prompts/*.md`, one file per prompt, loaded once into `PROMPTS`. Each is a `str.format` template, so a literal brace is doubled.
- **Plans** — `plans/*.json`, written tactics; `flat.json` and `night_flat.json` are the no-key defaults.

## Code rules

- **Every structured value is a Pydantic `BaseModel`, no exceptions.** New shapes go in `models.py`: emulator and CLI payloads, parsed files, AI replies, requests and settings. The adapters are models too (`ConfigStore`, `AdbController`, `MuMuAdapter`, `GeminiClient`), so they take keyword arguments and keep non-field state in `PrivateAttr`. Only logging handlers and exceptions stay plain classes. No `dataclass`, no `TypedDict`, no bare `dict[str, Any]` travelling between functions, and no `.get("key")` where a model could declare the field; historical key names go in `AliasChoices`. Parse at the boundary with `model_validate` / `model_validate_json`, write with `model_dump_json()` / `model_dump()`, and give a bare list that travels a `RootModel` (`AttackSeries`). Ask Gemini for structure through `GeminiClient.generate_structured`. Models mirroring an external format that gains fields carry `model_config = TOLERANT`.
- **No new Windows-only code where a portable way exists.** The controller is headed for another OS, driving the emulator's ADB over the network, and the Windows-only pieces are being removed one at a time: DPAPI and the emulators' bundled `adb.exe` are gone, and what is left is `winreg`, the emulators' own CLIs (`ldplayer.py` decodes `ldconsole`'s output as `mbcs`, a codec only Windows has) and `ctypes.windll` for the clipboard. So new code does not reach for `winreg`, `ctypes.windll`, a Windows key store, or a hard-coded `.exe` or install path when a cross-platform route does the job; what an emulator itself forces (its CLI, where it installs) stays inside that emulator's adapter. The user asked for this on 2026-09-26.
- **Every command works on the village the game is on, and none of them sails on its own.** Crossing is `ai_coc world --go` and nothing else, so whoever started a command knows where it left the game. A command with no version on the village it finds does nothing and says so by name: `builder_base` for the home village's `donate`, and for `probe` and `bounds`; `other_village` for an attack series on the other village, a `--plan` written for the other one included. Composing inside one village is allowed (the night attack visits its own cart, `collect` on the builder base empties it); stringing work across both villages is the caller's job — the skills run `world --go` first. The user asked for this on 2026-09-25; design a new command the same way.
- **A report carries fields and a named outcome, never a sentence.** No model in `models.py` has a `message`. If the fields can say which branch a run came back from, add nothing. If they cannot, the report carries a required `Literal` outcome with one value per branch and no default. The Chinese is built from the outcome at the edge, in a `dict[XOutcome, str]` beside the command in `commands.py`, and each table has a test asserting it covers the type and that every line says something the others do not. A `*_line` helper (`round_line`, `collect_line`) composes counts and names with the outcome.
- **A model field's type must be importable at runtime.** `[tool.ruff.lint.flake8-type-checking] runtime-evaluated-base-classes` keeps `TC003` from moving those imports under `if TYPE_CHECKING`; add any new model base class there.
- **Never invent master data.** What an upgrade costs, how long it takes and what it requires stay unknown until a value is source-backed, and the UI shows `—` rather than a guess.
- **`docs/` is generated and gitignored**, rebuilt from the three READMEs and the source by `make gen-docs`. Edit the READMEs and the docstrings, never the output.
- **Tests touch nothing outside their own temporary directory.** `tests/conftest.py` points the state file and the settings file at each test's `tmp_path`, because loading `config.json` rewrites it to the model. A test that can reach any other file under `~/.ai_coc` gets redirected there too, and a full run leaves that directory exactly as it found it.
- **A frame committed to `tests/frames/` carries nobody's identity.** Every frame is published with the repository, so before one is added, paint over every player name (the account's own included), player tag and Supercell ID, every clan name (under a player's name, in a list, or on a clan castle's banner), and every chat message somebody typed. Use a flat patch of the colour around the text, not black: `chat_open` counts black as nothing drawn, and blacking out a chat's messages made the chat stop reading as open. Paint over only those, since the rest of the frame is what the negative tests sweep. The own name stays where a test reads it: the builder base battle frames keep it, because `test_a_screens_own_text_is_not_trimmed_into_a_reading` takes it for text that must not read as a number. Run the full suite afterwards, since the patch changes pixels a parser may measure.
- **Deliberate deviations from the repo template**, each documented beside its setting (`pyproject.toml`, `.pre-commit-config.yaml`, the workflows) and listed in `.github/CONTRIBUTING.md`: pytest-xdist is capped at `--maxprocesses=8`, because `auto` counts every physical core on a hybrid CPU and stutters the emulator; `[tool.ty.environment] python-platform = "win32"` so `winreg` and `ctypes.windll` resolve on Linux; `allowed-confusables` carries `／` and `？`; `check-added-large-files` excludes `tests/frames/`, whose exact pixel values are the measurement; the sdist ships no `tests/`, because the frames alone are about 100 MB; `build_release.yml`'s build matrix and `test.yml` run on Windows only. **`test.yml` has to be Windows**: on Linux `test_core.py` cannot import (`adapters/emulator.py` imports `winreg`), and a suite that cannot import looks exactly like one that passes. The test step also runs under `shell: bash`, whose `pipefail` keeps `uv run pytest | tee` from reporting `tee`'s exit code. `code-quality-check.yml` stays on Linux, since ruff and ty read the source rather than import it.
- **The version is never written down.** `ai_coc/__init__.py` reads it from the installed package metadata, which CI derives from the git tag through `dunamai`. The `0.1.0` in `pyproject.toml` is a placeholder CI overwrites; do not hand-edit a version anywhere, `version_info.txt` included.
- **The PyInstaller step needs `--copy-metadata`** for the version lookup and one `--add-data` line each for `prompts/` and `plans/`, in `build_release.yml`; a missing data line raises the first time it is loaded.
