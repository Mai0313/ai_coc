---
name: code-review
description: >-
  Defect-first code review of the local checkout: uncommitted changes, a base-branch diff, a single commit, or custom review instructions. Returns prioritized P0-P3 findings anchored to file:line plus an overall correctness verdict. Read-only, reports into the chat only.
---

# code-review

A review in the style of the `/review` command from [codex](https://github.com/openai/codex): its rubric, its priority ladder, its acceptance bar. The name describes the methodology, nothing more. There is no Codex CLI here, no `codex` binary to shell out to, no external review service. You run the review yourself, against the checkout in front of you, using the rubric this skill ships.

Read the change, judge it against that rubric, hand back prioritized findings. Nothing else: no fixes, no commits, no comments on any review system.

Maintainers: [PORTING.md](PORTING.md) records where each piece came from and how to re-sync when codex moves.

The property that makes it work is isolation. Codex runs the review in a fresh sub-session that never sees the parent conversation, with the rubric replacing the system prompt entirely. Reproduce that here, or the review degrades into the same agent agreeing with itself.

## Pick the target

Four targets, same as codex's presets. Hand the reviewer the matching prompt:

```text
Base branch Review the code changes against the base branch '<branch>'. The merge base commit for this comparison is <sha>. Run `git diff <sha>` to inspect the changes relative to <branch>. Provide prioritized, actionable findings.

Uncommitted  Review the current code changes (staged, unstaged, and untracked files) and provide prioritized findings.

Commit       Review the code changes introduced by commit <sha> ("<subject>"). Provide prioritized, actionable findings.

Custom       <the user's instructions, verbatim>
```

Take the target from the request when it says one. Free-text instructions are Custom, used as-is. When the request does not pin a target down, offer the four in that order rather than guessing, because reviewing the wrong range wastes the whole run.

For a base branch, resolve the comparison ref first, then merge-base against it. Use the branch's upstream only when that upstream exists **and is strictly ahead** of the local branch (`git rev-list --left-right --count '<branch>...<branch>@{upstream}'`, right side greater than zero); in every other case, including no upstream at all, compare against the local branch. Then `git merge-base HEAD <ref>` and diff against that sha, never against the branch tip, so the review covers what would actually merge. Default branch guess is `main` then `master`, whichever `git rev-parse --verify --quiet` finds first.

Resolve the target before spawning anything. Not a git repo, no such branch or commit, or an empty diff → say so and stop. Do not quietly review a different range.

## Run the review

Spawn exactly one subagent. Give it two things and nothing else:

- The absolute path to `references/rubric.md` next to this SKILL.md, told to read it and treat it as its complete instruction set for the task.
- The resolved target prompt from the block above.

Withhold everything else. Not what you were building, not what you already fixed, not which findings you expect, not the fact that you wrote the code. That context is exactly what biases a reviewer into confirming its own work.

One reviewer, not a panel. Codex's acceptance bar ("prefer outputting no findings" over a marginal one) assumes a single judgement; fanning out across lenses produces a different, noisier artifact. Use the highest reasoning effort available. Keep it local: no web search, no fetching, no consulting an external review service.

The reviewer works from the repo on disk. It should read the applicable `CLAUDE.md` and `AGENTS.md`, inspect the full diff plus enough surrounding code to understand each changed path, and check tests and call sites to confirm each finding is real before reporting it.

## Report back

Relay every finding the reviewer returned. Do not drop one, do not merge two, do not soften a priority because the code is yours.

Keep its shape: the summary line, then numbered findings under `## Findings` in severity order, each a bold `[P1] Imperative title` line, its `path/to/file.rs:118-122` location, and one paragraph. Then `## Verdict`. The rubric already specifies all of it, so relaying the report as it came back is both the least work and the best result. Reformatting it only risks losing something.

Two outcomes look alike from the outside and must never be conflated. A reviewer that ran to completion and found nothing qualifying reports `No findings.` followed by a verdict: relay that, it is the honest answer. A reviewer that was interrupted, errored, or came back without a report has not reviewed anything: say the review did not complete and that it has to be re-run. An incomplete run is never reported as a clean one, and a missing `## Verdict` section is the tell.

Then stop. Findings are a report, not a task list. Acting on them is a separate request the user makes after reading it.

## Hard limits

- Read-only, start to finish. No edits, no commits, no branches, no pushes, no `gh`/`git` writes.
- No comments anywhere: GitHub, Gerrit, Critique, Buganizer, or any other tracker. The chat is the entire output surface.
- Do not fix what it finds. Applying review output is [gh-review](../gh-review/SKILL.md)'s job.
- Pre-existing problems are out of scope. Only what the change introduced counts.
- Never invent a finding to make the report look thorough, and never suppress one to make the change look clean.
