---
name: find-on-map
description: >-
  Find something on a Clash of Clans village map by looking at a frame yourself,
  and tap it through ai_coc, when no command, panel row or HUD button reaches
  it: a building's own menu (the lab's 研究, the hero hall's 英雄殿堂, a
  building's 資訊), the window behind it such as the builder base lab's 升級部隊
  while a research runs, a decoration, an obstacle, an event object, or anything
  else the game draws on the ground, on either village. Use it whenever a task
  needs a tap at a place on the map that no parser knows, including a live test
  that needs a screen only a building opens.
---

# Finding something on the map

The result is a tap on the right thing, confirmed by what the game shows back, with the village left as it was found. Nothing in ai_coc recognises a building: the parsers read only the UI the game paints on top (`.agents/rules/screen.md`), so finding one is your own reading of a frame, done fresh each time.

## A fixed route first

**Use a route that does not depend on where things stand, whenever one exists.** The build panel (`upgrades`' 「面板」), the HUD buttons and the commands sit in the same place on every layout; a building moves whenever the user rearranges the village, buys a scenery or the camera parks differently. So the map is for what nothing else reaches, for example a running research: `tap` refuses its panel row, which opens 立即完成 for gems or a menu whose 取消 throws the job away, so the 升級部隊 window is reached from the lab itself.

**Never write a position you found into code, a skill or a memory**, and never reuse one from an earlier run. It holds only for the layout, scenery and camera of the frame you read it from.

## Finding it

1. `ai_coc view --zoom out` parks the camera, then `ai_coc capture` gives the frame to read. Read only the newest frame: any tap or swipe moves what the next one shows.
2. Pick the candidate by what the game paints over it rather than by its art, which changes with level and theme. A running job carries a countdown bar, orange with the troop's icon beside it for a research over the lab and green for a building's own upgrade, so read the bar of the job you are after. The strongest check is that countdown against the `timer` `status` lists for the job you are after, the normal-speed countdown the game draws (not `done_at`, which works speed-ups in). Markers and labels the game floats over the ground count the same way. The art alone is a hint.
3. `ai_coc tap x y` on it. The frame it returns shows the selected thing's name and level above its menu, such as 星空實驗室（10級）. **That label is the confirmation.** A different name means a different building: tap empty ground to let it go and try the next candidate. Press nothing on a menu until its label says it is the one you wanted.
4. Read the menu off that frame before pressing anything on it. 資訊 and the building's own window (研究, 英雄殿堂) open things; 立即完成 spends gems, 取消 throws a running upgrade away, and a blue plate (強化英雄 with its count, an 加速) uses up a magic item, which cannot be won back. `tap` refuses the blue plates it knows of and, while a 立即完成 stands anywhere on the menu row, the whole row, 資訊 included: a refusal there is that guard, not a missed aim, so do not retry it at another point.

**Empty ground** is plain floor with nothing drawn on it: no building, wall, decoration, obstacle, marker or button. On a parked village, the open ground outside the walls is usually safest; an obstacle tapped by mistake opens its own menu, whose removal button spends resources.

## Leaving it as you found it

Close any window by its X, tap empty ground until the menu row is gone, then `ai_coc view --zoom out` again: it parks only once nothing is selected and answers `selected` while something still is. Take one last `capture` to show the village clear. A command run under someone else's loop gives the emulator back as `.agents/skills/farm/references/running.md` says.
