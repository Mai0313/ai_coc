---
name: agent-memory
description: >-
  The memory directory is a clone of a private git repository, so a memory that gets written but never pushed lives on one machine and looks identical to one that reached every machine. Read this right after your own mechanism records a memory, and whenever that directory's git is missing or refusing to move. It owns the git side alone: what deserves remembering, and in what format, stays with your builtin mechanism. What memory holds is where work got to and what was true at the time, so read it as a lead to verify rather than a source of truth; durable know-how about how a tool behaves belongs in a skill instead.
user-invocable: false
---

# Agent Memory

Your builtin memory mechanism decides what is worth keeping, writes it, and maintains its own index.
None of that changes here, and this skill never second-guesses it. Where no such mechanism exists,
this skill is not a substitute: stop here rather than adopting the repository as your memory.

What it adds is git. The memory directory is a clone, which is the only reason a memory survives a
reinstall or reaches a second machine, and nothing else in the system runs a single git command
against it. A memory that was written but never pushed has the same file and the same index entry as
one that was, visible to this machine alone. Closing that gap is the whole job.

## What memory is for, and what it is not

Memory records **where work got to and what was true at the time**: the state a machine was left in,
what an investigation concluded, which approach was already tried and rejected. That is genuinely
useful, and it is also why an entry is a lead rather than a fact. It was written with less evidence
than you are about to have, nothing re-checks it when the code or the tooling moves on, and a stale
entry looks exactly like a current one. Use it to know where to look and what has already been ruled
out; do not build a conclusion on it without confirming the part you are leaning on. When you find
one that is wrong, correct it instead of working around it, since the next session lands on the same
entry. That holds hardest for entries another machine wrote, which arrive with a pull and carry no
sign of how old they are.

The corollary matters as much: **durable know-how does not belong here.** How a tool actually
behaves, the trap in a command, the order steps have to happen in, the flag that is not in `--help`
are all things a future session needs whatever it is working on, and filing them as memory hides
them from every other agent and from the person reading the skills. Those go to the skill that owns
the area. If you are unsure which you have, ask whether the note reads as "where we got to" or as
"how this works". The first is memory; the second is a skill.

## Order of operations

1. Finish the task. Memory work never interrupts or delays the actual job.
2. Let your own builtin mechanism write the memory, in its own location and format. This step is not
    optional and this skill does not modify it.
3. Only then sync, following the routing below.
4. If the sync fails at any point, drop it and move on. No retry loops, and never let it become a
    task failure or delay your reply. One closing line telling the user the store looks broken is
    fine and often useful; turning it into a problem they must solve before getting their answer is
    not. The memory is already on disk, so the sync is what makes it durable elsewhere, never a
    blocker here.

## Routing

A corp machine is a hard stop. When `hostname -f` ends in `corp.google.com`, `c.googlers.com`, or
`roam.internal`, the memories written on it can hold internal information that must never leave it,
so let your builtin mechanism write the memory and stop there. The rest of this skill does not
apply: no sync, no improvised destination, nothing sent off the machine at all. A hostname you
cannot resolve counts as corp too. Skipping a sync costs nothing, while getting this wrong puts
something internal into a permanent record you cannot take back, so the doubt only ever resolves one
way.

Anywhere else, the store is a private GitHub repo. Read `references/external.md` and follow it.
Whether a usable store is actually there is a question that file answers, not this one.

## What never changes

- The judgement about what deserves remembering belongs to your builtin mechanism alone. If it would
    not have written a memory, do not write one here either.
- Do not lower the bar for what counts as a memory just because the result gets pushed somewhere.
- Do not move, rewrite, or delete a memory to make the sync tidier. Git serves the memory, not the
    other way round.
- Do not treat what a pull brought in as settled. It is editable from machines you are not sitting
    at, and it ages without anything marking it as aged.
