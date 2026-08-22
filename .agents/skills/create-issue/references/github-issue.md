# GitHub

A new issue captures the idea and the requirement only: what was observed, why it matters, what the desired outcome is.

Do not write the design or the implementation plan into it. Leave a `## Plan` section with `TBD` as a placeholder.

The reason is that **the implementation happens in a different session**, never this one. Filing the issue is where this session's involvement ends. A fresh agent picks it up later, in a clean context, with tools and information unavailable today, and that session is the one that fills the placeholder in before starting the work. Anything planned now would not be executed now; it would only carry the current task's assumptions into someone else's context and narrow their options before they have even looked. Keep that freedom for them.

Anything published to GitHub is English only, title and body alike.

## Look for it first

Search before drafting. Any repo with some history has been told most things once already, and a second issue saying the same thing splits the discussion across two threads that nobody reads together.

Hand the search to a subagent once the title list stops settling it and bodies have to be read, since reading them is where the context goes; on a tracker small enough for one `gh issue list` to answer, a worker is pure overhead. Either way the search covers **closed issues as well**. A closed one is the more valuable find: it means the idea was raised and then fixed, rejected, or folded into something else, and none of that shows up in the open list. Search the wording the repo itself would use rather than the wording this conversation happened to use, since the same want gets described in several ways. Ask for candidate numbers with a line each on where the overlap is, and judge them yourself:

- **Same want** — do not file. Hand back the existing issue. If this session learned something that issue does not say, that goes in a comment on it.
- **Related but distinct** — file it, and keep the number. It is either the parent below, or worth a sentence in the body pointing at it.

## Title

A conventional-commit prefix plus one specific line: `feat:`, `fix:`, `chore:`, `docs:`, and so on. Same vocabulary as commit messages, so an issue and the commit that closes it read as the same thing.

## Body

If the repo ships `.github/ISSUE_TEMPLATE/`, it wins — maintainers wrote it because their triage depends on those fields, and a well-written issue in the wrong shape still costs them work. Fit the material into whatever sections it asks for, and keep the `## Plan` placeholder at the end unless the template already carries a section that means the same thing.

A checkout with no template does not mean the owner has none. GitHub treats a repository literally named `.github` as the account's defaults for every repo under the same owner, so the template that a human sees on the web can live somewhere the working tree never sees. Derive the owner from the target repo rather than assuming one, then look there:

```bash
owner=$(gh repo view --json owner -q .owner.login)
gh api "repos/$owner/.github/contents/.github/ISSUE_TEMPLATE"
```

`ISSUE_TEMPLATE/` may sit under `.github/`, the repository root, or `docs/`; try the others before concluding there is nothing.

The trap is that `gh` will not do any of this for you. `gh issue create --template` reads the GraphQL `repository.issueTemplates` field, which only reports templates committed to the target repo itself. An inherited one comes back as an empty list, so the tooling will quietly hand you the free-form path while the owner's template exists and applies. Fetch the file yourself and file with `--body-file`.

Then mind which kind you fetched. A `.md` template is a body already: drop its YAML frontmatter and fill in the rest. A `.yml` template is an issue form, so there is nothing to paste. Read its `body` entries in order and write one `### <label>` section per field, which is exactly what GitHub produces when a human submits that form. When a directory offers several, pick the one matching the issue's kind, the same judgement that picked the conventional-commit prefix for the title.

With no template anywhere, prose followed by the placeholder:

```markdown
<what was observed, why it matters, what the desired outcome is>

## Plan

TBD
```

## Signing it

Everything reaches GitHub through the user's own account, so nothing in the metadata records which model wrote the issue. That goes in the body instead, as its own block at the very foot, carrying your full model name:

```markdown
---

Filed-by: Claude Opus 5 (1M context)
```

Last thing in the body, after whatever the template produced and after the `## Plan` placeholder. It is a block rather than a line because later sessions add their own to it, and `gh-dev-flow` owns what they add.

## Labels

Only labels that already exist (`gh label list`) — filing an issue should not create labels as a side effect.

A repo carries two vocabularies at once: the set GitHub ships with every new repo (`bug`, `enhancement`, `documentation`) and whatever the project added later, which overlap in meaning without being listed as synonyms. Choosing between `bug` and `bugfix` from their descriptions is a coin flip. Read which one the repo's own issues already carry and follow the majority, because a label earns its place by grouping this issue with the ones a human would expect to find beside it, not by being the more accurate word.

## Parent

`--parent` records a relationship that is already there. Reach for it when the issue being filed is plainly one piece of something already tracked, which is what the search above surfaces.

Opening a parent with sub-issues under it is available too, and the call is yours. What it turns on is whether the pieces were separate before you arrived. Cutting a want into implementation steps is design: it fixes the shape of the work before anyone has looked at it, and it belongs to whoever picks the issue up, where `gh-dev-flow` owns it. Cutting it along ground that was never one piece is description instead, so a sweep that has to cover `src/`, `tests/` and `scripts/` is one want landing on three trees, and each piece already knows what done means for it. The tell is whether you had to decide anything to draw the line, and whether the pieces could land in any order or at once. Where it fits, what it buys is a parent holding what they share, the constraints and the reason that would otherwise be copied into every child, so the children stay thin. Where it does not, nothing is lost by leaving the issue whole.

Filing several issues in one go is not a reason to invent an umbrella to hang them under. Follow-ups that fell out of the same PR are usually parallel and independent, and a parent that exists only to tidy the list claims a structure that is not there.

The hard limits rarely bite: 100 sub-issues per parent, eight levels of nesting, and a sub-issue may live in another repo.

## Filing

The user was already asked whether to open an issue and said yes; that was the decision point, so do not stage a second approval round on the drafted text. `gh issue create --body-file <path>` keeps the markdown intact, and the label and parent go on the same command rather than in an edit afterwards. Hand back the URL.
