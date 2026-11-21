---
name: attack-plans
description: >-
  Discuss, review or change how ai_coc attacks, the home village tactic and the builder base one alike, with the user on pictures of real battle frames: the current plan and a proposed one drawn where their taps land for the army on that frame, a grid fitted to the village to point with, and what a battle actually did against what its plan asked. Use it whenever the user wants to talk about attack strategy, where or when troops, heroes, the siege machine or spells go, how a battle went as a tactic, or wants a default plan (flat.json, night_flat.json) or a plan file for attack --plan looked at, compared or changed. A loop that failed to play the plan it was given is watch-and-fix.
---

# Talking about an attack plan

The user judges a tactic by looking at the game, not at JSON: he points at ground and says where things should go and when. So every claim in the discussion sits on a picture of a real battle frame he has in front of him, and what he decides comes back as a place on that picture. The result of a discussion is his decision, in his words and yours agreeing on the same ground; a changed plan file is separate work that follows from it.

## The picture

`ai_coc draw` draws on one battle frame, taken before the first drop, where every step of a plan would be tapped, each marked with its number in play order and the second of the plan's own clock (`#5 7秒`), and a grid fitted to that village's red deployment line, whose cells carry the bare numbers. The picture carries nothing else: no list of steps, no army, no caveats, because those are what the discussion is about (the user, 2026-11-21). The command prints them instead, after the picture's path (`drawn.png` in its own run directory): the army it read off the card row, each step by the plan's clock, what it could not read and where no tap can land; `result.json` has every point as numbers. It touches no emulator and claims nothing, so it runs beside a farming loop.

The grid is how he answers. Its sides are A 左上邊, B 右上邊, C 左下邊, D 右下邊, its corners 上角, 右角, 下角, 左角, and its nine cells are numbered row by row from the top corner: 1 at the top, 3 at the right corner, 7 at the left, 9 at the bottom. He also points between cells ("where 5, 6, 8 and 9 meet"). It is the bounding diamond of the red line, so it is only as square as the base: a corner marked (空地) is bare ground. A cell's number is moved a little off the cell's middle when a step's label is in the way; the place meant is the cell.

- **A frame, fresh each time.** No picture is kept as the one to discuss on (the user, 2026-11-21): use a battle from now. Every `attack` run keeps the frame its newest battle's first drop followed, with or without `--debug`, and `ai_coc draw --run <label or run directory>` draws on it and reads the camera's drag from that run's log. So the farming loop's own run on that village is usually enough: `~/.ai_coc/state.json`'s `emulator.log` is the run playing now, and `~/.ai_coc/runs.jsonl`'s `start` lines name the earlier ones. A run from before this existed, or whose frames were culled, answers `no_frame`.
- **A battle played for the discussion**, when no recent one will do (another army, another village): `ai_coc attack --repeat 1 --plan flat --stop-at 0 --label <name>` on the village the game is on (`ai_coc world --day` or `--night` crosses first), then `draw --run <name>`. That drives the game, so `share-emulator` (borrowing and giving back) and `AGENTS.md`'s live test rules apply in full.
- **A frame from anywhere else** (a capture, a frame of an older recorded run) goes in as the first argument instead of `--run`, with `--camera DX DY` when the loop had dragged the battle camera by then; its log line `Dragged the camera … now (x, y) off where the battle opened` gives the numbers.
- **Which plan.** `--plan` takes a shipped name or a JSON path, as `attack --plan` does; `flat` is the default, and `--night` reads the builder base's file for a name. `--only plan` or `--only grid` draws one of the two when both together are too busy to read.
- **A proposal is the whole plan with the change in it**, never the changed step alone: he compares one whole plan against another. The shipped plans are `src/ai_coc/plans/flat.json` and `night_flat.json`; save a copy outside the repository, change it, keeping its `reason` true to what it now does, and draw it with `--plan <copy> --against flat`: the proposal in colour, and in white dashes marked 對照 each point of the current plan the proposal no longer taps. A plan writes each point as `x_pct`, `y_pct` of the 1600x900 screen for a village in the middle of it, before any camera drag; `draw` prints every cell's middle and every crossing of four cells in that form, so a place he names turns into a point without arithmetic. A `wait` counts seconds from the step before it finishing.
- **Where the army stood at some moment** is drawn with `--on <path of a later frame of the same battle>`: the grid and the plan measured on the opening frame, painted over that frame; with `--only grid` it stays readable. Later frames exist only for a battle recorded with `--debug`. A frame's seconds into the battle are its `Saved frame` line's clock minus that of the first `Played` line, and a spell shows on the ground from the frame saved after its own `Played` line, not the one saved while it was being cast.

Look at every picture yourself before he sees it. A grid that does not enclose the village along its red line, a mark on the card row or marks that do not match the command's own text make a picture to fix or leave out, not one to explain. Then put the picture itself in front of him, every one you looked at, the one that answers him first: a path he has to open is one he will not. What the picture assumes goes in your own message beside it, from the command's text and in his language: which army it was drawn for, and whichever of the points below bear on what he is deciding.

## What a picture assumes, and what it cannot say

Say these when they bear on what he is deciding, rather than letting the picture claim more than it knows.

- **The army is that frame's.** How many bottles a spell step gets, and so which of the plan's points get one, depends on the card row: donated spells sit on a card of their own, and a hero being upgraded has no card. `draw` counts the row and prints it; say which army the picture assumes. A circle in dashes, in the step's colour and with no label, is a point of the plan no bottle reached with that army, and `×2` is two bottles on one spot. A step's last cast goes round its points in the order the plan lists them, whatever the split between cards, so every point takes one before any takes a second, with more bottles than points the first ones take the extra, and the order he wants is part of the proposal. Hero steps are named by card position, because nothing on the row says which hero a card holds.
- **Some of the grid cannot be tapped.** The card row covers the bottom of the screen, and after the camera's drag part of the grid lies under it. `draw` lists those places; never propose a point on one.
- **A default plan is fixed places on the screen, played against every opponent.** A cell he points at on one village is the same screen position on the next one, over different buildings. What he chooses on one frame is a position relative to the map, not to that base.
- **The marks are taps, not landings.** The game refuses a drop inside the red line and the loop sends it again further out; a note that the troops' line could not be fitted to the red line means that line is drawn where the plan wrote it, and says nothing about the grid. On 2026-11-21 the builder base plan's lanes lay outside the map and the game put the troops on its nearest edge. Only a recorded battle says where things ended up.
- **The clock is the plan's.** Each step is played later than its number by what the steps before it took: in one battle of 2026-11-21 the plan's 7, 15 and 16 seconds were played 11, 22 and 26 seconds in.
- **The builder base's second stage** plays the same plan on a different base, and a run keeps the frame of the last stage it reached.

## What one battle showed

Report a battle as what was observed, with its numbers, and stop there: one battle against one opponent is not evidence about a tactic (`dev-rules`' `references/code.md`, Measuring a change), and what he reports seeing is a hypothesis in the same way. The numbers are in that run's `run.log`:

- `Played <act>, Ns in` is when each step really went, to set beside the plan's clock.
- The `Tapping` lines just **before** each `Played` line are where it went, in screen pixels as the camera stood, which is the plan's point plus the camera's drag.
- A step's last cast taps each spell card once more than it holds (`held 3, tapped 4`), and the game answers the spare tap with 該法術已用完: that is not a fourth bottle.
- The journal's `round` line holds the plan that round played.

## How the conversation goes

Keep each message short and lead with the answer: the picture and the one thing it shows. Ask one question at a time, answerable with a place on the grid or a number of seconds; a second question waits for the answer to the first.

When he has decided, repeat the decision back as places on the grid and seconds, drawn as a proposal, before anything is changed. A proposal he turns down changes nothing: the last picture he accepted stands, and the next proposal starts from it.

## After the discussion

- **Pictures, frames and proposal files are working material.** Nothing from a discussion is committed or kept: delete the `frames/` of the runs you made for it once it is settled, as `AGENTS.md` says of frames, and leave another session's runs to it. A frame showing another player's name or clan never goes into the repository as it is; `dev-rules`' `references/code.md` says what has to be painted over first.
- **Changing a plan is a code change with its own evidence.** The default tactics are his (`dev-rules`' `references/attack.md`, The plan), so `flat.json` or `night_flat.json` changes only on his say-so, and a variant under trial is a file of its own fed through `--plan`. How a change is measured before it replaces a default is `references/code.md`'s Measuring a change, and the change ships through the repository's development flow like any other.
