# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Windows-only PyQt5 desktop app that drives Clash of Clans inside MuMu Player 12 or LDPlayer 14 (雷電). **The parsers read the screen, and Gemini answers five narrow questions**: where the walls or buildings are on the map (`GameRunner._spotted`), which building a menu belongs to (`upgrade`'s lite tier), what the jobs on a builder or laboratory panel are (`read_plate_jobs`, lite tier), how to attack a home village opponent that already passed the loot thresholds (`_plan`, once per battle), and how to attack a builder base (`_night_plan`, once per stage). Everything else — prices, card counts, storage bars, which village is up, whether a drop landed — is pixels. The 主控 tab runs the same `commands.*` functions the CLI does.

**Where the reasoning lives.** The code says what is. The comments and docstrings in `src/`, and the tests that pin each threshold, say why a value is what it is and which measurement set it. This file holds the rules and the map that no single comment can. Before changing a constant, read its comment and grep `src/` and `tests/` for its name: most were set by a battle that went wrong, and changing one back reopens that bug. A new measurement goes into the comment beside the constant it sets, not here.

## Driving the game

Six skills under `.agents/skills/` own the judgement of driving the game, and they are the only copy of those rules; a rule written here as well would drift.

- **`farm`** — run the loops on both villages and judge them. By default `stock_full` ends the run with a report; the loot is spent only when the user asked for that.
- **`spend-loot`** — walls, buildings and heroes, once the user asked; walls must be named even then.
- **`watch-and-fix`** — farm and watch the same run, fix whatever breaks between launching the game and landing back on the village, and take the fix through to a merged PR.
- **`build-feature`** — find what the game still does by hand, and automate it.
- **`watch-upgrades`** — read the builder, laboratory and shield timers, and come back when they run out.
- **`repair-emulator`** — bring the emulator itself back when it will not come up, without changing code, and hand it to the user when it cannot be fixed from here.

`.agents/skills/farm/references/running.md` owns the mechanics the skills that run a loop share: which commands run in the background, why a subagent blocks instead, what `result.json` holds, how a loop is stopped and when that is safe.

**Every session farms while it works, through one subagent.** This is the user's standing instruction: thinking, edits, test runs and reviews are all time the emulator could spend on a battle, whatever the task. Spawn **one** `farm` subagent — in the background, named so it can be messaged, **one model step down and no further**, with a lowered thinking budget where the runtime offers one (how low is the spawner's call), and told it is a subagent so it blocks the way `running.md` says. Both villages are farmed. The smaller model is enough because the skill carries the judgement in writing. The main session keeps its context for the change.

**`watch-and-fix` is the one exception, and only to the delegating half of that rule**: it still farms, but there the loop is the task, so that skill decides whether to drive the loop itself or hand it to a `farm` subagent, and a user who says 「你親自監督執行」 settles it.

**Taking the game for a live test.** There is one emulator, so a test and the loop cannot both drive it. Take it whenever a test needs it and give it back afterwards, without asking either time:

- Take it with `ai_coc stop`. The loop finishes its battle and exits, and the subagent, seeing a stop it did not issue, reports instead of restarting. **That report arriving is the signal that the screen is free**, not `stop` returning.
- A stop that lands between two of the subagent's commands is erased by the next one's claim, so no report after a battle's length means stop again. `state.json` still reading `stopping` means the loop has not reached a seam yet.
- A test battle faces the village the farming just filled, which `stop_at` stands a run down on, so tests pass `--stop-at 0`.
- Give it back by messaging the same subagent, which still holds the baseline; a fresh one, told where the last stopped, if it is gone. Say so in the reply, because the user cannot see either agent's output.

**Look before driving.** Before the first command of any piece of work, read `~/.ai_coc/state.json`. On `running` or `stopping`, check the `pid` with `Get-Process -Id`. A pid that is gone is residue that the next claim overwrites; one that answers is somebody's run, and starting a second interleaves taps on the one display.

**Two drivers that file cannot show** (both bit on 2026-09-21):

- A **monitor** session re-running `ai_coc worker` reads `idle` between runs, and `ai_coc stop` cannot reach it, because what loops is an agent rather than a process. Only killing that agent stops it.
- **The user playing on their phone** logs the emulator out. A `builders` run met that screen, spent 61 seconds pressing `back` and answering 取消, and failed with 畫面沒辦法回到村莊; each retry's `ensure_coc` took the session back and logged the user out again. A run that cannot reach a village while somebody might be playing is a reason to stop and say so, not to retry.

**Check what subagents leave behind yourself; their word is not evidence.** Before calling a task finished: `state.json` back to `idle`, no `ai_coc` process, no shell left polling. Say out loud what stays deliberately; kill and name the rest. On 2026-09-16 four `until [ -s "$RUN_DIR/result.json" ]` shells had waited 35 hours on a run that had crashed, while the subagent twice reported them cleaned up. A wait with no upper bound cannot end on its own; `running.md` owns what a wait looks like.

## Development flow

Read `gh-dev-flow` before starting. In this repo the path always ends the same way: open the PR as a draft, run `code-review` over the branch and fix what holds up, then merge once CI is green. Merging on green needs no further approval.

## Commands

```bash
uv sync --group test                 # the suite needs the `test` group
uv run pytest                        # full suite (xdist, coverage gate, JUnit/XML into .github/reports)
uv run pytest --no-cov tests/test_core.py::ScoutTests::test_loot_is_read_over_grass   # single test; the gate is in addopts and a partial run cannot meet it
make fmt                             # pre-commit: ruff, mdformat, codespell, ty, gitleaks, uv-sync/lock
make gen-docs                        # rebuild docs/ from the READMEs and the source
```

`uv run ai_coc` opens the window and takes no flags. The sub-commands run headless, and they are how the game is worked on; a smoke test against the live game is one of them run from a terminal. `cli.py`'s `_parser()` is the full reference.

```bash
uv run ai_coc attack --record            # one attack pass, every frame it reads kept
uv run ai_coc attack --plan tuned.json  # play a written tactic, no AI call at all, on the village it was written for
uv run ai_coc attack --plan-out used.json  # write down whichever plan actually ran
uv run ai_coc attack --min-gold 0 --min-elixir 0 --min-dark 0   # attack whatever comes up first
uv run ai_coc attack --repeat 0            # keep attacking until stopped
uv run ai_coc attack --restart-every 0     # skip this run's scheduled emulator restart
uv run ai_coc attack --stop-at 0           # attack however full the storages are, which a test against a farmed village needs
uv run ai_coc attack --record --shot-every 4   # plus a frame every four seconds
uv run ai_coc stop                        # ask whatever is driving the emulator to finish this battle and stand down
uv run ai_coc walls                       # spend the storages on walls until they will not stretch
uv run ai_coc walls --keep-elixir 2000000  # leave that much behind for an upgrade already chosen
uv run ai_coc walls --at 260,260          # start from this wall, skipping the village scan
uv run ai_coc collect                     # empty every collector that has something waiting
uv run ai_coc builders                    # what each builder is on, and how long it has left
uv run ai_coc stock                       # what this world's storages hold, as a share of what they take
uv run ai_coc worker                      # this world's builders: who is on what, and how long each has left
uv run ai_coc lab                         # the same for this world's research slots
uv run ai_coc status                      # worker, lab, stock and the shield, off one village in one pass
uv run ai_coc upgrade                     # put the idle builders on the dearest upgrade affordable
uv run ai_coc upgrade --at 900,430        # this building is a candidate, skipping the search
uv run ai_coc upgrade --only 金礦          # only a building whose name contains this
uv run ai_coc donate                      # give troops to whoever in the clan is asking
uv run ai_coc donate --dry-run            # walk the whole path and stop before giving anything
uv run ai_coc hero                        # what raising each hero next would cost
uv run ai_coc hero --upgrade duke         # put a builder on that hero
uv run ai_coc view --zoom out             # put the camera back at the far zoom everything was measured at
uv run ai_coc world                       # which of the two villages the game is on
uv run ai_coc world --go day              # sail to that one; already being there does nothing
uv run ai_coc launch                      # start the emulator if it is down, and bring the game up on it
uv run ai_coc launch --restart game       # relaunch the game alone, leaving the emulator running
uv run ai_coc launch --restart emulator   # restart the emulator, then bring the game up again
uv run ai_coc read <png>                  # what each parser makes of one frame
uv run ai_coc export                      # get the whole village out of the game, named
uv run ai_coc export --table              # the same, drawn for a person instead of piped
uv run ai_coc export --last               # the last one, off the disk, without touching the game
uv run ai_coc capture --count 30          # a burst off the live game, into this run's own directory
uv run ai_coc walls --label tuned         # name this run's directory; every sub-command takes it
uv run ai_coc probe --record             # spend a battle measuring the real boundary
uv run ai_coc bounds --record            # spend a battle measuring where the map ends
```

**Real battles are cheap evidence, and one battle is not evidence.** Training is instant and free and the search fee is negligible, so a battle that goes badly or deploys nothing costs only its wall-clock time, and a run spent entirely on probes is normal. Run five rounds back to back rather than reasoning from one sample: the opponent varies more than anything being changed. A round that fought nothing for a reason other than the army waits `IDLE_REST` before walking the menus again.

**Every run gets its own directory** (`RunLog`, `~/.ai_coc/logs/<when>-<what>[-<label>]/`), from the CLI and the window alike, and the first log line names it. It holds `run.log`, `result.json`, `frames/` when asked for, and for `attack` a `plans.jsonl` with one line per round carrying the tactic that round played; `jq -c 'select(.round==2).plan'` pulls one back out for `--plan`. Nothing else records which line a round drew, so debug a bad battle from `plans.jsonl` before opening frames. There is no merged log: the directory listing is the history, and `grep -r ~/.ai_coc/logs/*/run.log` searches across runs. `--label` names the directory on every sub-command; anything not a letter, digit, underscore or hyphen is replaced.

**Frames are the only thing worth culling.** Measured over 438 runs, 11 456 PNGs took 27.2 GB against 4.6 MB for every log, result and plan file together. `FRAME_RETENTION_DAYS` culls `frames/` alone, rides on `RunLog.open`, and never fails the run it rides on. At the end of a task, once what the frames were evidence for is settled and nothing is running, delete them; keep the logs, which are this machine's history. Wiping `logs/` whole throws that history away, so propose it rather than doing it quietly.

`--record` fills `frames/` and is off by default, because every frame is a PNG encode on the emulator; the window has the same switch. Each capture is named for what the loop was asking (`0006_scout`, `0010_pass`, `0018_dropped`), and `before-drop` / `dropped` are a pair for settling whether a hero went down. `--shot-every` adds `tick_00012.3s.png` frames between the loop's own captures.

**Releases are built in CI only.** Pushing a `v*` tag runs `build_release.yml`, which publishes the wheel to PyPI and attaches a PyInstaller Windows build. `scripts/gen_version_file.py` writes the gitignored `version_info.txt` just before PyInstaller runs. The build defaults to `--onedir` (1.4-3.1 s to the first log line, against 6.6-7.0 s for `--onefile`, which unpacks itself on every launch); the dispatch form's `package_mode` switches it, for one 76 MB file instead of a 174 MB folder.

## Architecture

`src/ai_coc/cli.py` is the entry point behind both console scripts (`ai_coc` and `cli`) and the PyInstaller build. PyInstaller runs it as `__main__`, so its imports stay absolute and it keeps an `if __name__ == "__main__"` block.

The layers are directories. Imports across layers are absolute (`from ai_coc.models import …`), within a layer relative (`from .adb import …`); Ruff's `TID252` enforces it.

- **UI and orchestration** — `ui/main_window.py` (`MainWindow`, the tabs and the automation cycle), `ui/workers.py` (thread-pool workers, the log handler), `ui/render.py` (log records to HTML). The loops are Qt-free so they can be driven and tested without the window: `ui/attack.py` (`AttackRunner`), `ui/world.py` (crossing, the loot cart), `ui/plates.py` (the top-row plates), and the `GameRunner` subclasses in `ui/walls.py`, `ui/upkeep.py`, `ui/clan.py` and `ui/hero.py`, over `ui/runner.py`. `cli.py` is argument parsing and `main()`; `commands.py` is the headless side it dispatches to. **New work belongs somewhere `commands.py` can call it**: a feature reachable only through a widget cannot be run against the live game.
- **Adapters** — `adapters/emulator.py` (what every emulator does the same way, over ADB), `mumu.py` and `ldplayer.py` (each one's own CLI: its instances and their lifecycle), `adb.py` (every ADB call), `ai.py` (Gemini), `secrets.py` (DPAPI), `config.py` (the settings file), `mapping.py` (the community `data_id` table), `clipboard.py` (the Windows clipboard).
- **Pure parsers** — `parsers/frame.py` decodes the 1600x900 frame and raises on any other size. Screens: `village.py`, `scout.py` (loot panel, card row, storage bars, which screen is up), `building.py` (building menus, prices, dialogs), `home.py` (collector markers, the builder panel, the plate row), `boundary.py`, `field.py`, `clan.py`, `hero.py`, `world.py` (which village), `settings.py` (the pages the village export sits behind). Two are machinery that every other reader is built from: `glyphs.py` reads numbers against ten bit-pattern digit templates, and `regions.py` finds and measures connected patches of one colour. A screen module keeps only its own constants.
- **Data shapes** — `models.py` holds every Pydantic model in the project.
- **Prompts** — `prompts/*.md`, one file per prompt, loaded once into `PROMPTS` and filled by `render`. Each is a `str.format` template, so a literal brace is doubled.
- **Plans** — `plans/*.json`, written tactics; `flat.json` and `night_flat.json` are the no-key defaults.

### Cross-cutting rules

**Everything is 1600x900 at the game's far zoom.** Every coordinate was measured there. Gemini answers percentages, converted with `x_pct * 16, y_pct * 9`. MuMu's `wm size` reports 900x1600 (LDPlayer's 1600x900) while the screenshot arrives as 1600x900 on both.

**The parsers read the UI the game paints on top, never the thing underneath.** Walls, buildings, heroes and troops are repainted at every level and by theme updates; buttons, icons, plates, numbers and borders are not. A wall is recognised by being upgradeable with gold *or* elixir, a hero by its card banner's flat colour, a resource by its icon. The rule binds the parsers, not a vision model making a one-off identification.

**One feature is rarely enough.** Many detectors pair two readings because either alone matched something else in recorded frames (`night_attack_menu`, `settings_open`, `export_row`, `loot_cart_open`, and `uncovered`'s battle test). A threshold is drawn from what was measured on both sides of it, over recorded frames and the committed fixtures where they exist; its comment says how wide the margin is and what it was measured on.

**Spending is guarded in code, by structure where it can be.** The wall loop only taps positions counted leftwards from the elixir drop, so the wall ring and every gem button on that row are unreachable by construction. Where a button cannot be kept out of reach, the parser refuses it before any caller sees a price: the hero hall reads `GEM_GREEN` off the button's icon because 立即完成 spends gems from the same place as 升級, and two buttons in the building row carry icons that read as a resource — 加速所有同類項目 (a magic item) and the wall ring — and both sit on a **blue plate**, which is refused. Any new capability that spends resources needs its own guard of one of these kinds; a prompt or a skill line is not one.

**Which village is up** (`current_world`, `parsers/world.py`). The two villages are separate maps joined by a boat, and the game reopens on whichever it was closed on. The home village carries a 護盾 plate that the builder base cannot have, so the reading is **how far the row of blue `i` badges reaches, not how many badges there are**: the home village's badges sit at x 516, 719 and 933, the builder base's at 628-644 and 830-846. A gem shower covers a plate for seconds, and counting badges read the home village as the builder base and sailed the attack loop away. One badge answers None unless it is the shield's; battles, dialogs and loading screens read none. `read_stock` answers on both villages (the builder base's gems bar reads as `dark=410152`), so it can say a village is up but never which one. Rejected: the builder base's trophy plate (breaks once the account is ranked) and counting resource rows.

**`back` is only pressed on a frame that is not a clear village** (`GameRunner._home`, `uncovered`). On a clear village `back` raises 確定退出遊戲嗎, whose 確定 is the same green in the same pixels as the one that pays for an upgrade, so `game_dialog` reports both buttons and picks neither; only the caller knows which question it asked. **`uncovered` also never presses at a battle**, where `back` aims at 放棄, because its callers hand it whatever is on screen, a game left mid-battle by a killed run included; `_home` has no battle filter. `uncovered` takes a frame for a battle only when a card row (`card_groups`) *and* the plate in the corner (`in_battle`, `ABANDON_BOX`) both read, because the 探礦者 sheet and the shop's 外觀 page have card-shaped rows and deadlocked runs that read them as battles; `in_battle` alone reads only the plate. `_dump_leftovers` and `_cast` ask `in_battle` as well, so a tactic that outlived its battle stops tapping. Two windows read False over a live battle: the builder base draws no plate during its opening countdown, and a popup dims the plate away. The countdown was tested live: `back` there raises 確認退出遊戲 rather than surrendering, and `uncovered` never answers a dialog.

**Getting back to the village** (`GameRunner._home`, `ui/runner.py`). Four things block it and each wants its own answer: a game still loading is waited on, a panel gets `back`, a dialog gets 取消, and a dropped session gets the game restarted rather than a tap on 重新登入遊戲 (連線已中斷 is the same sheet, read by `idle_disconnected`). `_seen_village` decides whether an unreadable frame gets the launch patience. A dialog has to be answered, since its dimming is what stops the storage bars reading.

**The camera is put back before the storages are read** (`GameRunner._settle_zoom`, `_settled`). A zoomed-in camera can put bright ground behind a translucent storage bar and fuse its digits, so `read_stock` fails and `_home` circles `back` → 確定退出遊戲嗎 → 取消 until it gives up. It runs once per run and is cleared on a restart. Either `current_world` or `read_stock` counts as a village for the pinch, but only `current_world` says *which* village the park should push toward, so a frame only the storages recognised is pinched and not parked, with a warning. Known and open: such a frame hands the builder base's storages back as the home village's, and `_opened` tests the village with `read_stock` alone, so `walls` or `upgrade` could sweep the wrong map. No recorded frame has taken that branch (4 304 swept), so it is a missing guard nobody has watched fail.

**A server outage is waited out, not tapped at** (`loading_screen`, `AttackRunner._wait_out_loading`). 正在載入 with the bar stuck at 1% has lasted 25 to 40 minutes, and neither kind of restart shortened it. The screen is read off the bar's plate and its purple fill. `_open_attack_menu` polls it every `SERVER_POLL` up to `SERVER_POLLS` (45 minutes), checks the state file inside the wait, and waits once per load: a load that drops back onto it ends the round `server_flapping`. `uncovered` and `_home` never press `back` at it.

**Camera and zoom** (`park_camera`, `view_shift`, `ai_coc view`).

- A pinch sets the scale and does not move the camera. `park_camera` pinches out first and then swipes into the corner until `view_shift` says the picture stopped moving; a fixed swipe count left the camera 130 px short and cost minutes of tapping water.
- `view_shift` slides one frame over the other along the drag. It answers None when its box is flat and every offset ties; the scores are a dict keyed by offset so that a small drag's duplicate offsets do not read as a tie. None means keep swiping to `park_camera` and leave the record alone to `AttackRunner._pan`.
- Zoomed in, the camera runs off the village into the map's border, so `view --zoom in` never parks. `view` parks only on a village, because a swipe with a card selected deploys troops, while the pinch is safe on any screen.
- The plate panel (`plate_panel_open`) floats over the village, reads as one to every village test, and pins `view_shift` at (0, 0), so `park_camera` presses it shut first (two taps at most) or stands down. Known hole: an idle panel has no running rows to find, so a park under one is unchanged.
- From fully zoomed in, two `out` pinches are needed: the game caps what one pinch may do, whatever the finger travel.

**Pinch mechanics** (`AdbController.pinch`, `touch_device_for`, `gesture_script`). `input` has no two-finger gesture, so a pinch is multi-touch events written to one input device node. `touch_device_for` resolves that node from `dumpsys input` (the display's physical id → the InputReader device → its EventHub path), keeping only a node `getevent` lists as multi-touch, because LDPlayer binds a `VirtualMouse` to the display ahead of its touchscreen; sending to every node lands two fingers on MuMu's launcher displays and switches away from the game. Whether a node's axes are the screen's swapped is read off its own ranges (`touch_devices`): MuMu's are (a point goes down as (y, x)), LDPlayer's are not. `BTN_TOUCH` is required, and so is `ABS_MT_PRESSURE` on a node whose pressure axis has a range (LDPlayer's is 0 to 2; MuMu's lists 0 to 0 and must be sent none): without it Android reads the finger as a hover and the game ignores it, which went unnoticed only while a real mouse click had left a pressure behind. The events are packed behind one redirect because opening the node cost 31 of every 33 ms a `sendevent` took; `PINCH_GAP` puts the pacing back so the game sees a drag.

**Crossing** (`ui/world.py`, `ai_coc world --go`). Park the camera, tap the boat's measured spot, and let `current_world` say whether it worked; nothing recognises the boat, which events redress. A spot that did not sail may have opened a building, so it is followed by `uncovered`, as is arriving on a covered village. The home village's spots stay up and left of the measured one (the clan capital's vessel is below and right); the builder base's stay left (the button column overlaps the stern).

### Attack loop (`ui/attack.py`, `parsers/scout.py`, `parsers/glyphs.py`)

攻擊 → 尋找對戰目標 → 攻擊 → scout → deploy or skip → 回營, driven by `AttackRunner`. **Nothing in the loop asks Gemini what is on screen.** The scout screen expires after 30 seconds and then forces the battle, so `read_scout` reads the loot against digit templates in about 0.15 s. It answers None (正在搜尋對手, or a panel it cannot read), a view with `can_skip=True`, or one with `can_skip=False` (read off the 下一個 button's orange).

**Reading the loot panel.**

- **`read_scout`'s None is overloaded**, and searching and unreadable are opposite instructions. The end of a battle is `_battle_ended`, which reads the green 回營 button, never a None.
- A row fails when a glyph in its *middle* will not match (skipping one turned 1 047 758 into 104 758); only a poor match off the right end is dropped. Touching digits wider than 18 px are cut where both halves read best, and a cut is believed only when both halves land close. A span too wide to be a digit is `NOT_A_GLYPH`. Ink shorter than `SPECKLE_ROWS` is dropped as a patch before columns are cut. `signature` measures a glyph from its tallest band of rows.
- A popup dims the whole screen, so a row that never reached `LOOT_INK_BRIGHTNESS` is retried at `_dimmed_floor` (85% of the row's peak). A bright theme bleeds in from the other side, so `_lit_floor` only ever raises the floor.
- A new opponent fades in, and a frame caught mid-fade reads half-drawn digits and an absent 下一個 — which means "forced" and commits the army. `_scout` refuses a frame whose panel peak (`panel_peak`) is under `PANEL_DRAWN_BRIGHTNESS`. The check lives in `_scout` rather than `read_scout`, because the battle poll must still read popup-dimmed panels.
- The storage bars (`read_stock`) are every loop's home village test, so a failure there takes every loop down. They have their own saturation ceiling (`STOCK_INK_SATURATION`, against the fill gloss). The unfilled part of a bar is translucent and the digits are right-aligned, so `_read_row` drops a poor match off the *left* end, and a trimmed row must pass `STOCK_TRIMMED_TOLERANCE`, which keeps a battle screen's text from reading as a village.

**Around the battle.**

- `army_strength` reads the camp off 我的軍隊 before the search fee is charged. Training is instant, so an army under `MIN_ARMY_RATIO` (0.5) cannot fill and ends the whole series (`army_short`) instead of resting. It is read twice, because a poor leading glyph turns `305/305` into `(5, 305)`.
- `_wait_for_battle` waits out the countdown, since a drop before it is ignored silently, and hands back the first battle frame, which is the first one carrying the boundary.
- Every battle opens with a pinch out (`_settle_zoom`), because the game reports no zoom level. The boundary's height before the pinch warns on a clipped, zoomed-in home village. The builder base is smaller at every zoom, so the warning is the home village's alone, and a zoomed-in builder base goes undetected: no recorded frame has one to draw a floor from.
- `_wait_out_battle`'s False is split two ways: `_seen` separates a panel that never read from one that never moved, and `_deployed` (set only where the loop watched a card give something up) separates an army that never left its cards from one that went in and took nothing.
- **The result screen** (`RETURN_HOME_GREEN`, `_leave_result`). The game draws 回營 plain and lit; the threshold sits between the lit button and the highest non-result frame, and the event reward pages' green ticks fall under it. `_leave_result` re-reads before pressing `back`, and `_open_attack_menu` hands any frame that is no village to `uncovered`, which clears a popup left over the result screen and still never presses at a battle.

**The plan** (`_plan`, `AttackPlan`, `AttackStep`, `prompts/attack_plan.md`).

- One Gemini call per battle, after the thresholds have passed, so a skipped opponent costs nothing. `--plan` plays a written plan with no call, `--plan-out` writes whichever ran, and `plans.flat()` (`plans/flat.json`) is what a run with no key or a failed call gets.
- A plan is an ordered list of steps, played in order, and it carries every clock: where each hero goes (`hero`), when each ability fires (`ability`), where each spell goes and when. `act`, `who`, `at` and `seconds` have no defaults, because Gemini leaves out fields that have one. Nothing in `config.json` holds a clock.
- A `wait` counts from the previous move finishing, so nothing depends on how long the call or the boundary read took.
- `hero` steps are matched to the cards left to right (an upgrading hero's card disappears) and are skipped when the cards run out; one naming `unknown` sends every hero still in hand. Which one-off card is the siege machine comes from the plan having a `siege` step, not from a health bar, since the game draws one over the siege machine too.
- The prompt asks for the side with the **heaviest** defences (troops take defences head on) and for rage on the heaviest defence group, where the army ends up. Moving rage onto measured motion (`_onto_army`, removed) landed bottles planned for 12, 15, 12 and 12 seconds at 18, 22, 20 and 19.
- The plan decides the order of the drops. The order the loop used to fix was siege machine, troops, then heroes straight behind; before that was tuned the siege landed 7 seconds in, the last troop at 44 and the heroes at 67, against 4, 8 and 12 after.
- One point per bottle. `BattleRow.freeze_count` exists because slicing freeze points by cards rather than bottles put the whole cargo on one spot. A spell covers a 240x120 ellipse; `spaced` pushes a crowded bottle to the edge of its neighbour's footprint (`NUDGE_CLEARANCE`, at most `NUDGE_REACH`) instead of dropping it.
- `planned_line` rejects a line whose midpoint is within `MIN_LINE_RADIUS` of the centre, since `push_out` cannot move it; the plan is kept, and its own `deploy_from` flank goes first among the four named flanks that follow.

**Deployment.**

- The whole tactic goes down blind in bursts (`tap_many`), then `_settle_drops` reads one frame inside the first pause long enough to hide it (`CHECK_BUDGET`) and resends what the game refused (`_drop_singles`, `_spread_troops`). A refused drop costs nothing, because the card is not consumed. Later pauses check the battle is still on, since some card slots sit where the result screen draws 回營.
- **A drop is judged by the card, never by the screen.** 你無法在紅線區域內派遣部隊 and the other red banners share one colour and are not given reliably. `card_drained` reads the `xN` corner repainting, which happens on the first troop or bottle to leave, so whether a card is *empty* is `live_cards`; a hero landing is `field_units`, reading the green health bar, told from grass by how little blue it has (`HERO_BAR_MAX_BLUE`).
- Troop passes walk the line twice and in `DROP_STRIDE` order, so a card that runs dry mid-pass has spread its troops. A counted card is tapped its count plus one, capped by `DROPS_PER_PASS`, because `card_count` can fail high.
- The card row: `card_groups` is valid only on a full row, so `_deploy` reads it once, off the frame captured after the pinch and before anything is dropped. Dark artwork splits a card, and `_rejoined` joins pieces that together make one card. The empty dashed slot has no level badge (`_badged`). `counted_cards` separates troops and spells from heroes and the siege machine by the whiteness of the `xN` corner, with the line at 0.16 because 飛龍公爵's artwork reaches 0.1386 and builder base troop cards go down to 0.1835. `freeze_cards` tells freeze from rage by its cyan.
- Timing constants are measured, not cautious. ADB was never the cost (a shell round trip is 47 ms, five chained taps 109 ms, a `screencap -p` 0.6-0.8 s): `TAP_GAP` and `SINGLE_DROP_DELAY` are 0, `SPELL_SELECT_DELAY` 0.6, `DROP_SETTLE` 0.5, `HERO_SETTLE` 1.5.

**The map and the camera in battle.**

- `DEPLOY_BOUND` is the ground the game accepts a drop on, measured with `ai_coc bounds` rather than detected. `push_out` clamps to it, and every drop point goes through `clear_of_controls`, which keeps it inside `PLAYFIELD`, off the card row and off 放棄. `single_spots` drops a pushed point the game clamps straight back and tries the next spot.
- The game draws its own red stroke around the refused ground. `boundary_reach` reads it along one ray, and `fitted_line` bends a preset flank onto it through three anchors, because a two-point chord cuts back inside the diamond. It is an opening guess, and `_spread_troops` pushing the flank out when a pass drains nothing is the backstop. `ai_coc probe` measures how good the guess is: its first run agreed on 7 of 12 rays, and four of the five misses put the line inside the real boundary.
- `_settle_camera` centres the village before `_plan`. Known and not fixed: the frame it measures is still the scout screen, whose orange 下一個 button `village_box` does not mask, so the box read x 1500 against 1269 for the same village moments later and a run can spend two drags shoving a centred village 72 px. Moving the call onto a battle frame puts it after `_plan`, which would need a second coordinate frame. `_clear_flank` drags the village off the card row before a lower flank (`FLANK_ROOM`). `_pan` / `_panned` record every camera move that leaves the village off centre (not `_settle_camera`'s own, which ends centred), measured by `view_shift` along the drag, so the flanks and the plan keep pointing at the village. The loop never selects a card before a drag, since with one selected a swipe deploys along its path; that is also how a machine the game preselects at the start of a builder base second stage gets sent.

### Builder base (`AttackRunner.world`, `_run_night`, `_night_plan`)

The same runner plays both villages. An attack series takes its village from the first round that reads one, and ends `other_village` on two readings of the other village; it never sails.

- **Getting in**: 攻擊 → 開始進攻 → 立即尋找. No scout screen, no thresholds, no fee and no army check, since the army refills the moment a battle ends. `night_attack_menu` needs the green button *and* the cream panel behind it. The matchmaker waits for a live player (one search took five and a half minutes); `searching_opponent` reads its 取消, and a search past `SEARCH_PATIENCE` is cancelled and started again.
- **The card row** is the machine first and the troops after it; the troops are the cards with a count. `card_count` reports the builder base's `4x` as unreadable on purpose, because its digits come back as wrong digits within tolerance.
- **`live_cards` means the unit died there**, not that the card is empty. So `_spread_night` decides by whether anything has drained yet: a barren pass before any drain pushes the flank out, and one after stops the passes. It does answer "is the machine still alive" correctly, so a machine that died in stage one is not sent in stage two.
- Troops go down across `NIGHT_LANES` lanes (`night_drops`), outwards only, so splash damage hits fewer at once.
- The machine lands first, with `NightPlan.troops_after` as its head start. Its ability recharges all battle, so `_wait_out_night` taps every one-off card each `ABILITY_TAP` and captures every `ABILITY_POLL`. A machine that survived opens stage two preselected: `_send_selected` taps the field and checks `selected_cards`, since a damaged health bar does not read as one (`CARD_SELECTED_SPAN` lets `card_groups` see the wider selected card).
- A stage can end with no result screen; the card row being repainted marks it. A second stage is played when offered, never predicted.
- **The storage limits** are checked before the search, which can take minutes. See **Storage limits** below.

**The loot cart** (`collect_cart`, `loot_cart_open`, `loot_cart_ready`, `loot_cart_load`, `CartReport`). Gold lands in the storages; battle elixir goes into a 聖水車 beside the boat, and tapping it opens a sheet.

- The sheet is recognised by its orange plank in two boxes (the lower of the two readings), never by the 收集 button's green, which greys when the storages are full or the cart is empty (`locked_empty`). Both boxes stay clear of the button, which the game can draw lit.
- `loot_cart_load` reads `held / capacity` beside the button and is gated on the sheet being up, because the card `xN` corners sit in the same place. The sheet is closed on every path.
- The cart is the builder base's third storage: defence rewards keep paying into it after the storages are full, so full storages end a run only when the cart is also past `stop_at` of its own ceiling (`_visit_cart`, `_cart_has_room`). A look that cannot say how full the cart is goes again at once, and after three the storages decide alone. One schedule, the first round and every `CART_EVERY` battles, empties the cart while the storages have room and reads it once they do not.
- Judged on `read_builder_stock`, never `read_stock`.

### Storage limits

`AppConfig.stop_at` is one share for both villages, measured against ceilings read off the game (`storage_capacity`), so nothing typed in goes stale. Tapping a bar opens a 最大儲存量 tooltip; `AttackRunner._settle_ceilings` and `ScreenRunner.read_ceilings` read it once per run. The tooltip is a toggle, so a row that reads nothing is left alone. The builder base's third row is the gems bar, whose `+` opens the shop, and is never tapped. **Every watched storage has to be full**, not one of them (`StorageCapacity.full`). A resource with no ceiling is left out and neither stops a run nor holds one open, which is what the builder base's missing dark elixir relies on. A read where any tapped row fails is thrown away whole. `CAPACITY_TOLERANCE` sits well under the label's distance from any digit; at 40 the colon reads as a `2`.

### Finding things (`GameRunner._spotted`, `prompts/find_targets.md`)

Every loop that acts on the map finds its target in order: the spots the caller named (`--at`), then Gemini (`_spotted`, the `main` tier), then `_sweep`, a blind grid that takes about two and a half minutes and misses most of a village. Each spot is verified by opening it and reading the menu, so a wrong one costs about 1.7 s, and asking for more than needed is the right shape (`WALL_SPOTS`, `BUILD_SPOTS`, `HALL_SPOTS`). A point below `SWEEP_LIMIT` is dropped, because the previous selection's button row is there. A tap that misses opens whatever building stands there, and a full-screen panel swallows every tap after it, so the sweep and the named spots walk through `GameRunner._opened`, which checks the stop before each tap and backs out when the storages stop reading. Not every tap goes through it: the hero hall's search, among others, taps with `_after_tap` directly.

### Wall loop (`ui/walls.py`, `parsers/building.py`)

- **A wall is recognised by the one thing only walls do**: be upgradeable with gold *or* elixir. That is an elixir drop with a gold coin one `BUTTON_PITCH` to its left and the same price beside each. The drop is the anchor, and nothing counts rightwards from it, which is what keeps the wall ring out of reach. No button in that row sits at a fixed x: the row is laid out from the middle, so a missing button shifts every other one.
- The plain and batch menus are never told apart. The loop taps the 升級更多 / 新增城牆 slot, and the price difference gives the unit price and the batch size. A price is read in red as readily as in white.
- `_pick` buys the cheapest menu found, which is the lowest wall found; a batch just bought is re-read and quotes its next level. The sweep samples a section's dearest walls, so `_scan` taps `WALL_PITCH` out in four directions from each wall it finds.
- A batch counts as bought only when a storage moved. `MAX_BATCH` keeps the price inside its button.
- **A wall upgrade takes a free builder and hands it straight back**, so at 0/N nothing buys (the game answers 所有建築工人都在忙碌中) and the run stops on the builder counter. At the town hall's wall cap 升級 opens a sheet (注意！你需要將大本營提升至16級！) whose 確認 is grey, `game_dialog` reports no button, and the run ends `nothing_bought`: walls are finished until the town hall moves, which is the player's decision. At that level the plain menu's second button is 整排選取 rather than 升級更多; a fallback that bought the single wall instead was written and reverted, since the purchase is gated anyway.
- It honours a stop between batches and during the opening scan.

### Upkeep loops (`ui/upkeep.py`, `ui/clan.py`, `ui/hero.py`, `ui/plates.py`)

- **`collect`** taps the marker over every collector holding something, found by colour and then held to a size. The dark elixir marker sits on either an orange or a pale plate, so the plate colour is learned per bubble from the gold and elixir markers on the same frame; a frame with none of those yields nothing and says so. Gains come from the storage bars, and a pass that finds the same markers standing stops (a full storage takes nothing).
- **`upgrade`** puts idle builders on the **dearest** affordable upgrade, or the one `--only` names: builders are scarce, which is the opposite of the wall loop's bargain. It skips walls. A building's name is Chinese, so `_name` crops `NAME_BAND` for the lite tier; without a key names are blank and `--only` matches nothing. Known and not fixed: the lite tier leaks its own reasoning into `name` (4 of 11 labels on one village); `--only` is a substring match, so it still finds the name. A `max_length` on the schema, and whether the main tier does it too, are the next things to measure. The confirmation is a wall's dialog or a full-screen sheet with a green 確認, so the storages are read through `_home` afterwards. It honours a stop, because it can fall back to a sweep lasting minutes.
- **`donate`** gives troops to whoever is asking. A tap gives immediately, which is what `--dry-run` is for. A friendly challenge's 偵察 is a larger green button than 增援 and is skipped by the 進攻 in red on its row. The panel's top floats with the chat's scroll, so its rows are offsets from it. A greyscale card cannot be given. A donation is judged by the panel repainting.
- **`hero`** reads what each hero's next level costs; `--upgrade <hero>` buys for that hero only. Heroes are told apart by their banner colour, and a banner matching no hero is dropped, since 我的軍隊 stands heroes on coloured plates too. `HeroCard.upgradable` reads the button plate (`BUTTON_LIVE`), because a hero capped by the hall keeps its price. A hero being raised shows 立即完成 with a gem price where 升級 was, so `GEM_GREEN` tells the gem icon apart and that card reports no price. Getting into the hall: tap its 升級 and read the screen that comes up; `--at` taps up to `AT_TRIES` times, since overlapping buildings cycle; the grid runs a second time at `SWEEP_STAGGER` when the first pass found nothing.
- **`builders`** opens the builder panel. Running rows are the ones with a progress bar. A countdown is digits plus its first unit character; `TIME_DIGIT_TOLERANCE` is 25 because at 30 the character 小 read as a 0 and 6小時 came back as 60小時.
- **`stock`** reports what the village on screen holds as a share of its ceilings. It reports whichever village is up and never crosses.
- **`worker`, `lab` and `status`** read the top-row plates on either village (`parsers/home.py`). A plate is found by where its badge sits (`ROW_LEFT_SPLIT` and `SHIELD_BADGE_LEFT`, shared with `current_world`, plus `NIGHT_ROW_SPLIT`), never by its index in the row. The plate's own digits sit at a fixed offset from its badge (`plate_box`); the panel does not (`PANEL_BANDS`, four measured bands). `running == total - free` is a check the reader gets for free. The shield plate's 無 means no shield, told apart from an unreadable frame by how many glyphs it has. Job names go to the lite tier (`prompts/read_plate_jobs.md`), one crop per panel.

Prices in a building row shrink with their length, so `digits_from` takes the row floor as a parameter. A truncated price is the dangerous misread, since seven of eight digits still clears `MIN_PRICE`.

### Stopping and the state file (`~/.ai_coc/state.json`, `RunnerState`, `claim`, `read_state`)

- Every command that opens ADB claims the file (`running`) on the way in and writes `idle` on the way out; `read`, `stop` and `export --last` do not claim. `ai_coc stop` marks it `stopping` and returns, touching neither the game nor any process. The file is never deleted, and `ended` and `log` ride on the `idle` record.
- Loops check it only where stopping is safe: `commands.attack`'s round loop, `AttackRunner.should_stop` between opponents (a committed battle is played out, since abandoning one leaves the game where the next run cannot get home from), `walls` between batches, and every tap in `GameRunner._opened`. The wait between rounds polls at `STOP_POLL`. `collect`, `builders`, `hero`, `donate`, `probe` and `bounds` do not check it.
- The claim at the start takes over a stale `stopping`, and only the process that claimed writes the release. **Deleting the file by hand is a stop**, the escape hatch when the driving agent has died; `_held` keeps a machine's first run from reading the missing file as one.
- Nothing refuses a second run; the check belongs to whoever is about to start one. Never test a pid with `os.kill(pid, 0)`: on Windows that is `TerminateProcess` and kills the run it asked about.
- Reads never raise and writes are a scratch file and a rename, because one of the two processes sharing the file is in a battle.
- The window uses the same file. `start_automation` claims it for the whole cycle in an `ExitStack`, `MainWindow._stopping` answers both the button and `ai_coc stop`, and `stop_seen` / `_stood_down` latch a stop so the cycle does not start the next pass after it. `should_stop` is a function parameter, not a field on the options model, which must stay serialisable.

### Launch, restarts and the session

- **`ai_coc launch` leaves the game at a village and at the far zoom** (`_settle_game`), not at a pid; `LaunchReport.at_village` says whether it got there and `was_running` whether it found the game up. `RestartScope`: `none` is `ensure_coc` alone; `game` calls `ensure_coc` first and then restarts the package; `emulator` restarts the instance and waits (`_await_shutdown`) until it is listed and not started, because MuMu's `control restart` returns at once and MuMu drops the instance from the listing for a moment mid-restart. LDPlayer restarts by `quit` and `launch` and **never by `reboot`**, which rewrote the instance's settings to 1920x1080 with ADB off.
- **The emulator is restarted on a schedule** (`restart_every`, counted in battles; `--restart-every 0` turns it off) because MuMu drops frames over hours. The restart waits for a village by `current_world` for up to `RESTART_POLLS` (a cold boot varies widely), pinches out (a restarted game comes back zoomed in, and every drop then misses), and points the runner and the frame ticker at the new display. A storage bar read straight after a restart is still animating up from zero.
- **Nothing keeps the session online.** The game raids a village whether or not its owner is in it, so `ai_coc online` and the window's 保持上線 were removed; the shield a raid hands out bounds the loss.

### Adapters

- **Which instance is driven is `AppConfig.adb_serial`** (`commands.emulators`, `chosen`), in either spelling `adb devices` shows (`127.0.0.1:5555` or `emulator-5554`). Every installed emulator is asked for its instances, MuMu's first, and lazily, so a match among MuMu's never asks LDPlayer. An empty setting takes the first instance with the game running, else the first listed, and writes its serial back; the window's emulator dropdown lists every instance of both and writes the one picked. A serial matches what the instance reports or where it will listen (`serial_for`, each emulator's port scheme), which is what finds an instance that is down. An unknown serial raises rather than falling back.
- **`Emulator`** (`adapters/emulator.py`) holds the lifecycle both share: `ensure_coc`, `_booted`, the controllers. A subclass answers only what its CLI can: which instances exist, start, restart, stop, version. Two rules were paid for in bugs, so do not undo them: `_clean_environment` strips `QT_PLUGIN_PATH` and friends from every subprocess, because the PyInstaller bundle's Qt paths break mumu-cli, itself a Qt app; and `ensure_coc` escalates launch → poll → restart instance, because MuMu reports Android ready before a launch reliably sticks.
- **`MuMuAdapter`** uses `mumu-cli.exe` (JSON output). **`LDPlayerAdapter`** uses `ldconsole.exe`, whose `list2` says 0 down, 2 booting, 1 up; an instance counts as up only once `sys.boot_completed` answers 1 (its serial is port 0 until then, as MuMu's CLI does), because `list2` says 1 before ADB answers and a game launched before the boot completes dies. ADB is asked whatever `list2` says, and an answer counts as up, because `list2` can lose an instance that is running and reachable (`0,…,-1,-1`), and `ensure_coc` then launched an emulator that was already up and failed on port 0; `quit` cannot reach such an instance either. It never launches an instance already starting.
- **`adb.py` owns ADB.** `AdbController` wraps `adbutils`, one per serial, cached by `Emulator.controller()`; each instance has its own port (`AdbEndpoint`, port 0 meaning not answering yet). `use_adb_executable` points `ADBUTILS_ADB_PATH` at the emulator's own `adb.exe`. Never shell out to `adb.exe` from anywhere else. `connect` asks the server first and connects only a device it has lost: every connect opens a new connection to the emulator's adbd, and LDPlayer's port forward (VirtualBox's NAT process) died under one per call, taking ADB and the game's network with it. `launch_app` starts the resolved launcher component with `am start`, because LDPlayer's image ships no `monkey`. `AdbController.shell` escapes every argument of a list, so shell syntax goes as a string; `tap_many` needs that.
- **Every capture and every input names a display.** MuMu opens the game on a display of its own; LDPlayer keeps it on display 0. `display_for(package)` returns a `DisplayTarget`: `screencap -d` takes its physical id and `input -d` its logical id. Do not drop the `-d` flags: without them the capture fails to decode and taps land on the launcher.
- **`GeminiClient`** is the only path to Gemini (the Interactions API through `google-genai`). Every request body is a `GeminiRequest`. `generate_structured(prompt, SomeModel, png)` returns a validated instance; never parse the text yourself. `list_text_models` feeds the Settings picker. The default model is `DEFAULT_GEMINI_MODEL` in `constants.py`.

### The window

- **Threading.** Every blocking call goes through `MainWindow.run_async`, a `Worker` on the global `QThreadPool` whose result comes back via `pyqtSignal`. Never call an emulator adapter, `GeminiClient` or a `commands.*` function directly from a slot.
- **The 主控 tab runs `commands.*` and nothing of its own** (`MainWindow._run_job`, `automation_cycle`). Each checkbox names a job — `collect`, `donate`, `upgrade`, `walls`, `attack` — and the cycle round-robins the ticked ones one pass at a time, crossing to the home village before its home village jobs. A pass gets its own `RunLog`. Walls get `rounds=1` so one pass does not spend the whole storage; the attack's default is already one round. `MainWindow.job_running` is the one busy flag. Stopping is `MainWindow.automation_active`, a plain bool the workers read too.
- **The 帳號進度 tab** is one button, `commands.export`; its table is filled from the saved export.
- **Logging.** `configure_logging()` sets up a `rich` stderr console plus this run's plain-text `run.log`. A marker handler of its own, not a sink, marks the process as configured, because the `--windowed` build has no stderr. The window hands the log back to its own run between jobs. `_attach_log_panel` mirrors records as HTML into the 執行紀錄 panel, whose level selector retargets the root logger (DEBUG adds full prompts and replies). `Worker.run` logs the traceback before the message box, and `sys.excepthook` catches what Qt swallows. **New adapter code logs its own decisions**; a feature that fails silently is the bug.

### Storage and settings

`~/.ai_coc\` holds `account_json/` (one `VillageExport` per account), `config.json`, `gemini.key.dpapi` (the API key, which never goes into `config.json`), `state.json`, `cocMapping.json` (the community `data_id` table, read back from disk when the download fails), and `logs/`. There is no database; an old `controller.sqlite3` is left where it is and nothing creates a new one.

**One settings file, so a run plays the same from the window and the CLI** (`AppConfig`, `adapters/config.py`).

- Its defaults are the ones the window shows, not the field defaults underneath (`LootThresholds()` means take anything). A file that will not parse raises.
- `attack --min-gold` / `--min-elixir` / `--min-dark`, `--stop-at` and `--restart-every` override it for one run: omitting a flag keeps the file's value, and `0` takes it out. `probe` and `bounds` use `LootThresholds()`.
- `gemini` holds two tiers, `main` (once per run, a whole screenshot) and `lite` (once per candidate, a cropped strip), each with `model`, `base_url` and `thinking_level` (default `low`). The key is not in that block, because `ConfigStore.save` writes every field to a plaintext file; `GeminiClient` carries it.
- A file holding keys the model no longer reads is rewritten to what it does read, with missing keys filled in; a file that already matches is left untouched. No migrations.
- The window's own settings are the `ui` block (`UiSettings`, `UiJobs`). The job flags are named for the sub-commands (`ui.jobs.walls` runs `ai_coc walls`), and `_job_boxes` is the one place a name is paired with its widget. The name has to hold in three places at once — a field of `UiJobs`, a checkbox, a sub-command — because the cycle cannot tell a missing checkbox from an unticked one, so a job whose name misses one of them silently stops running. Nothing is in `QSettings`.
- `_save_config` merges into a fresh read, and `ConfigStore.save` is a scratch file and a rename, because the window's copy is old and `commands.*` reads the file from worker threads and other processes.

### Village export (`ai_coc export`, `commands.export`, `parsers/settings.py`, `adapters/clipboard.py`)

The game writes the whole village out as JSON from 設定 → 更多設定 → the bottom of the page → 複製, a fixed path with no AI call. Each step confirms its screen before the next tap (`settings_open`, `export_row`), because 刪除帳號 is on that page and a scroll that did not take would flip the 更高幀數 toggle. The payload leaves through the Windows clipboard, which MuMu mirrors; there is no ADB route (`cmd clipboard get` is not implemented on this image). The clipboard is emptied before the tap, so what arrives is known to be new, and whatever was on it is put back afterwards; `adapters/clipboard.py`'s `_opened` names an owner window, because a clipboard opened with NULL cannot be written back. The saved file is one `VillageExport`. **Keep the parsing tolerant**, because game updates add sections and ids: a section is whatever the document holds a list under, unknown fields survive, and a `data_id` the mapping lacks is exported with `name: null`.

## Project rules

- **Every structured value is a Pydantic `BaseModel`, no exceptions.** New shapes go in `models.py`: emulator and CLI payloads, parsed files, AI replies, requests and settings. The adapters are models too (`SecretStore`, `AdbController`, `MuMuAdapter`, `GeminiClient`), so they take keyword arguments and keep non-field state in `PrivateAttr`. Only Qt subclasses and the ctypes `DATA_BLOB` stay plain classes. No `dataclass`, no `TypedDict`, no bare `dict[str, Any]` travelling between functions, and no `.get("key")` where a model could declare the field; historical key names go in `AliasChoices`. Parse at the boundary with `model_validate` / `model_validate_json`, write with `model_dump_json()` / `model_dump()`, and give a bare list that travels a `RootModel` (`AttackSeries`). Ask Gemini for structure through `GeminiClient.generate_structured`. Models mirroring an external format that gains fields carry `model_config = TOLERANT`.
- **Every command works on the village the game is on, and none of them sails on its own.** Crossing is `ai_coc world --go` and nothing else, so whoever started a command knows where it left the game. A command with no version on the village it finds does nothing and says so by name: `builder_base` for the home village's `walls`, `upgrade`, `builders`, `hero` and `donate`, and for `probe` and `bounds`; `other_village` for an attack series on the other village, a `--plan` written for the other one included. Composing inside one village is allowed (the night attack visits its own cart, `collect` on the builder base empties it); stringing work across both villages is the caller's job — the window's cycle and the skills run `world --go` first. The user asked for this on 2026-09-25; design a new command the same way.
- **A report carries fields and a named outcome, never a sentence.** No model in `models.py` has a `message`. If the fields can say which branch a run came back from, add nothing. If they cannot, the report carries a required `Literal` outcome with one value per branch and no default. The Chinese is built from the outcome at the edge, in a `dict[XOutcome, str]` beside the command in `commands.py`, and each table has a test asserting it covers the type and that every line says something the others do not. A `*_line` helper (`round_line`, `build_line`) composes counts and names with the outcome.
- **A model field's type must be importable at runtime.** `[tool.ruff.lint.flake8-type-checking] runtime-evaluated-base-classes` keeps `TC003` from moving those imports under `if TYPE_CHECKING`; add any new model base class there.
- **A limited-time event is not on its own a reason to build anything.** Its screens go away within weeks. If something an event shows looks worth automating, say so and let the user decide; build it when they want it.
- **Never invent master data.** What an upgrade costs, how long it takes and what it requires stay unknown until a value is source-backed, and the UI shows `—` rather than a guess.
- **`docs/` is generated and gitignored**, rebuilt from the three READMEs and the source by `make gen-docs`. Edit the READMEs and the docstrings, never the output.
- **`CLAUDE.md` is a symlink to this file.** A hand-kept copy drifted thirteen PRs behind; do not replace the link with a file.
- **A change that outdates a project skill updates that skill in the same PR.** Before opening the PR, grep the six skills for whatever was renamed, moved or reversed, and mention new capabilities they should know about.
- **This file gets rules, not measurement records.** How a threshold was measured, what the previous value cost and which alternative was rejected go in the comment beside the constant (or the test that pins it). A paragraph here earns its place only as a rule an agent must follow, an invariant that spans files, or a known defect nobody has fixed; a number here is the evidence for one of those, or one a skill points at, not a record of its own. It grew from 7 KB to 235 KB between 2026-08-22 and 2026-09-25, mostly by restating those comments.
- **UI strings, prompts and user-facing messages are Traditional Chinese**; code, comments, commit messages and anything published to GitHub are English.
- **Send the user every image you look at.** They cannot see your tool output, and a path they have to open themselves is one they will not open.
- **Debug from the screen, not from the log.** A failure that depends on what the game or the emulator was showing is settled by frames of it (`--record`, `ai_coc capture`, or `repair-emulator`'s `look.py` when the emulator is the suspect), not reconstructed from `run.log` or logcat. The user asked for this on 2026-09-26, after a launch failure was chased through logcat instead of looked at.
- **Deliberate deviations from the repo template**, documented in `pyproject.toml`: the coverage gate is `--cov-fail-under=75` rather than 80, because `ui/main_window.py` is the untested PyQt shell; `[tool.ty.environment] python-platform = "win32"` so `winreg` and `ctypes.windll` resolve on Linux; ty excludes `cli.py`, `ui/main_window.py` and `ui/workers.py` for PyQt5's inaccurate stubs; `allowed-confusables` carries `／` and `？`; `check-added-large-files` excludes `tests/frames/`, whose exact pixel values are the measurement; the sdist ships no `tests/`, because the frames carry a live account's player and clan names; `build_release.yml`'s build matrix and `test.yml` run on Windows only. **`test.yml` has to be Windows**: on Linux `test_core.py` cannot import (`adapters/mumu.py` imports `winreg`), and a suite that cannot import looks exactly like one that passes. The test step also runs under `shell: bash`, whose `pipefail` keeps `uv run pytest | tee` from reporting `tee`'s exit code. `code-quality-check.yml` stays on Linux, since ruff and ty read the source rather than import it.
- **The version is never written down.** `constants.py` reads it from the installed package metadata, which CI derives from the git tag through `dunamai`. The `0.1.0` in `pyproject.toml` is a placeholder CI overwrites; do not hand-edit a version anywhere, `version_info.txt` included.
- **The PyInstaller step needs `--copy-metadata`** for the version lookup and one `--add-data` line each for `prompts/` and `plans/`, in `build_release.yml`; a missing data line raises the first time it is loaded.
- CodeQL and dependency-review jobs are gated on `github.event.repository.visibility == 'public'`, because this private repo has no GitHub Advanced Security.
