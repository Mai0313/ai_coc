---
name: game-update
description: >-
  Bring ai_coc back in line after Clash of Clans changes what it draws: a UI or
  HUD overhaul, buttons or bars that moved, a panel that opens somewhere new, or
  a game mechanic that now behaves differently. Use it when the user says
  「遊戲改版了」「介面大改」「更新之後都讀不到」「按鈕換位置了」「HUD 變了」, or when many
  commands fail at once in a way that points at the screen rather than at one
  loop: `not_a_village` on a village that is plainly up, storages or counters
  that stop reading, a command that cannot find a screen it always found.
  Covers finding out what changed by looking and tapping, deciding what to fix
  first, fixing the parsers with committed evidence, proving every command live,
  and shipping it. A single loop misbehaving on an unchanged UI is
  watch-and-fix, and an emulator that will not come up is repair-emulator.
---

# game-update

The parsers read what the game paints at fixed places on a 1600x900 frame, so a game update that moves, restyles or replaces that UI breaks them all at once and quietly: a reader that no longer finds its plate answers None, and None reads as "not a village", "nothing there" or "unread" rather than as "the game changed". The job is to learn the new screen as well as the old one was known, fix the readers against evidence, and prove every command on the live game before calling it done.

## Done looks like

- Every command the user relies on has run on the live game after the fix, on both villages where it applies, and did what it should (see "Prove it live").
- Every reader that changed is pinned by a test on a committed frame of the new UI, with every identity `.agents/rules/code.md`'s Code rules list painted over (names, player tag, Supercell ID, clan names, chat); the profile and settings windows show the tag and ID. Frames of the old UI are kept where a test still needs them, not deleted to make a sweep pass.
- `AGENTS.md`, `.agents/rules/`, the other skills and the docstrings say what the new UI is; nothing that the update made false is left standing.
- The fixes are merged through `AGENTS.md`'s Development flow. Bugs found along the way that are not part of the update are filed as issues and fixed in their own PRs afterwards.
- The user has seen the frames each decision rested on, and has heard what changed from the player's side, what was verified live, and what could not be.

## Look before reasoning

**The screen is the evidence; the log only says that something failed.** Start by taking frames of both villages and of every screen the commands pass through (village, attack menu, scout, battle, result, the panels behind the bottom-right buttons, settings, the builder base's cart), and put each next to a committed frame of the same screen from before. Send the user the ones you judge by. Ask what the user noticed: they play the game and see changes first, but treat their account as a lead to check against frames, not as the inventory.

Before the first tap, follow `AGENTS.md`'s Driving the game: read `state.json` and park the camera (while no village reads, `view --zoom out` can only pinch out, not park). The farm subagent it calls for waits until the village and its storages read again, as `AGENTS.md` says. The user may be playing on the same emulator; a game that changes between two of your taps is a reason to ask.

## Explore by driving the game

**What moved has to be found by hand**, because nothing in the code knows the new layout yet, and the user will not have described every change. Tap the buttons that look like they replaced old ones, open every panel and window, and take a frame after each tap; a feature the old UI kept in one place often turns up behind a new button, and finding it is the work. A scratch driver that taps, swipes, presses back and captures, then runs every parser over the frame it took, makes this fast; keep it in your scratchpad, not in the repo.

Explore safely; the old dangers do not go away with the layout:

- Never tap a button that spends: a gem price, a blue plate, a green 確認 on a purchase. Read a menu before tapping inside it.
- `back` on a clear village raises 確定退出遊戲嗎, whose 確定 sits where a purchase's 確認 does. Answer it with 取消. A panel `back` does not close needs its own close button.
- A tap on the map selects a building, and a drag that starts on a selected building can move it. Deselect before dragging.
- Leave the game on a parked village when you stop.

## Decide what to fix first

**Fix in the order the commands depend on each other.** The test of which village is up (`current_world`) gates almost every command, so while it reads None everything else looks broken too; fix it first. Then the storage bars and their ceilings, which the loops use as their village test and their stop condition; then the counters and panels; then each command's own screens. Re-sweep after each layer, since much of the next layer often starts working by itself.

**Sweep every parser over every frame, old and new**, before changing anything and again after: which readers stop answering on the new frames, and which start answering on frames they should refuse. The same sweep over the run frames under `~/.ai_coc/logs` (new code against the old) shows what a change moved before a battle is spent on it.

## Fix against measurements

The rules in `.agents/rules/screen.md`'s Cross-cutting rules hold for the new UI as they did for the old: read what the game paints on top, pair two features where one alone matched something else, and draw every threshold from measurements on both sides of it, writing the margin and the frames it was measured on beside the constant. In particular:

- **Re-check every spending guard** on the new UI (the blue-plate refusal, the gem icon, anything counted from an anchor). A guard that reads stale does not fail quietly; it can let a tap reach the wrong button.
- **A changed mechanic is a finding too.** Watch what each tap does on the frame after it, not only whether the outcome came out right: a tap that now does more or less than it used to leaves the rest of a burst landing on whatever is underneath.
- **Keep what the user decided.** A feature the user has said they do not want (`AGENTS.md`, `.agents/rules/`, the skills) is not rebuilt because the update broke it; say it broke and leave it. The converse holds too: a feature he kept is fixed, not dropped because another reading could stand in for it (「我們不應該刪除功能」, 2026-10-13).

## Prove it live

Run each command the update touched on the live game, read its `run.log` and the frames it kept (`--debug`, where the command has it; for the others take a frame with `ai_coc capture`), and look at the screen it left behind: a command that answered right but left a dialog or a selection up has broken the next command. Spend-free ones are run freely (`world`, `world --go` both ways, `stock`, `status`, `view`, `launch`, `collect`, `donate --dry-run`, an `attack` round on each village with `--stop-at 0`); anything that spends needs the user's say-so. Exercise the recovery paths too, by putting the game into the state they recover from (a panel left open, a dialog up) and running a command over it. A path that only a rare state reaches and that could not be produced live is named in the report as untested.

A fix is proven when the new path appears in a live run's log and the frame after it shows the game where it should be. A clean run that never reached the new path proves nothing about it.

## Report

From the player's side, per `watch-and-fix`'s report: what works again, what was found broken along the way and fixed, what was verified live, and what was not, each with the PR and a frame that shows it.
