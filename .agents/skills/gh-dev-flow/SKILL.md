---
name: gh-dev-flow
description: >-
  Use when doing development work on a GitHub repository: planning a change, splitting it into commits, opening or merging a pull request, picking up an existing issue, or deciding whether something can go straight to the default branch. Not for google3, gpar, or any other host.
---

# Shipping a change on GitHub

One path from a task landing to the change being merged, with a single point where the user is needed.

The repo's own CLAUDE.md and CONTRIBUTING outrank everything here.

## The interruption budget

The user is at the keyboard for a minute or two after handing over a task, then gone. A question asked ten minutes in costs far more than the answer is worth: they are away, you are blocked, and both sides sit idle waiting for the other.

So there is **one window, and it opens once the draft PR is up and CI is green.** Everything before it runs unattended, red CI included.

Two things can open a window earlier, and both are rare:

- **PR or straight to the default branch**, when the call genuinely sits on the boundary drawn below.
- **Two viable approaches that differ a lot.** Ask which one. Not whether you may start.

Nothing else qualifies. Not a plan waiting to be approved, not a failing check, not "this turned out harder than expected."

**Break the window only when continuing would produce the wrong work** — the task's premise does not hold, the repo you were pointed at is not the one that needs changing, a credential you cannot obtain. A technical choice is not that. A hard bug is not that. Wondering whether the user will like the result is not that.

## PR, or straight to the default branch

The test is **whether breaking it takes anything else down with it**, not how many lines or files changed.

- Docs, prompts, config, a new file nothing references yet — it breaks alone. Push it.
- Shared code, build or CI config, an interface something depends on — it takes others down with it. Open a PR.

Both of those are obvious, so decide and keep moving. Ask only when the change really does sit on the boundary.

A straight push is the whole of it: make the change, run lint, format and tests, commit, push. No branch, no plan, no PR, no window, and it ends there.

**Everything below is the PR path.**

## 1. Plan

Write the plan yourself. Subagents fetch what it needs — read that file, find every call site, check what CI actually runs — but a plan is a synthesis, and a worker holding one slice cannot report a relation it never saw.

Then hand the finished plan to a **fresh subagent that did not watch you build it**, and ask it to find where the plan fails rather than to suggest improvements. It reads what you wrote instead of the reasoning that got you there, which is exactly the point: the gap you already filled in your own head is the one you cannot see.

What comes back is an argument, not a verdict — same as any review comment. What holds up, fix. What does not, say why and leave the plan alone. It is not an approval gate, so do not loop on it.

The plan reaches lint, format and tests before it reaches "open the PR."

Working from an existing issue, the plan goes into the issue body under the placeholder, or as a comment, **before the work starts**, and gets updated as it changes so the issue stays a usable trace of what happened. With no issue there is nothing to write into; keep it in the conversation.

Sign the plan with `Planned-by: <your full model name>`. In the body it joins the block `create-issue` left at the foot, below the `Filed-by` line rather than replacing it, because the session that plans is rarely the one that filed and often is not even the same model; as a comment it is that comment's last line. Everything on GitHub is posted through the user's account, so these lines are the only record of who is answerable for what.

This is also where a split that falls out of the plan gets made. `create-issue` opens a parent only where the pieces were already separate before anyone planned anything; a split the plan itself produces could not have been drawn at filing time, and this is the point it can be. Split when the plan comes out as pieces that each land on their own, one sub-issue per piece, each saying what done means for that piece alone; a plan whose three steps all ship in one PR is three commits, not three issues. Then mind which number the PR closes: `Closes` the sub-issue it actually finishes, not the parent, which stays open until the last piece lands and is what tells you which pieces are still outstanding.

Do not stop for approval here.

## 2. Off the default branch, then work

Be off the default branch before the first edit. Worktree or plain branch, whichever the runtime hands you — arriving already in one satisfies this. Do not open a worktree yourself when the CLI has one for you.

Name the branch `<type>/<short-kebab-slug>`, the type being the conventional-commits type of the change as a whole: `feat`, `fix`, `docs`, `refactor`, `test`, `perf`, `breaking` or `chore` (`ci`, `build`, `style` and `config` work too and all land on the chore label). `.github/labeler.yml` reads the head branch and nothing else, so a name without the prefix leaves the PR with no type label at all. Whatever opens the branch takes that name as an argument and invents one when it is omitted, so pass it; sitting on an invented one already, `git branch -m` costs nothing right up until the first push.

Split anything large or complex into focused commits, each a self-contained logical step. They all land in the same PR, so the count does not matter; granularity is what makes the change easy to follow, review and revert.

Commit messages: English, conventional commits, short, no implementation details.

## 3. Check your own work

Run the repo's own lint, format and tests. If it has none, skip it — do not invent a check.

Then run `code-review` over what you just wrote. Fix what holds up. For what does not, say why and leave the code alone. Update any docs the change made stale.

## 4. Draft PR, then green CI

Open it as a draft and leave it that way until every check passes. Title and body in English, body reflecting the current plan, and its foot carrying `Opened-by: <your full model name>` for the reason the issue carries one.

Red CI is yours. Read the log, push another commit, do not amend what is already pushed, and do not come asking.

Green is only evidence when the diff could have gone red. That mostly takes care of itself, except when the thing you changed **is** the CI config: those checks then pass exactly as they would have without your fix, and reading that as confirmation is how a broken fix merges. Push a probe commit carrying whatever used to fail, watch the check stay green on that, then drop the probe in the next commit. Both squash away at merge, so the cost is one round trip.

Anything you notice that belongs in a separate change: write it down, keep going, save it for the window.

## 5. The window

Draft PR up, CI green. Ask both at once, not in two rounds:

- **Ready for review, or merge it?** Skip this one if the user already said to merge on green — then just merge.
- **These follow-ups, worth issues?** Everything collected along the way, as one list. `create-issue` owns what goes in them.

## 6. Merging

Squash review fixups back into the step they belong to first. What lands on the default branch should be the steps, not the path taken to reach them.

Then: is every commit worth keeping on the default branch in its own right?

- **Yes** → merge commit, with `--subject "<PR title> (#N)"`. GitHub's `Merge pull request #N from <branch>` default says nothing, and `--subject` will not add the number for you.
- **No** → squash, which gets that subject automatically.
- **Never rebase merge.** It drops the branch commits onto the default branch with nothing left marking which PR they came from.

## 7. Closing out

Delete the merged branch, local and remote.

The worktree is yours only if you opened it in this session. Then close it — `ExitWorktree` in Claude Code, which puts the session back in the original directory before deleting, so standing in it is not a reason to leave it behind, and the tool's own advice to wait for the user does not apply here.

Any other worktree — one the runtime handed you, one from an earlier session, one made by hand — is not yours and never becomes yours. The runtime that opened it asks about it when the session ends, so there is nothing left for you to do: do not remove it, do not ask, do not hand over a `git worktree remove` command, do not raise it at all.

Record what you learned that will matter next time, then sync it with `agent-memory`.

## Throughout

- **English only** for anything that reaches GitHub: PR titles and bodies, issue titles and bodies, comments, review replies.
- Keep the issue and the PR body current as the plan moves, because plans always move.
