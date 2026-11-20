---
name: attack-plans
description: >-
  Discuss, review or change how ai_coc attacks, the home village tactic and the builder base one alike, with the user on pictures of real battle frames: the current plan and a proposed one drawn where their taps land, a grid fitted to the village to point with, and what a recorded battle actually did against what its plan asked. Use it whenever the user wants to talk about attack strategy, where or when troops, heroes, the siege machine or spells go, how a battle went as a tactic, or wants a default plan (flat.json, night_flat.json) or a plan file for attack --plan looked at, compared or changed. A loop that failed to play the plan it was given is watch-and-fix.
---

# Talking about an attack plan

The user judges a tactic by looking at the game, not at JSON: he points at ground and says where things should go and when. So every claim in the discussion sits on a picture of a real battle frame he has in front of him, and what he decides comes back as a place on that picture. The result of a discussion is his decision, in his words and yours agreeing on the same ground; a changed plan file is separate work that follows from it.

## The picture

`ai_coc draw` draws on one battle frame, taken before the first drop, where every step of a plan would be tapped, numbered in play order with the plan's own clock under the frame, and a grid fitted to that village's red deployment line. It touches no emulator and claims nothing, so it runs beside a farming loop. It prints the picture's path first (`drawn.png` in its own run directory), then the same facts as text; `result.json` has every point as numbers.

The grid is how he answers. Its sides are A 左上邊, B 右上邊, C 左下邊, D 右下邊, its corners 上角, 右角, 下角, 左角, and its nine cells are numbered row by row from the top corner, 1 at the top, 3 at the right corner, 7 at the left, 9 at the bottom. It is the bounding diamond of the red line, so it is only as square as the base: a corner marked (空地) is bare ground, and a lower corner can lie under the card row.

- **A frame to draw on.** A plan is drawn right only on a frame taken once the battle camera had settled, together with how far the loop had dragged it. `--run <label or directory>` takes both from an `attack` run recorded with `--debug` (`--battle N` picks a battle; each builder base stage counts as one). A lone frame is given as the first argument, with `--camera DX DY` when the loop had dragged the camera by then. The two committed frames are always there: `tests/frames/battle_flat_reference.png` with `--camera 0 102`, and `tests/frames/night_flat_reference.png` with `--night`.
- **A fresher frame** means one battle played for it: `ai_coc attack --repeat 1 --plan flat --stop-at 0 --debug --label <name>` on the village the game is on (`ai_coc world --day` or `--night` crosses first), then `draw --run <name>`. That drives the game, so `share-emulator` (borrowing and giving back) and `AGENTS.md`'s live test rules apply in full. The frames of old runs are culled, and `draw` answers `no_frame` for a run that has none left.
- **Which plan.** `--plan` takes a shipped name or a JSON path, as `attack --plan` does; `flat` is the default, and `--night` reads the builder base's file for a name. `--only plan` or `--only grid` draws one of the two when both together are too busy to read.
- **A proposal** is a copy of the plan with the change made, saved outside the repository, drawn with `--plan <copy> --against flat`: the proposal in colour, and in white dashes marked 對照 whatever the current plan does differently. A plan writes each point as `x_pct`, `y_pct` of the 1600x900 screen for a village in the middle of it, before any camera drag; `draw` prints each grid cell's middle in that form, so "freeze on 5 and 8" turns into two points without arithmetic. A `wait` counts seconds from the step before it finishing.

Look at every picture yourself before he sees it. A grid that does not sit on the red line, a mark on the card row or a legend that does not match the plan is a picture to fix or leave out, not one to explain. Then put the picture itself in front of him, every one you looked at: a path he has to open is one he will not.

## What a picture cannot say

Say these when they bear on what he is deciding, rather than letting the picture claim more than it knows.

- **A default plan is fixed places on the screen, played against every opponent.** A cell he points at on one village is the same screen position on the next one, over different buildings. What he chooses on one frame is a position relative to the map, not to that base.
- **The marks are taps, not landings.** The game refuses a drop inside the red line and the loop sends it again further out. On 2026-11-21 the builder base plan's lanes lay outside the map and the game put the troops on its nearest edge. Only a recorded battle says where things ended up.
- **The clock is the plan's.** Each step is played later than its number by what the steps before it took: in the reference battle the plan's 7, 15 and 16 seconds were played 11, 22 and 26 seconds in.
- **Bottles are counted off that frame's card row.** A circle in dashes is a point of the plan no bottle reached with that army, and `×2` is two bottles on one spot.
- **The builder base's second stage** plays the same plan on a different base.

## What one battle showed

Report a battle as what was observed, with its numbers, and stop there: one battle against one opponent is not evidence about a tactic (`dev-rules`' `references/code.md`, Measuring a change), and what he reports seeing is a hypothesis in the same way. The numbers are in that run's `run.log`: `Played <act>, Ns in` is when each step really went, to set beside the plan's clock, and the `Tapping` lines after each are where it went. The journal's `round` line holds the plan that round played. Where the troops were at a given moment is on the run's frames, which exist only when it ran with `--debug`.

## How the conversation goes

Keep each message short and lead with the answer: the picture and the one thing it shows. Ask one question at a time, answerable with a cell or a side; a second question waits for the answer to the first.

When he has decided, repeat the decision back as places on the grid and seconds, drawn as a proposal, before anything is changed.

## After the discussion

- **Pictures, frames and proposal files are working material.** Nothing from a discussion is committed or kept unless he asks for it. A frame showing another player's name or clan never goes into the repository as it is; `dev-rules`' `references/code.md` says what has to be painted over first.
- **Changing a plan is a code change with its own evidence.** The default tactics are his (`dev-rules`' `references/attack.md`, The plan), so `flat.json` or `night_flat.json` changes only on his say-so, and a variant under trial is a file of its own fed through `--plan`. How a change is measured before it replaces a default is `references/code.md`'s Measuring a change, and the change ships through the repository's development flow like any other.
