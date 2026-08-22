---
name: code-docs
description: >-
  Use when sweeping, auditing, or rewriting function docs, docstrings, or doc comments across a codebase, including bringing them up to Google Style. For GitHub repositories only; not for google3 or other Google-internal trees.
---

# Code Docs

Rewrite function, type, and method docs to Google Style, driven by reading the implementation.

Three rules below are checkable from the outside. Everything else in this file is explanation. Whenever you are unsure whether you are cutting a corner, check yourself against those three.

## Rule 1: never edit a file you have not Read

Every file you edit, you Read in full first, with the Read tool, in this session.

Enumerating candidates with `rg --files`, `rg -l`, or `git ls-files` is fine. Everything after enumeration is reading and judgment.

So: no script does the work. Not generating docs from signatures, not rewriting them in bulk, not deciding which files need attention. A script only shuffles text on the surface. It cannot tell that an existing comment is lying, cannot say what a function is for, and cannot notice that a `thread-safe` claim is false. Docs produced that way read plausibly and mislead the next maintainer, which is worse than no docs at all.

When there is a lot to cover, scale by reading more, not by reading less. That means more readers running at once, not a parser. See Delegating a sweep.

| Rationalization                                                     | Reality                                                                                     |
| ------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| "The codebase is too large to read every file"                      | Then split it across more subagents. Volume argues for more readers, never for a parser.    |
| "The script only finds candidates, I still write the docs myself"   | Deciding which files need work *is* the judgment. You cannot triage what you have not read. |
| "These are all the same pattern, I read a few already"              | That assumption is exactly what produces confidently wrong docs.                            |
| "`sed` is just a mechanical replacement"                            | No doc edit is mechanical. If it were, the code would not need a doc.                       |
| "I will script the boring files and hand-read the interesting ones" | You do not know which ones are interesting until you have read them.                        |

Red flags, stop if you catch yourself here:

- Writing a `.py` or `.sh` that opens source files
- `sed -i`, `perl -pi`, or any other in-place rewrite
- A regex matching `^def `, `^func `, `^pub fn ` or similar, driving an edit
- "Let me write a quick script to ..."
- Editing a file whose contents you have only seen through `rg` output

## Rule 2: every in-scope file is accounted for

List the in-scope files before editing. Every one of them appears in the final report under exactly one of:

- **edited**, with counts: documentable items in the file, docs added, rewritten, left alone
- **read, no change needed**
- **skipped**, with the reason

No file silently disappears. The counts carry the weight: writing "12 functions, 3 added, 2 rewritten, 7 already fine" forces you to look at all 12, instead of fixing the two obvious ones and calling the file done. Subagents report the same shape for their slice.

## Rule 3: types are copied from the signature

In `Args:`, `Attributes:`, and any equivalent section, the type is transcribed from the signature verbatim. Do not simplify `Sequence[bytes | str]` to `list`, do not drop `| None`, do not substitute a friendlier spelling. If you cannot copy it, you have not looked at the signature.

Where the signature carries no annotation, do not invent one. Describe the parameter in prose and leave the type out. If the type is genuinely unclear from the implementation, leave a `TODO(code-docs)`.

Never drop `name (Type):` from a file that already uses that form, including while converting between styles.

## What a doc contains

One line saying what the thing does, then only what the signature cannot show: errors and what each one means, ownership and lifetime, concurrency contract, side effects, preconditions. Nothing else.

- Public API: the above, complete.
- Private helper: one line. A second line only for a load-bearing precondition, such as "caller must hold the lock".
- Test: one line naming the behaviour verified, not how the test works.

If a sentence restates the signature or narrates the implementation, cut it.

## Delegating a sweep

Anything past a handful of files should run in parallel, and the unit of parallelism is a slice of the tree owned end to end. Partition by folder, package, or language into disjoint file sets, then give each worker the whole job for its slice: Read it, judge it, edit it, report the Rule 2 ledger. Dispatch them in one message so they run at once. A few slices is plain subagents; once the count outgrows what one conversation can coordinate, use a Workflow.

Do not plan the edits yourself and hand workers a list of changes to apply. Planning content means reading every file first, so your context fills with the whole repository and the fan-out saves typing instead of reading, when reading is the expensive part. It also puts a worker in the position of editing a file it never Read, which is Rule 1 gone: that rule binds whoever holds the Edit tool, not whoever read the file first.

A worker does not load this skill. Every worker prompt restates the three rules, the "What a doc contains" recipe, its file list, and the reference path for its language. Without the recipe the slices come back in different voices; without the rules the worker reaches for a script exactly as you were about to.

Two workers must never hold the same file, and none of them may run `git stash`, `git checkout`, or anything else touching the shared working tree outside its own files. One stash wipes every sibling's work.

Delegating makes you the reviewer, which is a real job and not a rubber stamp. You did not read these files, so you cannot vouch for the diff out of your own knowledge, and "done, 14 files, all clean" is a claim rather than evidence. Before handing back, pull files out of `git diff` yourself, Read the code behind them, and sample from every worker rather than from whichever answered first. Confirm the doc matches the implementation, that the ledger counts are real, and that the length matches the recipe. Fix or re-dispatch whatever does not hold up.

## Scope

Unless the user names a path, the whole repository is in scope. Do not quietly narrow to a subset.

In scope: functions, methods, types, traits, interfaces, classes, macros, and tests in source files. Existing README and docs files only where their examples or API claims contradict code touched in this pass; fix only drift you can verify from the code, and flag the rest.

Never edit: generated code, vendored dependencies, build output, release history, license, security or conduct boilerplate. Do not create new README or tutorial files.

Keep the diff to docs and comments. No reformatting, no refactors, no signature changes, no added type hints. Anything the code itself needs goes in the report, not in this diff.

Public and private, for the rules above: Python names without a leading `_`; Go exported identifiers; Rust `pub`, treating `pub(crate)` and narrower as private; C and C++ public headers and non-`static` definitions, unless the path says `internal`, `private`, or `detail`.

Write every code-facing text in English, including docstrings, doc comments, inline comments, and TODOs. Existing Markdown docs keep whatever language the project already uses.

## References

Load only the one for the language you are editing: `references/python.md`, `references/go.md`, `references/rust.md`, `references/cpp.md`. They are anchors, not rulebooks; use judgment where they are silent, and for other languages follow that language's established convention and say so in the report.

Read them for the **shape** of a doc. A worked example there shows which sections exist and in what order, never how much to write; length comes from the recipe above.

If a project already uses Doxygen tags or another house style, do not switch it silently. Convert only once the user confirms, and then convert wholesale rather than leaving a mix.

## Verify and hand back

Run the project's own formatter or linter. Rust also gets `cargo doc --no-deps` and `cargo test --doc`, which catch broken doc examples that formatters miss. Fix diagnostics your own edits caused, report pre-existing ones and leave them alone, and never reach for `--no-verify`.

Work the whole sweep through in one pass. Do not stop to have routine calls approved; make the call, leave a `TODO(code-docs): reviewer needed, <reason>` in the language's ordinary comment style where one is warranted, and raise it at the end.

Leave everything uncommitted. Show `git diff`, the Rule 2 report, every `TODO(code-docs)` with file and line, any style conversion applied, and anything suspicious you noticed in the code while reading. The user decides what happens next.
