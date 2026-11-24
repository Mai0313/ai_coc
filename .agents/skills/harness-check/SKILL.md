---
name: harness-check
description: >-
  Check whether the harness running this session (Claude Code, Codex, Antigravity or any other) can do what this project's skills assume of it, in one run of about twenty to thirty minutes that needs nobody at the keyboard: the session walks one story that starts a side session and subagents on the weakest model and switches them, messages them mid-call, sleeps on background timers and is woken by them and by reports while it is idle or busy in a six-minute foreground call, has a subagent follow a numbered brief with a hidden failure and a hidden stop, has a nested subagent print the takeover start table, then reads an image, renders HTML, sends a file and cleans up, and writes one table of what works. It runs scratch scripts and two read-only ai_coc commands, never touching the game. Use it when the user asks to check, test or compare what a runtime or harness can do here, such as whether it can run a takeover.
---

# Checking what this runtime can do

The skills under `.agents/skills/` were written against what Claude Code does: `takeover` hands the farming loop to a subagent on a cheaper model that blocks on `attack` for hours and takes its report of each step at its word, sleeps on a background `ai_coc watch` and relies on its exit to wake the session while that subagent keeps going and the sessions it started for a code change report in, starts work meant for later as a session on the weakest model that keeps time itself, wakes five minutes before an upgrade is due and waits out the rest so it can confirm seconds after the timer runs out, and `upgrades` and `find-on-map` tap at coordinates read off a frame. This skill finds out which of those the runtime running it can do, in one story that exercises them all.

The session the user asked runs the story: the **main session**. It directs a **side session** it starts, which starts a **nested subagent** of its own, and a **subagent** of its own (the subagent line). The result is `report.md` in the run's directory, also shown in the reply (The report, below).

## Ground rules

- **Nothing here drives the game.** A takeover may be running on the emulator. The only `ai_coc` commands in the whole run are the nested subagent's `status --last` and `search`, which read saved files and never claim the emulator; anything else could stand down a sleeping `ai_coc watch` or a farming loop. Each carries `--agent`, `--session` and `--mission` as `AGENTS.md` says, and every `uv run` is `--no-sync`.

- **The user stays off the keyboard until the end.** At the start, tell them the clock time the run should be over by, thirty minutes on, which leaves a few minutes' slack, and that if nothing has come from you by then, any message they send picks the run up again. A message from them starts a turn, which could no longer be told apart from a wake.

- **One results directory**, `.runs/harness-check/<runtime>-<YYYYMMDD-HHMM>/` under the checkout (git ignores `.runs/`), written `<dir>` below, passed to every actor as an absolute path. The main session's files sit at its top, the side session's in `side/`, the nested subagent's in `nested/`, the subagent line's in `sub/`; the file names below are the ones `grade.py` reads.

- **Every actor stamps each of its turns** before anything else, with `grade.py stamp --dir <its directory>`: `user` for a turn a message started (from the user, or for the side session and the subagents, from whoever started them), otherwise `turn` and what started it as the runtime presents it (a command's exit notice, a subagent's report, another session's report, a scheduled wake). A notice or message that reaches it inside a turn, alongside a tool's result say, gets a `mid` line as soon as it is seen. Where the notice was for a `sleeper.py` run, pass that run's log with `--log` and the line gets the token from its `exit` line; never the log of a run the notice was not for. Where it is a report that quotes a token, write the token it quotes into the line's text instead, so a report that did not quote one cannot pass. Which of these starts a turn, and how late, is half of what the story measures.

- **Evidence is what the scripts wrote and the runtime's own records**, never an actor's account, the main session's own included. A token in a report proves the report was written after that script ended. Which model and effort each turn ran on comes from the runtime's record:

    - Claude Code: a session's transcript is `~/.claude/projects/<project directory>/<uuid>.jsonl` (not named after the app's session id, so find it by a phrase from its brief), and a subagent's is `agent-<id>.jsonl` under `<session uuid>/subagents/`; each `assistant` line carries `message.model` and `effort` (a subagent's first record can carry the parent's model).
    - Codex: each session and subagent has its own rollout under `~/.codex/sessions/` (a subagent's marked `"thread_source":"subagent"`), with the model on its `turn_context` lines.
    - Antigravity: `~/.gemini/antigravity/conversations/<id>.db` (the `agy` CLI under `~/.gemini/antigravity-cli/`); a subagent's id is the row in `conversation_summaries.db` whose `parent_conversation_id` is its parent, and model names show up among the strings in its step payloads.

    Where a record names no model or effort, write not recorded.

- **Every step that cannot be done is recorded and the story goes on**, so one failure never leaves the rest unrun. A piece whose model cannot be chosen or switched runs on what it got to the end, and its row says so (cannot choose model, cannot switch model). Shift the later times when a step runs late rather than dropping what follows.

## The scripts

They run the same way in every shell, from the checkout root:

```bash
uv run --no-sync python .agents/skills/harness-check/scripts/sleeper.py (--seconds <N> | --until <HH:MM:SS>) --log <file>
uv run --no-sync python .agents/skills/harness-check/scripts/grade.py stamp --dir <dir> <turn|mid|user> "<what started it>" [--log <file>]...
```

`sleeper.py` writes a `start` line with its pid, an `alive` line every minute and an `exit` line with a random token that it also prints; `--until` aims the end at a clock time, which is how ends started by different actors minutes apart land where the schedule wants them, and it refuses a time already past. `job.py` stands in for an `ai_coc` command in the subagent line's briefs. `grade.py` also decides rows from the files: `wakes` (how each timed end reached its actor), `plan` and `report` (the briefs), `table` (the takeover table); `grade.py <command> -h` lists each one's arguments.

## The story

Every time is a clock time worked out from `t0`, when the prologue starts, and passed on when the step that needs it runs.

| when                          | end                                 | reaches                                        |
| ----------------------------- | ----------------------------------- | ---------------------------------------------- |
| t0 + 7:00                     | subagent line's long command        | the subagent; its report then the main session |
| t0 + 8:00                     | prologue's first background script  | main session, inside its long wait             |
| t0 + 8:20                     | prologue's second background script | main session, inside its long wait             |
| t0 + 8:40                     | side session's timer                | side session, idle                             |
| t0 + 10:30, or six minutes on | main session's long wait            | main session                                   |
| about t0 + 12                 | side session's report               | main session, idle                             |

The first four crowd together inside the main session's long wait, and the prologue's two scripts end 20 seconds apart, so one end comes right behind another for the same session. The subagent's end reaches the main session only through its report, as late as the report takes to write, so it ends a minute earlier and is left out of the rows that compare when ends arrived. The main session sends the two lines their work, `S`, a minute or three after `t0`; if it comes later than `t0 + 3:00` (the readies woke nothing, say), shift every time not yet set by as much. The prologue's scripts cannot move, so their ends may then fall before the wait, which the report says. The whole run takes about twenty to twenty-five minutes.

### 1. Prologue, and both lines started

All of this is the first turn, the one the user's request started: some runtimes start a session only on a turn the user began (Claude Code's desktop app refuses one on a turn a notice or another session's message started), so both lines are started before it ends.

- Make `<dir>`, stamp, and create a task list with one item per chapter of the story; keep it updated chapter by chapter to the end.
- Try to switch your own model to the next one down for your next turn; that turn's record shows whether it took, and you switch back on it to the model and effort you started on. Not offered is a finding, not a failure.
- Start `sleeper.py --until <t0 + 8:00> --log <dir>/p.log` and `sleeper.py --until <t0 + 8:20> --log <dir>/q.log`, each with the runtime's own background execution (a shell's `&` or `nohup` wakes nothing when it ends). These stand for the `ai_coc watch` `takeover` sleeps on.
- Start the side session on the weakest model the runtime offers, in this checkout rather than a worktree of its own, since the scripts run from the checkout root: its first message only asks it to stamp `user` into `<dir>/side` and reply `ready`.
- Start the subagent on the weakest model and one step above the lowest reasoning effort, the way `takeover` dispatches its farm subagent, with the same first message for `<dir>/sub`.
- End the turn on the background scripts, without waiting, polling or scheduling anything. The two `ready` replies should each wake you while they run; if neither has by the time `p.log` ends, that end is what wakes you, and the story carries on late from there.

### 2. Sending the work

On the turns the readies start: switch the side session to the model and effort you started on. The subagent keeps its tier for the briefs, which test the tier `takeover`'s farm subagent runs on; its switch comes after its report (step 4). Once both have answered, or `p.log`'s end has woken you, send both their work, back to back; that moment is `S`. Each message names the absolute paths, its clock times, and that the first thing on every turn is the stamp into its own directory.

**The side session's work** is the standby pattern: work meant for later gets its session started now, on the weakest model, and that session keeps time itself.

1. Create your own task list for these steps and keep it updated.
2. Start `sleeper.py --until <t0 + 8:40> --log <dir>/side/timer.log` with the runtime's own background execution.
3. Run `sleeper.py --seconds 120 --log <dir>/side/fg.log` as one foreground call. A message from the main session arrives during it: write to `<dir>/side/message.txt` the clock time it reached you and how it arrived (alongside the call's result, interrupting the call, as a turn of its own after yours ended, something else). If it has not come when the call returns, end the turn; it starts the next one.
4. End the turn without waiting, polling or scheduling anything: the timer's end wakes you.
5. When it does, start a subagent on the weakest model whose first message only asks it to stamp `user` into `<dir>/nested` and reply `ready`; on its reply, switch it to `<the main session's model>` where the runtime can, then send it the nested brief below. End the turn; its report wakes you.
6. When it reports, end your turn with your report to the main session: the table it printed, what `message.txt` says, and which of these steps could not be done.

The nested brief: run `uv run --no-sync ai_coc status --last --agent <agent> --session <the side session's id>-nested --mission "harness-check: takeover start table" > <dir>/side/status.txt` from the checkout root, read `takeover`'s 講什麼 (its 開始 part), and print the takeover start table the way it shapes it: the tables `status` printed, each with a fourth column 後續 filled by the takeover rules, the notes included. `ai_coc search` may be run where a 後續 cell needs a level `status` does not print; no other `ai_coc` command. Write the same text to `<dir>/side/table.md` and reply with it.

**The subagent line's work** is how `takeover` briefs its farm subagent (`.agents/skills/takeover/references/farming.md`): a numbered list of complete commands, one step per call, nothing decided by the subagent, stop at the first error. A stopped `attack` is the hard case, since it exits 0 with an ordinary outcome and only `state.json`'s `stop_by` shows the stop (`.agents/skills/takeover/references/runner.md`, 被叫停, 被借走). Make two briefs:

```bash
uv run --no-sync python .agents/skills/harness-check/scripts/grade.py plan --runs <dir>/sub/error --fate error
uv run --no-sync python .agents/skills/harness-check/scripts/grade.py plan --runs <dir>/sub/stop --fate stop
```

Each prints four numbered `job.py` commands of 20 seconds, steps `e1` to `e4` and `s1` to `s4`. One middle step, picked at random and not shown in the commands, fails in the first (exits 1, `result.json` says `error`) and is stopped partway in the second (exits 0, `result.json` says `done`, and only `state.json` in its runs directory shows the stop, which the next step's start clears, as a real command's claim does). The message, with the commands filled in:

```text
Work from <checkout root>; every command runs from there.
1. Start this with the runtime's own background execution, not a shell's & or nohup, and go on:
   uv run --no-sync python .agents/skills/harness-check/scripts/sleeper.py --until <t0 + 7:00> --log <dir>/sub/long.log
2. Run brief E, then brief S, each command in order, one command per call. You decide nothing: do not change, merge, skip, repeat or add a command.
   After each command, read <runs>/<label>/result.json and <runs>/state.json, <runs> and <label> being that command's --runs and --label.
   Stop the brief you are in, and go on to the next, when a command exits non-zero, when its outcome in result.json is not done, or when stop_by in state.json is set after it: a stopped run exits 0 and says done, and only stop_by shows the stop.
   Brief E:
   <the error brief's commands>
   Brief S:
   <the stop brief's commands>
3. Wait until <dir>/sub/long.log has its exit line, in foreground waits that each return when it appears or after at most 150 seconds, with each call's timeout set above that; never start a second copy of step 1.
4. End your reply with one line
   long token=<the token on that exit line>
   then one line per step, either
   <step> exit=<exit code> outcome=<outcome> token=<token>
   or
   <step> not run
   and after each brief's step lines one line:
   ended <e or s>: finished | error at <step> | stopped at <step> by <stop_by>
```

Step 3 is how `takeover`'s farm subagent waits on `attack` (`runner.md`, 開跑跟等): one call has a limit (Claude Code moved a subagent's foreground call to the background at 180 s whatever its timeout, 2026-11-08), so the wait comes in segments shorter than it while the command itself runs in the background; a script run as one plain foreground call dies at that limit.

### 3. The message during a foreground call

Watch `<dir>/side/fg.log`; about thirty seconds after its `start` line, send the side session one short message with the clock time you sent it written in it, and write the same time to `<dir>/side/message-sent.txt`. `AGENTS.md` has whoever hears a stop order run `ai_coc stop` themselves, so what matters is when such a message reaches an actor that is inside a long call. If `fg.log` has not started within two minutes of `S`, send it anyway and say so.

### 4. The long wait

`takeover`'s prep holds the last five minutes before a timer runs out, and on a runtime whose background ends wake nothing it spends them in one foreground `ai_coc watch` on the main thread. Run `sleeper.py --until <t0 + 10:30, or six minutes from now if that is later> --log <dir>/wait.log` as one plain foreground call on the main thread, its timeout set above the wait. The prologue's two scripts, the side session's timer and the subagent line's report all end while it runs. When the call returns, stamp `mid` with `--log` for `wait.log` and for each end that came with the result. This measures both whether one call of six minutes or more survives (not cut off, not moved to the background) and how ends that arrive while the main thread is busy reach it: inside the call, or a turn of their own afterwards, and how late.

Then end the turn, stamping each wake as it comes, until the subagent line and the side session have both reported. The side session's other turn ends, and its subagent's, may reach you as well; stamp them like any wake, since they show which ends a runtime passes up to the session that started the work. Save the subagent's reply verbatim as `<dir>/sub/reply.txt`. Then switch the subagent to the model and effort you started on, where the runtime can switch a running subagent's model, and send it one more message asking which long token it reported, to be answered from what it already holds without opening any file: its record shows the switched model on that turn, and the answer shows the same subagent carried on.

### 5. If a background end woke nothing

If any background end or report reached its actor neither as a turn nor inside one, try the runtime's own scheduler, which is what `share-emulator` tells such a runtime to set: one wake four minutes ahead, moved to nine minutes ahead, then end the turn. A scheduled wake two or three minutes late is normal, so the two times sit far enough apart that a late old one cannot pass for the new one. It passes with one turn at the new time and none at the old one; afterwards delete it and check the runtime no longer lists it.

### 6. Epilogue

- Open `tests/frames/hud_day_build_panel.png` through the runtime's own way of reading files, and write to `<dir>/image.txt` which screen it is, the name on the panel's top row and the gold count. The answers are not written here, so the file name alone cannot pass it; judge them against the image yourself. `upgrades` reads the build panel off a frame before every upgrade, so a runtime that cannot do this can farm but never upgrade. How well the model reads is not measured.
- Render a small HTML view of the summary table where the runtime can show one, and note how (inline, a page, a file).
- Send `<dir>/image.txt` to the user with the runtime's file-sending tool and judge the row from that tool's own result; never wait for the user to answer.
- List the kinds of subagent the runtime offers, as it names them.
- Decide the rows (The report), then clear everything: the side session ended and archived where the runtime can, no subagent still running, no `sleeper.py` or `job.py` process (the pids are in the logs and journals), no scheduled wake. Check each yourself rather than trusting a tool that said so, and name anything left on purpose. The results directory stays for the user.

A failed row is a finding to report, not something to fix here: changing a skill to suit a runtime is a separate piece of work.

## The report

`report.md` in `<dir>` and in the reply, in Traditional Chinese since it is for the user. A header names the runtime and its version, each actor's model and effort as recorded, the date, and the run's start and end. Then one summary table with the columns 能力 | 結果 | 不通過的話託管會怎樣 | 證據, 結果 being 通過, 不通過, 沒有這功能 (the runtime has no way to do it) or 沒跑; then the takeover table the nested subagent printed, as it printed it; then details, lateness and anything odd. Before deciding the rows, run:

```bash
uv run --no-sync python .agents/skills/harness-check/scripts/grade.py wakes --dir <dir>
uv run --no-sync python .agents/skills/harness-check/scripts/grade.py report --runs <dir>/sub/error --report <dir>/sub/reply.txt
uv run --no-sync python .agents/skills/harness-check/scripts/grade.py report --runs <dir>/sub/stop --report <dir>/sub/reply.txt
uv run --no-sync python .agents/skills/harness-check/scripts/grade.py table --status <dir>/side/status.txt --table <dir>/side/table.md
```

The rows, in story order, each with what decides it and what a failure costs `takeover` (the third column):

| 能力                          | decided by                                                                                                                                                                                   | if it fails                                                     |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------- |
| 主 session 任務清單           | the record shows it created and updated chapter by chapter                                                                                                                                   | the user cannot follow a long takeover                          |
| 主 session 換自己的模型       | the next turn's recorded model                                                                                                                                                               | none for takeover; noted                                        |
| 開 session 指定最弱模型       | the side session's first turn's recorded model                                                                                                                                               | later work cannot be parked cheaply                             |
| session 換模型                | its turn after the switch, recorded                                                                                                                                                          | parked work runs on the weak model                              |
| session 回覆叫醒主 session    | `ready` and the final report each got a `turn` or `mid` stamp in `turns.log`; name any that did not and how you learned of it                                                                | a code-change session's report is missed                        |
| 傳訊息交辦給 session          | the side session stamped `user` and did the work                                                                                                                                             | no session can be briefed after it starts                       |
| session 自己的任務清單        | its record                                                                                                                                                                                   | noted                                                           |
| 背景計時器叫醒閒置的 session  | `wakes`, side/timer.log; hold its lateness against the five-minute prep, which confirms seconds after the timer, so a late wake slows every confirm                                          | the standby pattern and sleeping on `watch` fail                |
| 醒來的回合開 subagent         | the nested subagent ran, on the model and effort its record shows, switched or not                                                                                                           | a woken takeover cannot dispatch farming                        |
| subagent 回報叫醒 session     | the side session's stamp for the nested report                                                                                                                                               | a farm report is missed                                         |
| 託管開始的表                  | `table`; whether each 後續 is the right judgment is `skill-evals`' job, not this row's                                                                                                       | the start report comes out wrong                                |
| 開 subagent 指定模型跟 effort | the subagent's record against what was asked                                                                                                                                                 | farming runs on the expensive model                             |
| subagent 回覆叫醒主 session   | its `ready` and its report each got a stamp                                                                                                                                                  | a farm report is missed                                         |
| subagent 換模型               | its turn after the switch, recorded                                                                                                                                                          | noted                                                           |
| subagent 接第二則訊息         | the same subagent, not a new one, did the work and answered the last message with its token from memory                                                                                      | a farm subagent cannot be re-briefed                            |
| subagent 等完長指令           | `long token=` matches `sub/long.log`'s exit line, the report came after it, and its `alive` lines ran on through the other ends; note the longest single call and any move to the background | an `attack` is cut off mid-battle                               |
| 每步照實回報 (E, S 各一列)    | `report` for each brief                                                                                                                                                                      | the loop goes on from a failure it was told was a success       |
| 失敗講成失敗                  | `report`, error brief; a sentence in the reply calling the failed step a success fails it whatever its lines say                                                                             | as above                                                        |
| 第一個錯就停                  | `report`, error brief                                                                                                                                                                        | steps run after an error                                        |
| 看出 exit code 藏住的叫停     | `report`, stop brief                                                                                                                                                                         | a stopped loop restarts itself                                  |
| 照寫的跑 (E, S 各一列)        | `report`, and the subagent's calls in its record: one call running two commands fails it                                                                                                     | the brief is quietly changed                                    |
| 前景呼叫中收到訊息            | `side/message.txt` against `message-sent.txt`: delay and how it arrived                                                                                                                      | a stop order waits for the call to end                          |
| 一個結束緊跟另一個            | `wakes`                                                                                                                                                                                      | one of two close ends is swallowed                              |
| 主執行緒一次前景等六分鐘      | `wait.log` has its exit line and your `mid` stamp for it came right after; otherwise what ended the call and after how long                                                                  | the five-minute prep cannot block on `watch`                    |
| 忙的時候到的結束              | `wakes`: inside the call or a turn of its own, how late after the wait returned, in the order they ended                                                                                     | an upgrade or a report waits behind a busy turn                 |
| 排程器備援                    | step 5, or 沒跑 when every end woke                                                                                                                                                          | a runtime that wakes on nothing has no fallback                 |
| 看圖                          | `image.txt` against the image                                                                                                                                                                | no upgrades                                                     |
| 畫 HTML                       | it showed                                                                                                                                                                                    | noted                                                           |
| 傳檔案給使用者                | the send tool's own result                                                                                                                                                                   | frames never reach the user                                     |
| subagent 種類                 | the list                                                                                                                                                                                     | noted                                                           |
| 收乾淨                        | your own check of processes, sessions, subagents and scheduled wakes                                                                                                                         | stray waiters act at their old moment beside their replacements |
| 等核准                        | every command or tool any actor stood waiting on an approval for, as the runtime showed it or the record holds it                                                                            | an unattended takeover halts on the first one                   |

Each row resting on a cheap model's work also names the model and effort its record shows, since a pass holds only for that tier, and says it is one sample. The approvals row is the one place the run needs the user: nobody else can give an approval, so an actor standing on one waits for them, they get a one-line push saying so where the runtime can push, and the row says where.
