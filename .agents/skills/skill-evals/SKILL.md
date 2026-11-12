---
name: skill-evals
description: >-
  Run the dry-run evals that check whether an agent following this project's skills (takeover, upgrades, share-emulator) makes the decisions they prescribe: which upgrade each slot picks, a goblin's gems against the cap and the agent's own reading, the takeover's watch and farm brief, a new instruction kept as a note, stopping on another device's login or the user's stop, waiting out another session's hold or holding again after one was lost. Each scenario hands a fresh agent a brief inside a sandbox whose ai_coc replays recorded screens and reports, and a grader scores the commands it ran and the reply it wrote. Nothing reaches the emulator or the real ~/.ai_coc. Use it before and after a change to those skills, to compare models or runtimes, or when the user asks how well an agent follows the skills.
---

# Skill evals

Each run answers one question: on this runtime and model, does an agent that follows `takeover`, `upgrades` and `share-emulator` make the decisions they prescribe? The result is `report.md` in the run's results directory, also shown in the reply: a header naming the runtime, the model and effort, the skills' commit and the date, and one row per check with pass, fail, stale or not run, the rule it pins and the evidence.

## Ground rules

- **Nothing here touches the game.** Every `ai_coc` call an eval makes goes to a stand-in inside its sandbox; run no `ai_coc` command of your own for the whole run. It can run beside a live takeover.
- **The agent under test gets the brief and nothing else.** Hand it `brief.md` as written, or its path to read, answer none of its questions, and never open a scenario's checks while its agent runs: a hint about what is checked is the answer.
- **One run is one sample.** A check that flips between two runs is run three more times (`--repeat 3`) before it is called a regression or a fix.

## Running

From the checkout root:

```bash
uv run --no-sync python .agents/skills/skill-evals/scripts/evals.py setup [--only <names>] [--repeat N]
```

It builds one sandbox per scenario under the system's temp directory (about a second each, longer on a cold `uv` cache) and prints the results directory and each sandbox's `brief.md`. Each sandbox is a checkout of its own: copies of `AGENTS.md`, `.agents/` and `src/`, a `uv` project whose `ai_coc` is the stand-in, and a `home/.ai_coc` the brief tells the agent to read in place of its own.

For each sandbox, start a fresh agent on the model and effort being measured, a subagent or, on a runtime without one, a new session, and give it the brief. Start it where a `uv run --no-sync ai_coc` it runs before changing directory cannot reach a real one: its sandbox, or a fresh worktree whose environment was never synced, where the command fails to find `ai_coc`. Never the checkout you run this from, whose `ai_coc` drives the real emulator and the real `~/.ai_coc`. Scenarios are independent and can run side by side. Save its final reply, verbatim, as `reply.md` in its sandbox, and note the tokens and time the runtime reports for it, which are the run's cost. An agent that ends without a reply leaves its checks `not run`.

```bash
uv run --no-sync python .agents/skills/skill-evals/scripts/evals.py grade <results dir> --runtime "<runtime and version>" --model "<model and effort>" [--against <earlier results dir>]
```

`grade` copies each sandbox's `calls.jsonl`, `reply.md` and `out/` into the results directory (git ignores `.runs/`), writes `results.json` and `report.md`, and with `--against` lists the checks whose verdict changed. The sandboxes can be deleted afterwards.

## Reading a result

- **fail**: read the scenario's `calls.jsonl` (every `ai_coc` call: its arguments, exit code, outcome and the taps it sent) and `reply.md` before blaming anyone. The cause is the skill's text (a rule missing, unclear or contradicted by another), the scenario (a check stricter than the rule, a fixture that does not stage the case), or the model. Say which, with the evidence; a skill change it calls for is separate work.
- **stale**: the rule's phrase is gone from the skill it names. The check would have given the verdict its evidence shows; read the rule where it went and update the scenario.
- **not run**: no `reply.md`.

## The scenarios

`scenarios/<name>.toml`, one bounded slice each that ends at a reply: the situation and the user's words, the fixture edits that stage the case, the scripted reports, the frames, and the checks. `scripts/standin.py`'s docstring says what the sandbox's `ai_coc` runs for real and what it replays; `scripts/evals.py`'s `Scenario`, `Check` and `Select` say what a scenario and a check can hold.

- **Every expected value comes from the fixture and the skill's text**, never from what an agent answered. `village/export.json` is a real village export with the player tag replaced, and `village/status.json` the `status` run recorded with it; times in fixtures are shifted at setup so they read as now. A scripted report has to be what the current CLI returns: take it from a real `result.json`.
- **Frames are named from `tests/frames/`, never copied**, and only frames whose readings a test pins: a scenario that leans on a reading nobody checks can pass for the wrong reason.
- **A screen lists the taps that lead on from it, and nothing else does.** Frames come from different recordings, so one shown after an input that would not have produced it changes the HUD under the agent, which then takes it for the user playing and stops: on 2026-11-12 two agents did. End the recording where no frame of the next screen exists; a check that needs the screen after it cannot be graded there.
- **The sandbox carries `village/entities.json.gz`**, the game's tables for the export's version trimmed to what the village holds. Without them `search` answers a name with `no_tables` and the agent goes to `ai_coc gamedata`, which the recording cannot answer.
- **A check is deterministic**: a command run or not, its arguments, nothing claiming after a call, a cell of the upgrade table the skill prescribes, a pattern in the reply or in a file the brief has the agent write (`out/`). Each carries an anchor, the skill file and a phrase of the rule it pins, so a rule that moves shows up as stale rather than as a silent pass or fail.
- `tests/test_skill_evals.py` stages every scenario, checks its anchors and replies, and runs the stand-in and the grader.

Left out of the first set: buying through the build panel end to end and the five-minute prep, which need one coherent recorded frame sequence from a live upgrade; the night builder's pick, where the skill does not say whether a trap counts as a building; and the other skills.
