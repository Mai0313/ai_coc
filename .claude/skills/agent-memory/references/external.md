# External Memory Store

Read this when routing in `SKILL.md` sent you here. The memory directory is itself a clone of a
private GitHub repository, so a memory your own mechanism wrote is already in a working tree and
this file covers the only part nothing else does: getting it committed and pushed, and picking up
what other machines pushed.

## Core rule

The repo is private, but it still sits on someone else's servers and its history is permanent.
Credentials, tokens, customer data, and anything under an NDA stay out regardless of what your own
mechanism decided to record.

## Where it lives

`~/.memories` is a clone of `<login>/memories`, and it is the same directory your memory mechanism
writes into, one markdown file per topic at the root plus the index it maintains. Subdirectories are
fine once a subject grows enough to need one, and filenames are kebab-case.

The owner is never hardcoded. Resolve it from the authenticated account:

```bash
gh api user --jq .login
```

The fallback, if that fails, is `gh auth status --json hosts --jq '.hosts | to_entries[] | .value[] | select(.active) | .login'`, which returns the same login. Scraping the plain `gh auth status` text is not a fallback: its `Logged in to github.com account <login>` line is human output, and matching against it breaks the day the wording changes.

No `gh` on PATH, or a non-zero `gh auth status`, means this machine has no store to sync with. Stop
there and leave the memory where it is. Do not improvise a substitute destination, and do not append
memories to whatever repo you happen to be sitting in.

## Setup

Once per machine, and also the recovery path when `~/.memories` is missing:

```bash
login=$(gh api user --jq .login)
gh repo view "$login/memories" > /dev/null 2>&1 || gh repo create "$login/memories" --private
gh repo clone "$login/memories" ~/.memories
```

Creating the repo needs no permission, but say in one line that you did it. Always qualify it as `$login/memories`: a bare `memories` resolves against whatever remote context you are in and can land on a different account's repo. `--private` is not a preference, it is the reason the store is usable at all.

When `~/.memories` already exists, confirm it is the right clone instead of assuming:

```bash
git -C ~/.memories remote get-url origin
```

A directory that is not a git repo, or one pointing elsewhere, is the user's to sort out. Do not delete it, do not re-clone over it.

A freshly created repo has no commits and no default branch, so cloning it warns `You appear to have cloned an empty repository` and drops you on whatever local `init.defaultBranch` says, which is still `master` on plenty of machines. Pin the branch before the first commit, or two machines bootstrap two different default branches and the store silently splits in half:

```bash
git -C ~/.memories checkout -B main
```

On a clone that already has commits this is a no-op, so run it unconditionally rather than trying to detect which case you are in.

## Syncing

Commit before you pull, not after. Your mechanism has already written the memory and updated the
index by the time you get here, so the tree is dirty and `pull --rebase` refuses to run at all
(`cannot pull with rebase: You have unstaged changes`, exit 128). Reversing the order is what keeps
that from silently ending every sync before it starts.

1. Commit what your mechanism just wrote.

    ```bash
    git -C ~/.memories add <name>.md MEMORY.md
    git -C ~/.memories commit -m "docs: add <name>"
    ```

    Add the files by name rather than `git add -A`, which sweeps up whatever another session left
    lying in the tree. The index is the exception you cannot avoid: every write touches it, so it
    has to be in that line, and it may carry a line another session added moments ago. That is
    harmless, since the entry points at a file that session is about to commit.

    Commit messages follow conventional commits and stay in English, same as any other repo.

2. Rebase onto what other machines pushed.

    ```bash
    git -C ~/.memories pull --rebase
    ```

    Your commit lands on top of theirs rather than forking the history. This is also where memories
    written elsewhere arrive, index included, so nothing extra is needed to read them.

3. Push, then verify.

    ```bash
    git -C ~/.memories push
    git -C ~/.memories status -sb
    ```

    The first line reads `## main...origin/main` with no `ahead` marker when the push landed.
    Committing and failing to push is the one failure mode that leaves the memory looking saved
    while no other machine can see it, so do not skip this.

    A push rejected as non-fast-forward means another machine pushed in between. Repeat step 2 once,
    push again, and stop. A push that fails on credentials is the user's to fix, `gh auth setup-git`
    being the usual answer. Report it and stop rather than editing their git config yourself.

4. When the rebase conflicts, another machine edited the same memory. Do not resolve it by hand, and
    do not stop at the abort either:

    ```bash
    git -C ~/.memories rebase --abort
    [ -z "$(git -C ~/.memories status --porcelain)" ] && git -C ~/.memories reset --hard origin/main
    ```

    Aborting alone leaves your commit sitting on the branch at `ahead 1, behind 1`, and every later
    session's `pull --rebase` then hits the same conflict and aborts again. That wedges the clone
    permanently, so the reset is what actually ends it. This one `reset --hard` is the sanctioned
    exception to the rule in Known traps, which is why it is guarded on a clean tree; a dirty tree
    means someone else's work is in there, so leave it alone and tell the user instead. Never
    `git push --force` and never rewrite pushed history.

    Discarding your own commit does lose that memory here, so say in one line which one it was.
    Never let any of this become a task failure or delay your reply: the work the user asked for is
    already done, and a sync that did not land is worth a closing sentence, not a problem they have
    to solve first.

## Known traps

- **A stale clone looks exactly like a current one.** Reading without pulling shows whatever your last pull left behind, which can be weeks old. `origin/main` is what other machines actually see.
- **Deletion is not erasure.** `git rm` plus a push removes the memory from the tree, but the content stays in history forever and stays reachable to anyone with repo access. That is the real reason the secrets rule above is absolute rather than tidy.
- **The repo must stay private.** Never `gh repo edit --visibility public`, at any point, for any reason.
- **This is not the skills repo.** `~/.agents` is a public repo that happens to hold this skill. Memories never go there.
- **The clone is shared by every agent session on the machine.** No `git stash`, no `git reset --hard`, no force push. A concurrent session's uncommitted work is invisible to you and trivially destroyed by any of the three. The single exception is the guarded reset that unwedges an aborted rebase, in step 4 of Syncing.
