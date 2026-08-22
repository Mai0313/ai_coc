# Porting notes

Maintainer documentation. Not loaded at runtime, and not part of the skill's instructions: `SKILL.md` and `references/rubric.md` are the whole skill.

## Where this came from

A hand port of the built-in `/review` command in [openai/codex](https://github.com/openai/codex), not a vendored copy. There is no entry in `.skill-lock.json` and nothing syncs it automatically. Baseline: upstream `4c43465133` (2026-07-25).

Seven upstream files matter, with the commit that last touched each at port time:

| Upstream path                                              | Pin          | What it contributed                                                                                                                                                       |
| ---------------------------------------------------------- | ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `codex-rs/prompts/templates/review/rubric.md`              | `81de4f251c` | The reviewer's entire system prompt. `references/rubric.md` is the port of this file and nothing else.                                                                    |
| `codex-rs/prompts/src/review_request.rs`                   | `ba2b67f9cd` | The four targets and the exact synthesized prompt for each. Copied close to verbatim into the block in `SKILL.md`.                                                        |
| `codex-rs/core/src/tasks/review.rs`                        | `687f05cb94` | Proof that the rubric replaces `base_instructions` wholesale, and that the run is one-shot.                                                                               |
| `codex-rs/core/src/session/review.rs`                      | `4657ab06c6` | Sub-session config: what the reviewer inherits and what gets disabled.                                                                                                    |
| `codex-rs/git-utils/src/branch.rs`                         | `0f957a93cd` | `merge_base_with_head`, including the upstream-must-be-ahead condition.                                                                                                   |
| `codex-rs/protocol/src/review_format.rs`                   | `23aac925e7` | How findings render for a human.                                                                                                                                          |
| `codex-rs/skills/src/assets/samples/review-agent/SKILL.md` | `83a4187837` | Codex's own skill-form port of the same rubric. The closest precedent for every adaptation below, and the tie-breaker whenever the Rust path and the skill path disagree. |

## Naming

The skill has been called `codex-review` and `local-review` during development; it landed as `code-review`. Two things about the name are worth preserving.

**Codex is a style label, not a dependency.** The skill runs entirely inside the calling agent against the local checkout, and never invokes the Codex CLI, the `codex` binary, or any remote service. Keep the frontmatter description explicit about that. An earlier draft opened with "Codex's `/review` ported to run locally" and read, to a model scanning descriptions, as though the skill belonged to Codex or required it installed, which is a reason to skip it. The separately installed `codex` plugin does ship `/codex:review` and `/codex:adversarial-review`, and those genuinely do shell out to the Codex CLI. This one is unrelated to them.

**The name collides with a Claude Code builtin.** `/code-review` is a built-in slash command for reviewing the working diff, and the built-in `/review` description points at it by name. Which one a bare `/code-review` resolves to is up to the host, so if triggering ever behaves unexpectedly, this collision is the first thing to check. The frontmatter description is what disambiguates the two for a model; keep it distinctive.

## What changed in the port, and why

Each of these is deliberate. Do not "fix" one back toward upstream without reading the reason.

**The reviewer is a subagent, not a sub-session.** Codex forks a fresh thread with `initial_history: None` and swaps in the rubric as `base_instructions` (`tasks/review.rs:117`). Claude Code has no equivalent, so a subagent's independent context stands in for it. This is the load-bearing property of the whole design: the reviewer must not see the conversation that produced the code. Everything else is negotiable; this is not.

**Markdown output, not JSON.** Upstream's rubric ends in a strict JSON schema because a Rust layer parses it (`parse_review_output_event`) and renders it (`review_format.rs`). Nothing here parses anything, so the port emits the human-readable shape that `render_review_output_text` would have produced. Codex made the same call in its own `review-agent` sample. `confidence_score` was dropped along with the schema, since codex never shows it to the user either.

**Findings first, verdict last.** `render_review_output_text` prints the explanation before the findings, but that ordering is an artifact of the protocol struct. Both `rubric.md` ("at the end of your findings") and the `review-agent` sample say findings first. The port follows the prose.

**A summary line and two headings around that ordering.** Upstream never needed an output shape, because a Rust renderer built one from the parsed JSON. Here the reviewer's prose *is* the artifact, and it lands in a terminal, so the port pins a tally line, `## Findings`, and `## Verdict`. The tally is the only real addition; the headings mostly buy something the port already wanted, since a required `## Verdict` makes a review that died halfway visibly different from a clean one at a glance. The example in `references/rubric.md` also keeps finding bodies flush left on purpose: upstream's indented sample renders as a code block once a real renderer sees it.

**`CLAUDE.md` alongside `AGENTS.md`.** Upstream's rule-attribution section names `AGENTS.override.md` and `AGENTS.md`. Same precedence logic, renamed for where this skill actually runs.

**Stricter about writing than upstream.** Codex's reviewer inherits the parent's shell policy and skills, so it technically can commit or run `gh`; the rubric only says "Do not generate a PR fix." The `Hard limits` section in `SKILL.md` closes that gap on purpose, because the whole point of this skill is that it looks and reports and does nothing else. Upstream's line about findings becoming inline PR comments was dropped for the same reason.

**Self-restraint moved into the rubric, and made unoverridable.** Upstream's Rust path never needed to tell the reviewer not to delegate, because `session/review.rs` pins `MultiAgentVersion::Disabled` and the delegation tools simply are not there. A skill has no such mechanism, so the restraint has to be text, and it has to sit in `references/rubric.md` where the reviewer will actually see it. It is also explicitly carved out of the "later guidance overrides these instructions" clause, because a repository `CLAUDE.md` that encourages farming bulk reading out to subagents would otherwise override exactly the rule that keeps the review a single judgement. `review-agent/SKILL.md:8-10` carries the same prohibition for the same reason.

**An incomplete run is never a clean run.** Upstream keeps three distinct terminal states: a finished review, an interrupted one (`render_review_exit_interrupted`, "Review was interrupted. Please re-run `/review`"), and a finished-but-empty response (`REVIEW_FALLBACK_MESSAGE`, "Reviewer failed to output a response."). An early draft of `SKILL.md` collapsed all of them into "if the reviewer returned nothing, `No findings.`", which licenses the most expensive failure a review tool has: a false clean verdict over a diff nobody finished reading. The `Report back` section now separates completed-and-empty from did-not-complete, and names the missing verdict as the tell.

**One reviewer, never a panel.** Codex's `/review` is a single reviewer, and its acceptance bar ("prefer outputting no findings" over a marginal one) is calibrated for one judgement. Fanning out across lenses produces a noisier artifact that no longer resembles what this skill is for. The `.codex/skills/code-review*` orchestrator in the codex repo does fan out, but that is a different feature reviewing codex's own PRs, with repo-specific lenses (app-server APIs, context fragments, an 800-line budget) that do not travel.

## Local policy, not from upstream

One rule in `references/rubric.md` has no counterpart in codex and is not trying to acquire one, so a re-sync should leave it alone rather than reconcile it.

**Replacing instead of migrating.** The owner of this checkout would rather tear a thing out and rebuild it than carry a compatibility shim, on the grounds that the shims are what turn a codebase to sludge. Left alone, a reviewer treats every breaking change as a missing migration and files the same finding forever. The rule says the absence of a migration is not a defect, while a caller or stored file that still expects the old shape is one, held to the ordinary evidence bar. That direction is a narrowing, which is why it sits under its own heading instead of inside the fidelity list: it is a deliberate policy call, not a claim about what codex does. Upstream's own criteria already lean the same way (rigor absent from the rest of the codebase, no speculating that a change "may disrupt something else"), so nothing here contradicts the rubric it came from.

## Re-syncing

The governing rule is asymmetric: **the port may be stricter than upstream, it must never be looser.** Exact fidelity is not the goal. Where upstream is weak, beating it is the point. So a re-sync is not a diff-and-apply, it is a one-way check that upstream has not gained a constraint we lack.

That also means the two files cannot be mechanically compared. `references/rubric.md` is a rewrite, not a copy: against upstream's 95 lines it shares exactly two lines of real content, having been recast into imperatives and restructured under headings. Every rule survives, but `diff` will tell you nothing. Comparison is rule by rule, by hand.

Nothing here changes on its own, so a re-sync is a deliberate act. When it is worth doing:

1. `cd ~/repo/codex && git fetch && git log <pin>..origin/main -- <path>` for each row in the table. Empty output for every row means there is nothing to do.
2. For a change to upstream's `rubric.md`, ask only one question: does it impose a constraint this port lacks? If yes, port it. If it merely reworks the output schema, relaxes something, or adds an escape hatch, ignore it.
3. Changes to `review_request.rs` matter only if a target's prompt wording moved or a fifth target appeared.
4. Changes to `branch.rs` matter only if the comparison-ref logic moved. That logic is spelled out in prose in `SKILL.md`, so it can drift silently. It already did once: the upstream-must-be-strictly-ahead condition (`right > 0`, `branch.rs:112`) was missed in the first draft and caught in review.
5. Update the pins in the table above afterwards, otherwise the next re-sync has no baseline.

Upstream also runs a detached review path that never touches the rubric (`app-server/.../turn_processor.rs`, `start_detached_review`). It is not what `/review` does in the TUI and is out of scope here.

## Things worth knowing before editing

- `references/rubric.md` is written for the reviewer subagent, in the second person. `SKILL.md` is written for the orchestrator. Instructions belong in exactly one of them; a rule in the wrong file reaches the wrong agent.
- Reviewer-layer behavior that is absent from the rubric (read the full diff, check tests and call sites, no web search) reaches the reviewer only through the spawn instruction in `SKILL.md`. "Give it two things and nothing else" scopes the parent-conversation context, not those constraints.
- `mdformat` runs in pre-commit and will reflow Markdown tables. That is why the target prompts live in a fenced block rather than a table: the base-branch prompt contains backticks that a table cell renders wrong.
