---
name: gh-spawn-task
description: >-
  Fan a batch of GitHub issues out into one isolated session each, so the user starts each with a single click instead of opening and briefing them by hand. Desktop only — it needs a runtime that can spawn a session on its own git worktree (Claude Code Desktop's spawn_task, Codex's Worktree chats), which no CLI can, so say so up front when it cannot. Use whenever the user points at more than one issue and wants them worked, however they phrase it: a range ("解掉 #507 到 #536"), a parent issue whose sub-issues are the work ("幫我完成下面全部 issue" with one issue URL), "把這批 issue 分 session 處理", "open a session for each of these". A lone issue URL is this skill when that issue has children, and gh-dev-flow when the issue is the whole job. Reach for it even when the user never says "task" or "session" but the shape is a batch handover. Not for google3, gpar or any other host.
---

# Fanning a batch of issues out into one session each

The user has just filed a batch and wants each issue worked in its own session. They are handing the whole thing over, usually because they are tired of it: the reason they asked for this rather than doing the issues themselves is that they do not intend to read the PRs either.

That sets the bar. A task they have to think about before clicking has failed, and so has a session that stops halfway to ask a question. Each one carries everything a fresh session needs, and reaches an ending on its own — the change merged, or the issue closed with the reasoning that says why it should not be done.

## What the runtime has to provide

Two things, and both are desktop-app features rather than anything a CLI has:

- **A way to hand the user a session they start with one click**, carrying a prompt you wrote. In Claude Code Desktop this is `spawn_task`, from the app's own MCP server (`ccd_session`). `claude mcp list` in a terminal shows no such server, so a CLI session cannot do this at all.
- **Its own git worktree per session.** This is what makes a batch safe to run at all: ten sessions editing one checkout would trample each other, while separate worktrees give each its own files and its own branch off the same base. Verify rather than trust it, but not straight after spawning: what you spawn is an offer, and the worktree comes into existence only when the user starts it. Once they have, `git worktree list` should show one entry and one distinct branch per started session.

Codex's desktop app has the second half but not the first: its Worktree chats are started by the user picking Worktree under the composer, and nothing there lets an agent raise one on their behalf (see https://learn.chatgpt.com/docs/environments/git-worktrees). So there the handover has to be a list the user acts on rather than tasks you spawn. Everything below still applies — the method is the handover, not the button.

When the runtime has neither, say so and let the user re-run from a desktop app. Working through the issues one after another in the current session is not a smaller version of this — it is the thing they asked not to happen.

## Build the work list

Everything below runs on one flat list of issues, and building it is the same three steps whatever the user named — a range, one URL, a handful of numbers. Keeping it a single path is not tidiness. A range that happens to contain a parent is an ordinary input, and a design with a branch per input shape spends a session on the container and never reaches its children at all.

**Resolve what was named.** A range takes one listing, filtered:

```bash
gh issue list --repo <owner>/<name> --state open --limit 500 \
    --json number,title \
    --jq '[.[] | select(.number >= <start> and .number <= <end>)] | sort_by(.number) | .[] | "\(.number)\t\(.title)"'
```

`gh issue list` already excludes pull requests, so whatever comes back is workable. For numbers in the range that did not come back, one pass with `--state all` separates the closed ones from the rest; anything still missing is a PR or was never created.

**Ask every issue for children, including the ones that arrived as children.** A listing cannot carry that field, so this is one call each:

```bash
gh api repos/<owner>/<name>/issues/<n>/sub_issues --jq '.[] | "\(.number)\t\(.title)"'
```

`gh issue view <n> --json subIssues,subIssuesSummary,parent` reads the same relationship, and the summary's completed count cheaply skips children that are already done. One call per issue is what it costs never to miss a layer; on a large range, say what that costs rather than quietly skipping the step.

**Flatten and dedupe.** From here the tree is gone and an issue is just an issue. That is what lets the collision test below compare a child against an unrelated issue from the range, which a per-shape design would never think to do because it would never hold both in one list.

Then take out what is not work: PRs, closed issues, gaps, anything that already has a session or an open PR closing it, and containers. For the first of those the runtime's session list (`list_sessions` in Claude Code Desktop) shows what is in flight, and the titles carry the issue number when they were made this way.

A container is a parent whose work lives entirely in its children. It gets no session, and a child's PR closes the child alone since the parent closes when its children do. Read the body before dropping one, though: a parent can carry work of its own *and* have children, and a rule applied as "parents are dropped" deletes that work without saying so.

Two things do not follow from the tree, and both cost something if assumed. "Same parent" is not evidence that children collide — a split can be good or bad and its provenance does not say which, so the test below still has to read the children themselves. And a child read alone loses the shared premise, what is being changed overall and what deliberately stays, which lives in the parent's body: a child's prompt carries its parent's number and an instruction to read it.

Say what you dropped and why when you report back. Expansion can make the list longer than the range the user named, so nine sessions for six numbers looks arbitrary until you say which one expanded and what fell out.

## Group the ones that will collide

The list is also where you decide what must *not* be split. Isolation is per session, so two sessions working the same code never see each other: both branch off the same commit, both go green, and the collision surfaces only when the second one merges. A textual conflict is the cheap version of that, and git says so plainly. The expensive version is two changes that merge cleanly and are wrong together — one renamed what the other relies on, or both introduced the same helper twice — because each branch's CI passed against a base that no longer exists and nothing ever tested the combination. Under this skill's closing block nobody is reading those PRs either, so that lands on the default branch unchallenged.

Two signals cost nothing and are available before reading any code:

- **The same scope in two titles.** Repos that write conventional-commit subjects hand you the module for free: `fix(opencode):` twice, or two `chore(core):` sweeps, is two issues in one place.
- **The same files named in two bodies.** Issues filed out of a review usually name what they touch, so read the bodies of whatever the first signal flagged.

On a long list, reading every body is real context, and fanning that reading out to subagents is tempting. Fetching can go to them — one worker per issue, returning the files and modules its body names. The grouping cannot. Whether one issue collides with another is a relation, and a worker that only ever saw one of the two cannot report it, so synthesising over their summaries is combining reports that already dropped the evidence. Take facts from workers if it helps; make the pairings yourself.

Put what those turn up into one session that works them in order and opens a single PR closing all of them. A context that can see every one of those changes at once is the only thing able to get the combination right. Title it for the group — `Issues #182 #193: <what they share>`.

When it is a close call, group. Over-grouping costs a little parallelism; under-grouping puts a broken combination on the default branch.

If most of the list collapses this way, say so rather than quietly shipping one enormous session. It usually means the issues were split along the wrong seam to begin with, and that is the user's call, not yours.

## Tell the blocked one what it is waiting for

Some issues cannot start rather than cannot share: "close the issues these removals made unactionable", "delete the code that replaced it", "update the docs once the rename lands". The work does not exist until the others are in. That is not a collision — grouping would fix it too, but it does not have to, and a batch reads better when only the genuinely entangled ones are merged.

Give such an issue its own session and put the wait **in its own prompt**, naming the issues it is waiting on and telling it to hold until every one of them is closed before touching anything. Have it wait the way its runtime watches a condition in the background — a backgrounded loop that exits when the last one closes — rather than a foreground sleep, which some runtimes refuse outright. Tell it to re-read its own issue body once it wakes, because the thing it was asked to do may have shifted while it waited.

The wait belongs in the prompt and nowhere else. The session that spawns the batch finishes and stops as soon as the batch is out, so anything built on that session still being around to notice and wake the blocked one will simply never happen — it was observed happening exactly that way, and the blocked issue sat until a human started it by hand. A constraint that lives with the thing it constrains needs nobody to survive.

## The local checkout

A task starts in a working directory you give it. The user's checkouts live at `~/repo/<name>`, which usually matches the repo name, but confirm it with `ls` rather than assuming, because the request often names a different repo than the one you are currently sitting in. Without a checkout there is nowhere to point the task, so say so and ask instead of guessing.

## One task per issue

The field names here are Claude Code Desktop's; another runtime will spell them differently but wants the same four things — a title, a one-line summary, the prompt, and where to start.

**The `title` becomes the session's title**, which is how the user picks it out of a list of twenty later. Write it as `Issue #123: <short imperative>` and keep the whole thing under 60 characters, so roughly 45 for the part after the number.

Write `tldr` in whatever language the user is speaking, plain, no paths. It is a tooltip, not a spec.

The `prompt` is the only context that session will ever have, so nothing can be left implicit. It needs:

- The repo slug and the local checkout path.
- The issue number and title, plus an instruction to read the full body with `gh issue view <n>` rather than working from the title alone.
- `follow the gh-dev-flow skill end to end`, which owns branching, commits and the PR itself.
- Whatever that repo requires before a PR. Read its CLAUDE.md or CONTRIBUTING for this instead of carrying commands over from another repo; a formatter or a pre-commit invocation that is right for one project is wrong for the next.
- The parent's number and an instruction to read it, when this issue came in as a child.
- The wait, if this one is blocked on others, as above.
- The closing block below.
- A line keeping the session inside its own issue. Separate worktrees stop the sessions from corrupting each other's files, but nothing stops one from "helpfully" fixing a neighbouring issue and silently stealing another session's work.

## The closing block

Unless the user states their own, end every prompt with this, adapted to the repo:

> Once the draft PR is up, run the `code-review` skill over the change and act on the findings that hold up; for the ones that do not, say why and leave the code alone. Then make sure every CI check passes, mark the PR ready, and merge it (never a rebase merge).
>
> If you turn up an unrelated problem along the way, it is yours to judge: fix it in passing when it is small and sits in the code you are already changing, or file it with the `create-issue` skill and leave it. Either is fine; carrying it silently is not. A problem that is plainly another issue in this batch is not yours at all — that one has an owner already.
>
> You are also allowed to decide the issue should not be done. If the problem has already gone, the code it describes no longer exists, or the change it asks for would make things worse, close the issue with what you checked and what you found, and stop there. That is a real outcome, not a failure to deliver. Difficulty is not one of those reasons: "harder than it looked" means keep going. Where you were given several issues, judge them one at a time: close the one that should not be done, note that in the PR carrying the others, and finish those. The PR only goes away if every issue you were given falls away.
>
> Sibling PRs are merging into the default branch around the same time, so if it moves ahead and your branch falls behind or conflicts, update the branch and let CI go green again before merging.

Each part is load-bearing. The review step is there because nobody else is going to look at the diff. Acting only on findings that hold up matters more than usual here: with no human in the loop, a session that changes working code to silence a wrong review comment buries that mistake in the history unchallenged. The middle paragraph closes the escape hatch that unattended work otherwise leaks through — a session that notices something and mentions it only in a final report has effectively thrown it away, because nobody is reading that report either; giving it two acceptable outcomes and ruling out the third is what makes the choice safe to delegate. And the last paragraph exists because the first PR to land makes every other branch stale, which is invisible from inside any one session.

The right to decline is there because the alternative is worse than doing nothing. An unattended session that has worked out an issue is pointless has only bad moves left if the prompt does not give it this one: build the change anyway and merge something nobody wanted, or stop and write an explanation into a transcript nobody opens. Closing the issue with the reasoning is the only ending that leaves the finding somewhere a person will see it, and it costs nothing to undo — reopening is one click, which is what makes the authority safe to hand over. The evidence requirement and the exclusion of difficulty are what keep it from becoming the exit from anything unpleasant.

The merge authority is narrower than it looks, for the same reason. It covers a green PR that does what its issue asked, and nothing else. A session that cannot get CI green has not earned it and should leave the PR open and red rather than merge something it does not believe in.

## Spawned tasks are write-once

A task's prompt cannot be edited after it is created, so a requirement added once a batch is out means spawning the whole set again with the new prompt and dismissing the old ids.

Work out what has already started **before** you respawn, not after. Anything the user has clicked is a running session, and respawning that issue hands them a second offer for work already underway; the check is the same one either way, but run late it only tells you the duplicate exists.

Two things about the pending list make this easy to get wrong:

- **It evicts.** Spawn around ten and the oldest silently drop off the list the tool result prints. One you just created can vanish from that list while still existing, and one you meant to create can be gone. Do not read it as the record of what exists.
- **A dismiss on a consumed task reports `already dismissed`**, which does not distinguish "the user dismissed it" from anything else.

So confirm what actually launched from the sessions themselves, not from tool results. Each session writes a transcript under the runtime's own directory (`~/.claude/projects/` for Claude Code Desktop), and the prompt you wrote is in it:

```bash
rg --hidden -l "<a distinctive phrase from your prompt>" ~/.claude/projects/
```

One file per session that really received it, plus the current session, since you composed the text here. Grep each for its issue number to confirm one per issue and no duplicates. Full-text search over transcripts (`search_session_transcripts`) lags by several minutes and reports nothing for sessions already running, so it is no use for this.

## Report back

A table of issue number against session, what expanded into what, the numbers you took out and why, and anything left needing the user. Keep it to what they would have to go look up otherwise.
