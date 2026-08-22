---
name: gh-review
description: >-
  Address every suggestion on the current GitHub Pull Request, or external review output the user pastes into the chat. Fix, commit, and resolve reasonable ones; reply with reasoning and leave the rest open.
---

# gh-review

Address every actionable conversation on the PR of the current branch. Suggestions live in two places on GitHub and people often forget one of them:

- **Review threads** — anchored to code. Resolving one requires its thread id, which only the GraphQL API exposes.
- **Issue-style PR comments** — top-level, unanchored; some bots post their review summary here. These cannot be resolved, only replied to.

Collect both, then for each item decide: act on it, or push back.

**Two input sources.** Either the GitHub PR above, or review suggestions the user pastes into the chat (copied from tools like codex / opencode — severity tags, `file:line` anchors, reasoning). Pasted mode runs the same flow minus the GitHub-specific steps: nothing to collect, resolve, or reply to on GitHub — judge each pasted item, read the actual referenced file (not just the snippet), fix/commit/push the same way, and report the outcome back to the user.

## Ground rules

- PR mode: the PR is whatever the current branch points to. No PR on this branch → fail loudly; do not guess and do not accept a PR number. Pasted mode: there is no PR — the pasted text is the input.
- Before changing anything: working tree clean. PR mode also requires local HEAD matches the PR head and current branch is the PR branch; pasted mode just needs the clean tree and pushes to the current branch's upstream. Otherwise stop and tell the user.
- Reasonable suggestion (or you see a clearly better fix for the same concern) → apply it, commit it as its own commit, resolve the thread after pushing.
- Right but out of scope → agree in the reply, name where the fix belongs, and leave the thread open. A branch carries one intent, and a fix for something it did not introduce is a line the next reviewer cannot weigh against that intent, so it goes in its own change however small the edit is — offer to open an issue rather than quietly carrying it.
- Unreasonable → reply with the actual reason, leave the thread open for the human to decide.
- Skip: threads you authored yourself, pure status bots (CI, coverage, deploy previews), and summary blocks with no actionable content. Code-review bots' comments usually do contain suggestions — read them carefully.

## Judging

Every suggestion is advisory, not an order — the deciding judgment is yours. Whether it comes from a GitHub reviewer, a bot, or a pasted external tool (codex, opencode), treat each item as a claim to check, not a task to carry out: decide whether a change is actually warranted, whether the suggestion misreads the code, or whether the current code is already right. Disagreeing and leaving it as-is — with your reasoning — is a valid, expected outcome. Never edit just because something was flagged.

Read the file at the anchored path, not just the diff hunk — the hunk window often hides context that changes the answer. Bots are wrong often: stale language assumptions, fixes that contradict repo conventions or introduce new bugs. Note threads marked outdated may already be addressed by later changes — judge, don't mechanically resolve.

If a suggestion targets the PR description rather than code, edit the PR body instead. After applying accepted changes, check whether the body has gone stale and update it. The PR body must be entirely in English.

## Judging in parallel

Judging is the expensive half of this skill and it is per-item: each suggestion means reading a whole file, and no item's answer depends on another's. Past a couple of items, hand each one to its own subagent, all launched in the same message.

A worker never loads this skill, so its prompt has to carry what it needs: the suggestion verbatim, the path it is anchored to, and the standard from "Judging" above, that the suggestion is a claim to check rather than a task to carry out, and that "the code is already right" is a valid answer. It reads and decides. It writes nothing at all: no edits, no commits, no `gh`.

```
item | accept / reject / out-of-scope | one-line reason | if accept: the exact edit, file:line
```

Applying them stays here, and not for tidiness. One commit per accepted suggestion is an ordering constraint on a single shared index, and two suggestions land in the same file often enough that parallel editors would clobber each other. Workers judge, you act.

Read the file yourself before acting on a verdict you did not reach. A wrong accept shows up in the diff, but a wrong reject closes a real finding with a confident reason attached, and nobody looks again.

## Committing

One commit per accepted suggestion — do not batch. Messages in English, conventional commits, short, no implementation details. If the repo has a lint or typecheck entry point, run it once over the final state before pushing; fix failures as additional commits, do not amend. If nothing exists, skip — do not invent a check. Push once at the end.

## Closing the loop

Wait until the push lands so replies can cite the commit:

- Accepted review thread → short reply (`Done in <sha>.`, plus one line if you deviated from the proposed fix) and resolve.
- Accepted issue-style comment → reply on the PR citing the sha.
- Rejected → reply with the concrete reason, do not resolve. Be respectful even to bots — a human reads these later.

In pasted mode there is nothing on GitHub to resolve or reply to — the report below is the whole closing step.

Finish with a compact report: the PR (or pasted source), commits pushed, then each item as accepted (→ sha), deferred (where it goes), rejected (reason), or skipped (reason).

## Hard limits

- Never `--force` or `--no-verify` unless the user explicitly asks.
- Never resolve a thread without the change actually existing on disk — and if it was already addressed before you arrived, say so in the reply.
- Ambiguity mid-flight (reviewers disagree, suggestion conflicts with the user's intent, fix larger than a small in-place edit) → stop and ask instead of guessing.
