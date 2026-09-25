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

A Windows desktop application that plays Clash of Clans for you inside MuMu Player 12. It reads the screen itself and turns what it finds into ADB taps, checking the outcome on the next screenshot. Gemini is asked exactly one question per battle: how to attack this village.

Other Languages: [English](README.md) | [繁體中文](README.zh-TW.md) | [简体中文](README.zh-CN.md)

## ✨ What it does

**Attacks for loot on its own.** It searches for opponents, skips the ones carrying less than your thresholds, puts the whole army down in about ten seconds and plays the battle out, then comes round for the next one. It stops by itself when the storages are full. Screen reading is done here rather than sent away: the loot panel, the army bar and every card in the row are matched against templates, so a skipped opponent costs nothing and a battle is never waiting on a network call.

**Asks Gemini exactly one question per battle**, and only after an opponent has already passed your thresholds: how to attack this particular village. It answers where to drop the line of troops, where each rage and freeze goes, and which hero is on which card. Without an API key the app plays a fixed tactic instead and everything else still works.

**Spends what it farms.** Wall upgrades finish the moment they are paid for, so they are where a full storage goes; the loop finds walls on the map, works out the cheapest batch the village can afford and buys it. Idle builders can be put on the most expensive upgrade affordable, and heroes raised one at a time.

**Keeps the village ticking.** Empties the collectors, reads what every builder is on and how long is left, and donates troops to whoever is asking in the clan chat.

**Recovers on its own.** A session dropped for idling restarts the game and carries on, whichever loop was running. A server that is not answering is waited out rather than tapped at.

**Keeps your key out of plain settings.** The Gemini API key is stored through Windows DPAPI, never in the registry or a config file.

Village exports keep unknown fields and unknown `data_id`s instead of failing on them, and the sections are read off the document rather than from a written-down list.

## 📋 Requirements

- Windows. The app talks to `mumu-cli.exe`, reads the registry through `winreg` and calls DPAPI through `ctypes.windll`, none of which exist elsewhere
- [MuMu Player 12](https://www.mumuplayer.com/) with Clash of Clans installed, running at 1600x900
- A Gemini API key, entered in the app's settings tab. It answers three things and nothing else: the tactic for each battle, where the walls and buildings are on the map, and which building a menu belongs to. Without one the attack loop falls back to a fixed tactic, the wall and upgrade loops sweep for their targets instead, and the collector, builder, hero and donation commands never ask it anything in the first place

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

## 🎮 Playing from the terminal

The window is one way in. The other is a sub-command, which runs the same loops with no window at all, and this is how the game is usually played, because it leaves the terminal free to watch the log.

### Attacking

```bash
ai_coc attack                    # one battle
ai_coc attack --repeat 5         # five in a row
ai_coc attack --repeat 0         # keep going until a storage fills up
ai_coc stop                      # stand down after the battle in progress
ai_coc attack --restart-every 0  # skip the scheduled emulator restart this run
ai_coc attack --stop-at 0        # attack however full the storages are
```

**The emulator is restarted every so many battles**, because MuMu drops frames after running for a while and nothing short of a restart clears it. How many sits in the settings file (`restart_every`, 50 by default) rather than being hard-coded, because that number is whatever a given machine turns out to need — `--restart-every` overrides it for one run, and `0` there turns it off the same way the loot flags do. It counts battles rather than rounds, so a night mostly spent searching for an opponent does not spend restarts on an emulator that has barely been working.

`stop` marks `~/.ai_coc/state.json` and returns at once. The loop reads it between battles and between opponents, never mid-battle, so the worst case is one more battle: abandoning one halfway would leave the army on the field and the game on a screen the next run cannot get home from. With nothing running it says so, rather than leaving a request nobody will take.

Loot thresholds come from the settings file, and can be overridden for one run. Passing `0` is different from leaving a flag out: out means "use the configured value", `0` means "take this threshold out entirely":

```bash
ai_coc attack --min-gold 800000
ai_coc attack --min-gold 0 --min-elixir 0 --min-dark 0    # attack whoever comes up first
```

A run can keep everything it looked at, which is what makes a battle worth arguing with afterwards:

```bash
ai_coc attack --record                # every frame the loop reads, named for what it was asking
ai_coc attack --record --shot-every 4 # plus one frame every four seconds
```

A tactic is a file rather than a set of constants, so a battle worth repeating can be repeated and one worth arguing with can be edited. Replaying one calls Gemini not at all:

```bash
ai_coc attack --plan-out used.json    # write down whichever plan actually ran
ai_coc attack --plan used.json     # play that one again, edits included
```

### Spending what you farmed

```bash
ai_coc walls                          # buy wall upgrades until the storages will not stretch
ai_coc walls --keep-elixir 2000000    # leave that much behind to train an army with
ai_coc upgrade                        # put idle builders on the dearest upgrade affordable
ai_coc hero                           # what raising each hero next would cost
ai_coc hero --upgrade duke            # put a builder on that one
```

A wall upgrade needs a free builder and hands it straight back, so `walls` stops and says so when every builder is busy. `hero` reads by default and only spends when told which hero, because which one is worth a builder is a judgement about how the village plays.

### Keeping the village going

```bash
ai_coc collect                        # empty every collector that has something waiting
ai_coc builders                       # what each builder is on, and how long is left
ai_coc worker                         # the same for whichever village is up, without sailing
ai_coc lab                            # what that village is researching, and how long is left
ai_coc status                         # worker, lab, storages and the shield in one pass
ai_coc donate                         # give troops to whoever in the clan is asking
ai_coc donate --dry-run               # walk the whole path and stop before giving anything
```

### Getting the game up

Every other command assumes the game is already running and brings it up if it simply is not. What it cannot fix is an emulator or a game that is up and no longer answering, which is what these are for:

```bash
ai_coc launch                         # start the emulator if it is down, bring the game up
ai_coc launch --restart game          # relaunch the game, leave the emulator alone
ai_coc launch --restart emulator      # restart the emulator, then bring the game up again
```

### Looking at what it sees

```bash
ai_coc capture --count 30             # a burst off the live game, into this run's own folder
ai_coc <any command> --label baseline # name this run's folder so it is findable later
ai_coc read shot.png                  # what each reader makes of one frame
ai_coc export                         # the whole village out of the game, named, as JSON
ai_coc export --table                 # the same, drawn as a table for a person
ai_coc export --last                  # the last one off the disk, without touching the game
ai_coc view --zoom out                # put the camera back where every coordinate was measured
ai_coc world                          # which of the two villages the game is on
ai_coc world --go day                 # sail there; already being there does nothing
ai_coc attack --world night           # attack the builder base instead of the home village
```

The game keeps two villages — the home village and the builder base — and reopens on whichever one it was closed on, so `world` is worth asking before anything that assumes one of them. Reading it is a single screenshot: no tap, no swipe, no camera move.

`read` is the quickest way to answer "did it misread the screen, or did the tap miss?" It prints what every reader got from one frame: the loot panel, the storages, the card row and the builder panel.

### Where a run leaves what it saw

Every run gets a directory of its own, whichever side of the app started it:

```
~/.ai_coc/logs/2026-08-29-011423-attack/
├── run.log        # this run's log, and nothing else
├── result.json    # what the command answered
├── plans.jsonl    # ai_coc attack only: one line per round, the tactic it played
└── frames/        # only with --record
```

The first line of every run says which directory it is, and the name is `<when>-<what>` so a listing reads as a history. `--label` adds a third part on any command (`2026-08-29-011423-attack-baseline`) for a run you want to find again. There is no second file mixing every run together — `grep -r ~/.ai_coc/logs/*/run.log` answers across runs and tells you which one each hit came from.

**Frames are kept for a week; the logs are kept forever.** A run's `frames/` is deleted once it is older than that, and nothing else in the directory ever is. The split is worth stating because it is lopsided: on this machine 11 456 recorded PNGs came to 27.2 GB while every `run.log`, `result.json` and `plans.jsonl` together came to 4.6 MB. Copy anything you want to keep longer out of there.

## ⚙️ Settings

`~/.ai_coc/config.json` is read by both the window and the terminal, so a run plays the same way from either side:

```json
{
  "thresholds": {
    "min_gold": 500000,
    "min_elixir": 500000,
    "min_dark": 5000
  },
  "stop_at": 90,
  "restart_every": 50,
  "gemini": {
    "main": {
      "model": "gemini-3.5-flash",
      "base_url": "",
      "thinking_level": "low"
    },
    "lite": {
      "model": "gemini-3.5-flash-lite",
      "base_url": "",
      "thinking_level": "low"
    }
  },
  "ui": {
    "jobs": {
      "collect": false,
      "donate": false,
      "upgrade": false,
      "walls": false,
      "attack": false
    },
    "cycle_minutes": 10,
    "live_view": true,
    "record_frames": false
  }
}
```

That is the whole file. **A run that finds no file never creates one** — it uses exactly these values and leaves the disk alone — so the file appears the first time the window writes it, which is 儲存設定 or either switch over the preview. Once it exists it is kept honest on every load, by whichever side loads it first: a key the app no longer reads is dropped and a key the file never had is filled in, so what is on disk is always a statement of what the next run will do rather than of what was saved once.

- **thresholds**: who is worth attacking. Set them too high and a run skips dozens of opponents without ever starting a battle
- **stop_at**: how full every storage has to be before a run stands down, as a percentage. **One number for both villages**, because the loop reads each storage's real ceiling off the game — tap a storage bar and it writes 最大儲存量 on the spot. Every storage has to reach it, not just one of them: a battle brings home three, so one at the ceiling is no reason to stop earning the other two. On the builder base the elixir cart counts as one more: past a full elixir storage it keeps banking each battle's defence reward, so a run there stands down only once the cart has reached the same share of the ceiling written on it. `0` never stands a run down, and a storage whose ceiling would not read is left out of the count. `--stop-at` overrides it for one run, `0` included, which is what a test battle against a village that farming has just filled needs
- **restart_every**: how many battles to fight before restarting the emulator and the game. MuMu drops frames after running for a while and nothing short of a restart clears it, which is a property of the emulator rather than anything this code can measure — so this is the one number in the file that is somebody's observation. `0` turns it off; `--restart-every` overrides it for one run
- **gemini**: which model answers each kind of call. `main` is asked once per run against a whole screenshot — the attack plan, the target finder — so nothing there is racing anything and the better model is simply the right one. `lite` is asked once per candidate against a cropped strip, which is a classification rather than a judgement and is where a cheaper model earns its place. `base_url` empty means Google's own endpoint. **The API key is not here**, deliberately: it lives in the DPAPI store beside this file, so there is no slot in a plaintext file that looks like the place to put it
- **ui**: what only the window reads, and the last thing that was not in this file. It lived in the Windows registry, where no editor opens it and no `ai_coc` sub-command can reach it, on the rule that a setting no terminal command asks for does not belong in a shared file — which is how the one part of this app nobody could edit by hand turned out to be the window's own behaviour. `jobs` is which of the five commands the window's cycle round-robins, named for the sub-commands themselves, so `"walls": true` runs the same job `ai_coc walls` does, one pass of it per turn rather than until the storages will not stretch. `cycle_minutes` only ever paces a cycle that found nothing to do, because a finished job queues the next pass straight away. `live_view` is the live preview in the 主控 tab, and `record_frames` is the checkbox form of `--record`

There are no ability or spell timings here any more. They were a table of per-hero constants, and editing them meant guessing how long an army takes to walk across a village nobody had looked at — which is the planner's job, done with the village on screen. Every clock lives on the plan now: see `plans/flat.json` for the shape, and `--plan` to replay one.

Everything else lives in `~/.ai_coc`: the account JSON `ai_coc export` writes, the run logs, the DPAPI-protected key file, and `state.json` — which command is driving the emulator, its pid, and its log directory. It is kept after a run ends rather than removed, so the record says what the last run was instead of going blank; deleting it by hand asks whichever run wrote it to stand down, which is the way out when whatever started that run is gone.

## 🤝 Contributing

Setup, architecture, packaging and the CI layout live in [CONTRIBUTING.md](https://github.com/Mai0313/ai_coc/blob/main/.github/CONTRIBUTING.md).

## 📄 License

MIT, see `LICENSE`.
