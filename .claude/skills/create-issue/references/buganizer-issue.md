# Buganizer

Google-internal. Filed with the `buganizer-cli` skill, body passed through `--description_file` so the markdown survives intact.

An issue is a **handle on a piece of work**, not a plan for it. Whoever picks it up later — often another agent, in a fresh context, with tools and information you don't have today — needs what they cannot re-derive: why this exists, what counts as in scope, where to look. The how is theirs to decide; steps written down today just freeze today's assumptions and quietly foreclose a better approach. The line to hold: "run the TFTF regression on the affected platforms before landing" sets a bar, while "run `run_tftf.sh --platform ...`" is an instruction and belongs to whoever executes.

Shape the body however the work calls for. Every line should be something the reader could not have worked out alone.

**English only**, title and body alike, regardless of what language the conversation is in.

## Component

Land on the **leaf**, not an ancestor. Component paths nest (`[Android, Pixel, Security, Secure ARM FW, RF-A]`), and stopping one level up looks defensible while missing the team that actually triages the work.

When the subject matter and the owning team disagree — a TF-A merge carried out by the RF-A team — the team wins, because that is whose queue and workspace the issue needs to show up in. The title prefix carries the subject instead.

Everything else (priority, type, assignee, parent) follows the sibling issues.

## Iteration hotlist

Teams that plan in Taskflow track each two-week iteration through a hotlist of its own, so an issue filed without one never appears on the board the team actually works from. Attach the current iteration's hotlist whenever the owning component belongs to such a workspace.

The component chosen above is what resolves this, so there is nothing to infer from the subject matter. Taskflow workspaces are component-based: list the workspaces (the `taskflow` skill owns the CLI) and take the one whose components contain the one you picked. Match on the component, never on the workspace name, which often reads nothing like the project it tracks.

Then read that workspace's iterations and take the hotlist off the current one. Three things make that harder than it looks, and each yields a plausible wrong number with no sign anything went wrong:

- **`Status: ACTIVE` carries no information.** Iterations that closed months ago keep it, and ones that have not started yet carry it too. A workspace routinely has a dozen of them at once.
- **`(Current)` is the reliable marker, but the year and the quarter carry it as well.** Require `Type: ITERATION` alongside it, or you file into a quarterly or annual hotlist that nobody triages from.
- **Check that the planned date range covers today.** This is the independent confirmation of the other two, and the only signal that does not depend on how the tool chose to label a row.

Each workspace runs its own cadence, and two related ones are usually offset by a day or two, so resolve every workspace separately instead of reusing one date window. The hotlist rotates every two weeks, which makes it a filing-time lookup every single time; a number carried over from an earlier session is a guess wearing the shape of an answer.

Put the iteration's name next to the id in the draft. That is what lets the user confirm the right fortnight at a glance, which a bare number does not. If the workspace has no current iteration, say so and file without one rather than reaching for the nearest.

## Filing

Show the full draft and wait for approval. Buganizer has no confirmation step of its own: the issue is live the moment the command returns, and a wrong component pages the wrong team.

Then stop. The description records why the work was filed; it is not a live status page, so later agents leave it alone even while doing the work it describes. Rewriting it as things progress destroys the original framing, which is the part worth reading. Editing an existing issue is a separate, explicit request.
