---
name: harness-check
description: >-
  Check whether the harness running this session (Claude Code, Codex, Antigravity or any other) can do what this project's skills assume of it: dispatch a subagent on a weaker model and lowest effort it picks, let that subagent wait out a long command, take a second message, follow a numbered brief and report each step's real outcome, wait out a long foreground call on the main thread and take the user's message meanwhile, wake the session when a background command, a subagent or another session finishes while others still run, once for each of several staggered ends and in order, fall back to its own scheduler, put an image in front of the model, and reach the user's phone with a file and a question. It runs scratch scripts only and never touches the game. Use it when the user asks to check, test or compare what a runtime or harness can do here, such as whether it can run a takeover.
---

# Checking what this runtime can do

The skills under `.agents/skills/` were written against what Claude Code does: `takeover` hands the farming loop to a subagent on a cheaper model that blocks on `attack` for hours, takes that subagent's report of each step at its word, sleeps on `ai_coc watch` and relies on its exit to wake the session while that subagent keeps going and side sessions report in, wakes five minutes before an upgrade is due and waits out the rest so it can confirm seconds after the timer runs out, `upgrades` and `find-on-map` tap at coordinates read off a frame, and every trip sends the user pictures and questions on their phone. This skill finds out which of those the runtime running it can do.

The result is `report.md` in the run's results directory, also shown in the reply: one row per check below, each with pass, fail, not offered (the runtime has no way to do it) or not run, the evidence file behind it, and what was seen. A header names the runtime and its version, the main thread's model, and the date.

## Ground rules

- **Nothing here drives the game.** Run no `ai_coc` command for the whole run. The checks are about the runtime, and the commands they might borrow have side effects: a new `ai_coc watch` stands down whichever one is sleeping, and any command that touches the emulator stands down, or borrows from, a loop that is running.
- **Evidence is what the script wrote**, not anyone's account of it. Times come from the script's logs, or from the clock read and written down at that moment.
- **One results directory per run**, `.runs/harness-check/<runtime>-<YYYYMMDD-HHMM>/` under the checkout (git ignores `.runs/`), written `<dir>` below. Every file named below goes in it.
- **The first thing on every turn of the run** is a line appended to `<dir>/turns.log` with `grade.py stamp` (below), which takes the time from the script's clock: `user` for a turn the user's message started, otherwise `turn` and what started it as the runtime presents it (a command's exit notice, a subagent's report, another session's report, a scheduled wake, something else). A notice that reaches you inside a turn, alongside a tool's result say, gets a `mid` line as soon as you see it. Where the notice was for tasks with a `sleeper.py` log, pass each of those logs with `--log` and the line gets the token from its `exit` line; never the log of a task the notice was not for. Which of these starts a turn is half of what this skill measures.
- **Before any wait that ends your turn**, tell the user the clock time you expect to be back by, with a few minutes' slack, and that they must not type until then: their message starts a turn too, and the wake could no longer be told apart from it. If you have not spoken by that time, they send any message, and that wait is recorded as not woken.

## The scripts

They run the same way in every shell, from the checkout root:

```bash
uv run --no-sync python .agents/skills/harness-check/scripts/sleeper.py --seconds <N> --log <file>
uv run --no-sync python .agents/skills/harness-check/scripts/grade.py stamp --dir <dir> <turn|mid|user> "<what started it>" [--log <file>]...
```

`sleeper.py` writes a `start` line with its pid, an `alive` line every minute, and an `exit` line with a random token that it also prints; a report that quotes the token was written after the script ended, so the agent under test cannot make the answer up. `grade.py` also decides the rows of checks 7 and 8 from the files the scripts wrote, so those rows never rest on anyone's account; `job.py` stands in for an `ai_coc` command in check 8. `grade.py <command> -h` lists each command's arguments.

## 1. A subagent on a chosen model

Dispatch one subagent on the lowest model and the lowest reasoning effort the runtime offers, which is how `takeover` dispatches its farm subagent. Its brief: run `sleeper.py --seconds 900 --log <dir>/t1-sleeper.log`, wait until it exits, and reply with the token. Fifteen minutes is longer than one tool call allows in most runtimes, so the brief also says how to wait, the way `takeover`'s farm subagent waits on `attack` (`.agents/skills/takeover/references/runner.md`, 「開跑跟等」): start the script with the runtime's own background execution, then block on it in foreground waits, each shorter than one call's limit and with that call's timeout set above it, until the log has its `exit` line; never start a second copy. A script run as one plain foreground call dies at the call's limit, which would fail this row on a runtime that can do what `takeover`'s farming needs. Wait for the reply however the runtime lets you; if that means ending your turn, the wait rule above applies. Write the time to `<dir>/t1-reply.txt` when the reply arrives. Then send the same subagent a second message asking which token it reported, to be answered from what it already holds without opening any file.

Rows:

- **dispatched**: a subagent ran at all.

- **model chosen**: the model asked for against the one the runtime recorded for the subagent, read from the runtime's own record, never from the subagent's word. Known places:

    - Claude Code keeps each subagent's transcript under `~/.claude/projects/<project>/<session>/subagents/`, with the `model` and `effort` on the subagent's own replies (its first record can carry the parent's model).
    - Codex gives each subagent its own rollout under `~/.codex/sessions/`, marked `"thread_source":"subagent"`, with the model on its `turn_context` lines.
    - Antigravity keeps `~/.gemini/antigravity/conversations/<id>.db` (the `agy` CLI under `~/.gemini/antigravity-cli/`); a subagent's id is the row in `conversation_summaries.db` whose `parent_conversation_id` is the main session, and model names show up among the strings in its step payloads.

    Where the record names no model, write not recorded. If the runtime gave you no way to pick a model, the row is not offered.

- **effort chosen**: the effort asked for against the one the same record shows for the subagent; not recorded where it shows none, not offered where the runtime gave you no way to pick one.

- **waited out a long command**: the reply's token matches the log's `exit` line, and the reply arrived after it. Note the longest single wait one of the subagent's tool calls allowed, where its record shows it: `.agents/skills/takeover/references/runner.md` sizes a subagent's wait segments by it. Note too whether the runtime moved one of those foreground calls to the background by itself, and after how long, since that caps a wait segment whatever timeout the call was given: Claude Code moved the farm subagent's at 180 s on 2026-11-08, with the call's timeout at 590 s.

- **took a second message**: the same subagent, not a new one, answered with the token.

## 2. Woken while something else still runs

This is the shape of `takeover`: the farm subagent runs for hours, the session sleeps on a timer, wakes for an upgrade while the subagent keeps going, and later wakes on the subagent's report. Run it with nothing else of yours in flight, or a wake cannot be attributed.

Start both in the background, then end the turn without waiting, polling or scheduling anything:

- a subagent that runs `sleeper.py --seconds 600 --log <dir>/t2-sub.log` to its end, waiting on it the same way as in check 1, and replies with the token;
- `sleeper.py --seconds 120 --log <dir>/t2-cmd.log` as a background command.

A runtime that passes shows two turns in `turns.log`. The first is started by the command's exit about two minutes in, while `t2-sub.log` has no `exit` line yet (look, and write down what it held); end the turn again. The second is started by the subagent's report about ten minutes in. The gap keeps the user's message after the first deadline clear of the subagent's report.

Rows: **command exit wakes** (lateness: that turn's time minus the `exit` line's), **subagent report wakes** (lateness the same way), **subagent outlived the first wake** (its log kept going and it reported).

Hold the command exit's lateness against `takeover`'s five-minute prep as well, which sleeps on a background `ai_coc watch` to the moment the timer runs out and confirms seconds later: a wake more than a few seconds late passes this row and still slows every confirm by as much, so say so in it. A runtime whose command exit wakes nothing runs that last wait in the foreground instead, which check 5 measures.

**If the command's exit woke nothing**, wait out the subagent's row first, then try the runtime's own scheduler, which is what `share-emulator` tells such a runtime to set: one wake five minutes ahead, moved to fifteen minutes ahead, then end the turn. A scheduled wake two or three minutes late is normal (`share-emulator`), so the two times sit far enough apart that a late old one cannot pass for the new one. It passes with one turn at the new time and none at the old one; afterwards delete it and check the runtime no longer lists it. Row: **scheduler** (lateness, and whether moving and deleting worked), or not offered.

**The long version runs only when the user asks for it**, since it holds the session for hours: the same check with the subagent at two hours and the command at three, the longest `takeover` sleeps while it farms. With the subagent finishing first, it also covers the other order.

## 3. Seeing an image

This checks the runtime, not the model: whether it can put an image file in front of the model at all. `upgrades` reads the build panel off a frame before every upgrade, so a runtime that cannot do this can farm but never upgrade. How well the model reads the image is not measured.

Open `tests/frames/hud_day_build_panel.png` through the runtime's own way of reading files, and write to `<dir>/t3-seen.txt` what it shows: which screen it is, the name on the panel's top row, and the gold count. The answers are not written here, so the file name alone cannot pass it; whoever reads the report holds them against the image. Row: **image reaches the model** (pass, or fail with what came back instead, such as an error or the file's bytes as text).

## 4. Reaching the user

Send that same frame to the user through the runtime's way of sending files, then put a question with three options to them through its way of asking, and as a push notification too where that is separate. The user tells you whether the image reached their phone, whether the question did, and whether they could answer it there. Note whether your turn stood waiting on the answer: some runtimes block the turn on a question, which `takeover` plans around. Rows: **file reaches the phone**, **question reaches the phone and is answerable there**.

## 5. A long foreground wait on the main thread

`takeover`'s prep holds the emulator for the last five minutes before a timer runs out, and on a runtime whose command exit wakes nothing it spends them in one foreground `ai_coc watch` on the main thread. Run `sleeper.py --seconds 360 --log <dir>/t5-fg.log` as one plain foreground call on the main thread, its timeout set above six minutes. Row: **main thread waits six minutes in one call**: pass when that same call returns the token; otherwise note what ended it and after how long (a limit on the call, a move to the background, something else).

## 6. A message while the main thread waits

`AGENTS.md` has whoever hears a stop order run `ai_coc stop` themselves, so what matters is when a message the user sends during a long foreground call reaches you. After check 5's call has returned, start a second one the same way (`sleeper.py --seconds 240 --log <dir>/t6-fg.log`), and before it ask the user to send one short message about a minute after you say it has started, with the clock time they sent it written in it. It gets a call of its own because a runtime where the message cuts the call short would otherwise leave check 5 unanswered. Write to `<dir>/t6-message.txt` the time it reached you and how it arrived (alongside the call's result, interrupting the call, as a turn of its own after yours ended, something else). Row: **message during a foreground call** (the delay against the time written in it, and how it arrived).

## 7. Several ends at staggered times

Check 2 has one command and one subagent; `takeover` is busier. While it sleeps on a background `ai_coc watch` the farm subagent runs for hours and side sessions report in, so ends come close together or while the session is busy with another. This check asks whether each end still wakes the session on its own, in the order they end, and whether waking for one swallows or delays another. Leave out a kind of end that check 2 showed wakes nothing, and skip the check if neither did. Run it with nothing else of yours in flight, and start it on a turn the user's own message started, asking them for one when the check before has ended on a notice: a runtime can refuse to start a session on a turn a notice started, as Claude Code's desktop app did on 2026-11-12.

Each task runs `sleeper.py --seconds <sleeps> --log <dir>/t7-<task>.log`. Start them in this order, slowest to get going first, then end the turn without waiting, polling or scheduling anything:

| task | what                                                                                                                                                                                                                                                                                       | sleeps |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------ |
| g    | another session, where the runtime can start one that reports back to this one, told only to run the script to its end, waiting on it the way check 1's subagent does, and report the token; give it the checkout root and the log as absolute paths, since it may start in another folder | 660 s  |
| f    | a subagent on the lowest model and effort, waiting on the script the way check 1's does and replying with the token                                                                                                                                                                        | 540 s  |
| c    | the same                                                                                                                                                                                                                                                                                   | 240 s  |
| d    | a background command                                                                                                                                                                                                                                                                       | 360 s  |
| e    | a background command                                                                                                                                                                                                                                                                       | 405 s  |
| b    | a background command                                                                                                                                                                                                                                                                       | 140 s  |
| a    | a background command                                                                                                                                                                                                                                                                       | 120 s  |

`b` ends 20 seconds behind `a`, an end right behind another. As soon as `d`'s end reaches you, after its stamp, run `sleeper.py --seconds 90 --log <dir>/t7-hold.log` as one foreground call, so `e` ends while the turn is busy; on every other wake, stamp and end the turn. The user stays off the keyboard until the last task should have reported, about thirteen minutes on and longer when a session is slow to start: every wake before then is one of these.

Then `grade.py wakes --dir <dir> --tasks <the tasks started, comma-separated>` prints the rows from the `exit` lines and the stamps:

- **every end woke the session**, with each task's lateness (its first stamp minus its `exit` line) and the worst. A subagent's or a session's lateness includes the time it took to report.
- **woken in the order they ended**.
- **an end right behind another**: whether it got a turn of its own, shared one or arrived inside one.
- **an end during a busy turn**: whether it arrived inside the held turn or started the next, and how long after the hold returned.
- **another session's report wakes**. Without `g` the script prints not run; write not offered where the runtime cannot start a session that reports back.

Hold the lateness against `takeover`'s five-minute prep as check 2 does.

## 8. A subagent follows a numbered brief and reports what happened

`takeover` briefs its farm subagent the way `.agents/skills/takeover/references/farming.md` does: the lowest model and effort, a numbered list of complete commands, and a subagent that decides nothing, runs one step per call and stops at the first error. The loop is only as good as the report that comes back: a failure called a success, a step run after a stop, or a step merged, skipped or changed is how a cheap subagent breaks it. A stopped `attack` is the hard case, since it exits 0 with an ordinary outcome and only `state.json`'s `stop_by` shows the stop (`.agents/skills/takeover/references/runner.md`, 被叫停, 被借走).

Make two briefs:

```bash
uv run --no-sync python .agents/skills/harness-check/scripts/grade.py plan --runs <dir>/t8-error --fate error
uv run --no-sync python .agents/skills/harness-check/scripts/grade.py plan --runs <dir>/t8-stop --fate stop
```

Each prints four numbered `job.py` commands of 20 seconds. One of the middle steps, picked at random and not shown in the commands, fails in the first brief: it exits 1 and its `result.json` says `error`. In the second it is stopped partway: it exits 0, its `result.json` says `done`, and only `state.json` in its runs directory shows the stop, which the next step's start clears, as a real command's claim does.

Dispatch two subagents side by side, each on the lowest model and effort the runtime offers, each given this text with its own runs directory and commands filled in:

```text
Run the numbered commands below from <checkout root>, in order, one command per call. You decide nothing: do not change, merge, skip, repeat or add a command.
After each command, read <runs>/<label>/result.json and <runs>/state.json.
Stop and run nothing more when a command exits non-zero, when its outcome in result.json is not done, or when stop_by in state.json is set after it: a stopped run exits 0 and says done, and only stop_by shows the stop.
<the numbered commands>
End your reply with one line per step, either
sN exit=<exit code> outcome=<outcome> token=<token>
or
sN not run
and then one line:
ended: finished | error at sN | stopped at sN by <stop_by>
```

Wait for both replies however the runtime allows; if that means ending your turn, the wait rule applies, for a few minutes. Save each reply verbatim as `<dir>/t8-error-reply.txt` and `<dir>/t8-stop-reply.txt`, then:

```bash
uv run --no-sync python .agents/skills/harness-check/scripts/grade.py report --runs <dir>/t8-error --report <dir>/t8-error-reply.txt
uv run --no-sync python .agents/skills/harness-check/scripts/grade.py report --runs <dir>/t8-stop --report <dir>/t8-stop-reply.txt
```

Rows, each with what `grade.py` printed:

- **reported each step as it ended** (a row for each brief): every step's line and the ended line match what `job.py` recorded. No reply is a fail.
- **failure reported as a failure** (error brief). Read the reply's prose as well: a sentence that calls the failed step a success fails the row whatever its lines say.
- **stopped at the first error** (error brief).
- **noticed a stop its exit code hides** (stop brief).
- **ran the brief as written** (a row for each brief): nothing skipped, repeated, changed or added. Two steps run in one call do not show in the files, so read the subagent's calls from the runtime's record (the places check 1 lists) and fail the row if one call ran two commands; write not recorded where the record does not show them. `grade.py` prints the gaps between steps beside it.

Each row also names the model and effort the runtime recorded for that subagent, since a pass holds only for that tier, and says it is one sample.

## Order, and the end

Run 3 and 4 first, then 5, then 6, then 1, then 8, then 2, then 7: check 8 builds on the dispatch check 1 proved, and check 7 leaves out what check 2 found cannot wake the session.

At the end ask the user which commands or tools stopped to ask their permission, and add a row **approvals** listing them: an unattended `takeover` halts on the first one.

Leave nothing running: no `sleeper.py` or `job.py` process (the pids are in the logs and journals), no subagent still waiting, no scheduled wake, and no session check 7 started still open (archive it where the runtime can). Name anything you leave on purpose. The results directory stays for the user. A failed row is a finding to report, not something to fix here: changing a skill to suit a runtime is a separate piece of work.
