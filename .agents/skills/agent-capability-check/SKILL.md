---
name: agent-capability-check
description: >-
  Check whether the agent runtime running this session (Claude Code, Codex,
  Antigravity or any other harness) can do what this project's skills assume of
  it: dispatch a subagent on a weaker model it picks, let that subagent wait out a
  long command and take a second message, wake the session when a background
  command or a subagent finishes while the other is still running, fall back to
  its own scheduler, read text and coordinates off a frame, and reach the user's
  phone with a file and a question. It runs scratch scripts only and never touches
  the game. Use it when the user asks to check, test or compare what a runtime or
  harness can do here, for example 「測一下這個 harness 能不能託管」 or "run the
  capability check".
---

# Checking what this runtime can do

The skills under `.agents/skills/` were written against what Claude Code does: `farm` hands the loop to a subagent on a cheaper model that blocks on `attack` for hours, `takeover` sleeps on `ai_coc watch` and relies on its exit to wake the session while that subagent keeps going, `upgrades` and `find-on-map` tap at coordinates read off a frame, and every trip sends the user pictures and questions on their phone. This skill finds out which of those the runtime running it can do.

The result is `report.md` in the run's results directory, also shown in the reply: one row per check below, each with pass, fail, not offered (the runtime has no way to do it) or not run, the evidence file behind it, and what was seen. A header names the runtime and its version, the main thread's model, and the date.

## Ground rules

- **Nothing here drives the game.** Run no `ai_coc` command and spawn no farm subagent for the whole run (`AGENTS.md` says the same). The checks are about the runtime, and the commands they might borrow have side effects: a new `ai_coc watch` stands down whichever one is sleeping, and any command that touches the emulator stands down, or borrows from, a loop that is running.
- **Evidence is what the scripts wrote**, not anyone's account of it. Times come from the scripts' logs, or from the clock read and written down at that moment.
- **One results directory per run**, `.runs/capability-check/<runtime>-<YYYYMMDD-HHMM>/` under the checkout (git ignores `.runs/`), written `<dir>` below. Every file named below goes in it.
- **The first thing on every turn of the run** is a line appended to `<dir>/turns.log`: the time, and what started the turn as the runtime presents it (a user message, a command's exit notice, a subagent's report, a scheduled wake, something else). Which of these starts a turn is half of what this skill measures.
- **Before any wait that ends your turn**, tell the user the clock time you expect to be back by, with a few minutes' slack, and that they must not type until then: their message starts a turn too, and the wake could no longer be told apart from it. If you have not spoken by that time, they send any message, and that wait is recorded as not woken.

## The scripts

Both run the same way in every shell, from the checkout root:

```bash
uv run --no-sync python .agents/skills/agent-capability-check/scripts/sleeper.py --seconds <N> --log <file>
uv run --no-sync python .agents/skills/agent-capability-check/scripts/make_image.py --out <png> --key <json>
```

`sleeper.py` writes a `start` line with its pid, an `alive` line every minute, and an `exit` line with a random token that it also prints; a report that quotes the token was written after the script ended. `make_image.py` draws a 1600x900 frame, the size of ai_coc's frames, with a row of text and three coloured dots at random places, and writes the answer only to the key file, so the image is read without knowing what it holds. The point of both is that the agent under test did not make up the answer itself.

## 1. A subagent on a chosen model

Dispatch one subagent on a model one step below your own, with a lower reasoning effort where the runtime offers one. Its brief: run `sleeper.py --seconds 900 --log <dir>/t1-sleeper.log`, wait until it exits, and reply with the token. Fifteen minutes is longer than one tool call allows in most runtimes, so the brief also says how to wait, the way `farm`'s subagent waits on `attack` (`.agents/skills/farm/references/running.md`, 「你是 subagent 的話反過來」): start the script with the runtime's own background execution, then block on it in foreground waits, each shorter than one call's limit and with that call's timeout set above it, until the log has its `exit` line; never start a second copy. A script run as one plain foreground call dies at the call's limit, which would fail this row on a runtime that can do what `farm` needs. Wait for the reply however the runtime lets you; if that means ending your turn, the wait rule above applies. Write the time to `<dir>/t1-reply.txt` when the reply arrives. Then send the same subagent a second message asking which token it reported, to be answered from what it already holds without opening any file.

Rows:

- **dispatched**: a subagent ran at all.

- **model chosen**: the model asked for against the one the runtime recorded for the subagent, read from the runtime's own record, never from the subagent's word. Known places:

    - Claude Code keeps each subagent's transcript under `~/.claude/projects/<project>/<session>/subagents/`, with the `model` on the subagent's own replies (its first record can carry the parent's).
    - Codex gives each subagent its own rollout under `~/.codex/sessions/`, marked `"thread_source":"subagent"`, with the model on its `turn_context` lines.
    - Antigravity keeps `~/.gemini/antigravity/conversations/<id>.db` (the `agy` CLI under `~/.gemini/antigravity-cli/`); a subagent's id is the row in `conversation_summaries.db` whose `parent_conversation_id` is the main session, and model names show up among the strings in its step payloads.

    Where the record names no model, write not recorded. If the runtime gave you no way to pick a model, the row is not offered.

- **waited out a long command**: the reply's token matches the log's `exit` line, and the reply arrived after it. Note the longest single wait one of the subagent's tool calls allowed, where its record shows it: `.agents/skills/farm/references/running.md` sizes a subagent's wait segments by it.

- **took a second message**: the same subagent, not a new one, answered with the token.

## 2. Woken while something else still runs

This is the shape of `takeover`: the farm subagent runs for hours, the session sleeps on a timer, wakes for an upgrade while the subagent keeps going, and later wakes on the subagent's report. Run it with nothing else of yours in flight, or a wake cannot be attributed.

Start both in the background, then end the turn without waiting, polling or scheduling anything:

- a subagent that runs `sleeper.py --seconds 600 --log <dir>/t2-sub.log` to its end, waiting on it the same way as in check 1, and replies with the token;
- `sleeper.py --seconds 120 --log <dir>/t2-cmd.log` as a background command.

A runtime that passes shows two turns in `turns.log`. The first is started by the command's exit about two minutes in, while `t2-sub.log` has no `exit` line yet (look, and write down what it held); end the turn again. The second is started by the subagent's report about ten minutes in. The gap keeps the user's message after the first deadline clear of the subagent's report.

Rows: **command exit wakes** (lateness: that turn's time minus the `exit` line's), **subagent report wakes** (lateness the same way), **subagent outlived the first wake** (its log kept going and it reported).

**If the command's exit woke nothing**, wait out the subagent's row first, then try the runtime's own scheduler, which is what `running.md` tells such a runtime to set: one wake five minutes ahead, moved to fifteen minutes ahead, then end the turn. A scheduled wake two or three minutes late is normal (`running.md`), so the two times sit far enough apart that a late old one cannot pass for the new one. It passes with one turn at the new time and none at the old one; afterwards delete it and check the runtime no longer lists it. Row: **scheduler** (lateness, and whether moving and deleting worked), or not offered.

**The long version runs only when the user asks for it**, since it holds the session for hours: the same check with the subagent at two hours and the command at three, the longest `takeover` sleeps. With the subagent finishing first, it also covers the other order.

## 3. Reading a frame

Run `make_image.py --out <dir>/t3.png --key <dir>/t3-key.json`. Open the image, not the key, and write `<dir>/t3-answer.json`: the text exactly as shown, and each dot's centre as `{"red": [x, y], ...}` in the image's own pixels. Only then read the key. It passes with the text exact and every centre within 20 px, about what still lands a tap on the same panel row; record the largest miss. A runtime that shows you a scaled copy still owes coordinates on the full 1600x900 frame, and missing that scale is a failure this check is here to catch.

## 4. Reaching the user

Send `t3.png` to the user through the runtime's way of sending files, then put a question with three options to them through its way of asking, and as a push notification too where that is separate. The user tells you whether the image reached their phone, whether the question did, and whether they could answer it there. Note whether your turn stood waiting on the answer: some runtimes block the turn on a question, which `upgrades` plans around. Rows: **file reaches the phone**, **question reaches the phone and is answerable there**.

## Order, and the end

Run 3 and 4 first, then 1, then 2.

At the end ask the user which commands or tools stopped to ask their permission, and add a row **approvals** listing them: an unattended `takeover` halts on the first one.

Leave nothing running: no `sleeper.py` process (the pids are in the logs), no subagent still waiting, no scheduled wake. Name anything you leave on purpose. The results directory stays for the user. A failed row is a finding to report, not something to fix here: changing a skill to suit a runtime is a separate piece of work.
