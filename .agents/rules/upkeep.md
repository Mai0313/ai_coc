---
trigger: glob
globs: "src/ai_coc/ui/upkeep.py, src/ai_coc/ui/clan.py, src/ai_coc/ui/plates.py, src/ai_coc/commands.py, src/ai_coc/parsers/building.py, src/ai_coc/parsers/home.py, src/ai_coc/parsers/clan.py, src/ai_coc/parsers/village.py, src/ai_coc/parsers/glyphs.py, src/ai_coc/models.py, tests/**"
paths:
  - "src/ai_coc/ui/upkeep.py"
  - "src/ai_coc/ui/clan.py"
  - "src/ai_coc/ui/plates.py"
  - "src/ai_coc/commands.py"
  - "src/ai_coc/parsers/building.py"
  - "src/ai_coc/parsers/home.py"
  - "src/ai_coc/parsers/clan.py"
  - "src/ai_coc/parsers/village.py"
  - "src/ai_coc/parsers/glyphs.py"
  - "src/ai_coc/models.py"
  - "tests/**"
---

# Upkeep, the build panel and status

## Upkeep loops (`ui/upkeep.py`, `ui/clan.py`, `ui/plates.py`)

- **`collect`** taps one marker of each resource, since a tap collects every collector of its kind; the markers are found by colour and then held to a size. A marker on the orange plate is over a full storage, which a tap cannot empty, so only the pale plate's are tapped; the dark marker is found by its icon on that plate, written down and learned per bubble from the gold and elixir markers on the same frame. Gains come from the storage bars, and a pass that finds the same markers standing stops (a full storage takes nothing).
- **`donate`** gives troops to whoever is asking. A tap gives immediately, which is what `--dry-run` is for.
- **`tap`, `swipe` and `confirm`** are the hands the agent upgrades through the build panel with: it reads every frame itself and picks the row, so the code names nothing and finds only the buttons it is about to press. They claim the emulator but go through `_attached` and never `_home`, so a panel left open stays open, and each saves the frame it left behind in its run directory for the agent to read. A sheet is told by its red X as well as its green button (`sheet_open`), since `upgrade_sheet` alone answers on any frame whose storages do not read, and each village draws its own: the builder base's 是否提升至N級？ is larger, with one button in the middle (bottom right on a hero's), white or red digits drawn bigger, a square coin and violet elixir, and pale enough to pass for a dialog, so the sheet is tested first. `confirm` presses once (under `--pay`, the wall's price button and then what that raised) and then looks several times, cancelling a dialog that slides in, so it never presses the same thing twice.
- **`boost`** starts the builder base clock tower's free boost (`clock_tower_marker`, `free_boost`, `boost_sheet`). Out of turn the same button asks for gems, so it presses nothing without the marker the game floats over the tower only while the free boost is ready, taps the tower below it, and presses only the button carrying the tower's clock with no price printed on it and no blue or green plate, once. That press raises the boost's own sheet, whose lime 加速 it presses because it raised that sheet itself, as `confirm` does a dialog; nothing on the sheet tells a gem, the gem icon being the same yellow-green. It leaves the tower selected for the agent to clear.
- **`stock`** reports what the village on screen holds as a share of its ceilings. It reports whichever village is up and never crosses.
- **`status`** reads the counters in the bottom right on either village (`plate_count`, `parsers/home.py`) without opening the panel, plus the stock and the shield bar (無 or its countdown), and takes its timers from the village export (`_exported`, the walk `export` uses, run only on a village), worked through the running speed-ups (`parsers/village.py`: `SPEED_UPS`, measured rates). So it covers both villages without sailing and asks no model. The panels draw a countdown at normal speed whatever is running, which is why they are not the source: under a builder potion one read 6小時 for a job done 40 minutes later. The Builder's Apprentice and Lab Assistant speed-up is not counted (the user's call of 2026-10-10). **The shield is not watched**: the user has said more than once that its time does not matter, since loot is cheap and a few battles win back what a raid takes (2026-10-10, 2026-10-13), so nothing sleeps on it or sails for it. Its countdown is read since 2026-10-14, when he asked for it once a shield was up to measure; 天 and 秒 on the bar have not been seen and read as None.

**A price is what the menu says today, not a fixed table.** The gold pass and event tasks discount upgrades on every building, walls and heroes included, so the same level can ask 8 500 000 one week and 8 000 000 the next; a price that differs from an earlier run or the wiki is not a misread. So nothing, in the code or in an agent's reasoning, assumes what a building, a wall or a hero costs (`MIN_PRICE` is only a floor against single figures), and a suspected misread is settled against a crop of the button, never against a remembered price.

Prices in a building row shrink with their length, so `digits_from` takes the row floor as a parameter. A truncated price is the dangerous misread, since seven of eight digits still clears `MIN_PRICE`.
