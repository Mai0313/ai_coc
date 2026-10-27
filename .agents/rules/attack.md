---
trigger: glob
globs: "src/ai_coc/ui/attack.py, src/ai_coc/ui/world.py, src/ai_coc/parsers/scout.py, src/ai_coc/parsers/glyphs.py, src/ai_coc/parsers/boundary.py, src/ai_coc/parsers/field.py, src/ai_coc/plans/**, src/ai_coc/prompts/**, src/ai_coc/adapters/ai.py, src/ai_coc/adapters/config.py, src/ai_coc/commands.py, src/ai_coc/models.py, src/ai_coc/ui/runner.py, src/ai_coc/parsers/home.py, src/ai_coc/cli.py, tests/**"
paths:
  - "src/ai_coc/ui/attack.py"
  - "src/ai_coc/ui/world.py"
  - "src/ai_coc/parsers/scout.py"
  - "src/ai_coc/parsers/glyphs.py"
  - "src/ai_coc/parsers/boundary.py"
  - "src/ai_coc/parsers/field.py"
  - "src/ai_coc/plans/**"
  - "src/ai_coc/prompts/**"
  - "src/ai_coc/adapters/ai.py"
  - "src/ai_coc/adapters/config.py"
  - "src/ai_coc/commands.py"
  - "src/ai_coc/models.py"
  - "src/ai_coc/ui/runner.py"
  - "src/ai_coc/parsers/home.py"
  - "src/ai_coc/cli.py"
  - "tests/**"
---

# The attack loop

## Attack loop (`ui/attack.py`, `parsers/scout.py`, `parsers/glyphs.py`)

攻擊 → 尋找對戰目標 → 攻擊 → scout → deploy or skip → 回營, driven by `AttackRunner`. **Nothing in the loop asks Gemini what is on screen.** The scout screen expires after 30 seconds and then forces the battle, so `read_scout` reads the loot against digit templates in about 0.15 s. It answers None (正在搜尋對手, or a panel it cannot read), a view with `can_skip=True`, or one with `can_skip=False` (read off the 下一個 button's orange).

**Reading the loot panel.**

- **`read_scout`'s None is overloaded**, and searching and unreadable are opposite instructions. The end of a battle is `_battle_ended`, which reads the green 回營 button, never a None.
- A new opponent fades in, and a frame caught mid-fade reads half-drawn digits and an absent 下一個 — which means "forced" and commits the army. `_scout` refuses a frame whose panel peak (`panel_peak`) is under `PANEL_DRAWN_BRIGHTNESS`. The check lives in `_scout` rather than `read_scout`, because the battle poll must still read popup-dimmed panels.
- The storage bars (`read_stock`) are the home village test of every `GameRunner` loop (`_home`), so a failure there takes `collect` and `donate` down; the attack loop tests the village with `current_world`.

**Around the battle.**

- `army_strength` reads the camp off 我的軍隊 before the search fee is charged. Training is instant, so an army under `MIN_ARMY_RATIO` (0.5) cannot fill and ends the whole series (`army_short`) instead of resting.
- `--until-idle` reads the builder and laboratory counters in the bottom right (`plate_count`), or only the one it names, off the same village frame `_stood_down` reads the storages from, ahead of them, and ends the series `builder_free` or `lab_free` once either reads an idle count above 0. Only the idle count decides, never the total, because events add a builder or a research slot (drawn with a goblin face) and take it away again. Any idle slot counts, including one idle from the first round, because `farm` restarts the command around every loan and a remembered starting count would swallow whatever freed up in the gap; a counter that will not read plays the round. It is read once, so `farm` confirms it with `status` (the export's running jobs against the counter) before telling anyone. Without the flag nothing reads them.
- The tactic goes in as soon as the plan is back, inside the scout countdown: a drop there starts the battle, and the boundary is already drawn. Only `probe` and `bounds` still wait the countdown out (`_wait_for_battle`).
- Every battle opens with a pinch out (`_settle_zoom`), because the game reports no zoom level. The boundary's height before the pinch warns on a clipped, zoomed-in home village. The builder base is smaller at every zoom, so the warning is the home village's alone, and a zoomed-in builder base goes undetected: no recorded frame has one to draw a floor from.
- **The result screen** (`RETURN_HOME_GREEN`, `_leave_result`). `_leave_result` re-reads before pressing `back`, and `_open_attack_menu` hands any frame that is no village to `uncovered`, which clears a popup left over the result screen and still never presses at a battle. The report the game opens on after a raid (首領，歡迎回來！, `welcome_back`) puts its one 確定 where 回營 sits, so `battle_over` refuses it, and `uncovered` and `_settle_game` both press `back` at it. The 魔法護盾 sheet the old shield plate opened does the same with its 繼續, and `shield_sheet` keeps it out of `battle_over` the same way. So does the 勝利之星 reward sheet (`star_bonus`), raised a few times a day when the star bonus is won, which `_settle_game` presses `back` at as well, and so does the notice an event's end raises (`event_ended`), whose `back` has not been tried: an event has to end. A reward chest the game raises over the whole screen after some battles (`reward_chest`) ignores `back`, so `uncovered`, `_settle_game` and `_home` tap it open and press 繼續 on each reward it shows (`open_chest`); that green button is pressed only because `open_chest` opened the chest itself.

**The plan** (`_plan`, `AttackPlan`, `AttackStep`, `prompts/attack_plan.md`).

- One Gemini call per battle, after the thresholds have passed, so a skipped opponent costs nothing. `--plan` plays a written plan with no call, `--plan-out` writes whichever ran, and `plans.flat()` (`plans/flat.json`) is what a run with no key or a failed call gets.
- A plan is an ordered list of steps, played in order, and it carries every clock: where each hero goes (`hero`), when each ability fires (`ability`), where each spell goes and when. `act`, `who`, `at` and `seconds` have no defaults, because Gemini leaves out fields that have one. Nothing in `config.json` holds a clock.
- A `wait` counts from the previous move finishing, so nothing depends on how long the call or the boundary read took.
- `hero` steps are matched to the cards left to right (an upgrading hero's card disappears) and are skipped when the cards run out. The cards follow the order the player set the army in, which has differed between armies, so a written `--plan` cannot know which hero a card is: `flat.json` names only its first hero, and sends the rest as `unknown`; one naming `unknown` sends every hero still in hand. Which one-off card is the siege machine comes from the plan having a `siege` step, not from a health bar, since the game draws one over the siege machine too.
- The prompt asks for the side with the **heaviest** defences (troops take defences head on) and for rage on the heaviest defence group, where the army ends up. Moving rage onto measured motion (`_onto_army`, removed) landed bottles planned for 12, 15, 12 and 12 seconds at 18, 22, 20 and 19.
- The plan decides the order of the drops. The order the loop used to fix was siege machine, troops, then heroes straight behind; before that was tuned the siege landed 7 seconds in, the last troop at 44 and the heroes at 67, against 4, 8 and 12 after.
- One point per bottle.
- **Troops and spells can be split over several steps of one act** (the user's call of 2026-10-08). Before the last step of its act, a troop step pours an even share of each card along the plan's one line and a spell step casts one bottle per point; the last step empties the cards (`_spent` carries what was taken). Only neighbouring spell steps are merged, so a split needs a `wait` between its halves. `_settle_drops` judges the troop row only once its last step has played, since a card holding a later share is holding on purpose.
- `planned_line` rejects a line whose midpoint is within `MIN_LINE_RADIUS` of the centre, since `push_out` cannot move it; the plan is kept and its own `deploy_from` preset flank is played instead (`deploy_candidates(plan)[0]`); no other flank is tried.

**Deployment.**

- The whole tactic goes down blind in bursts (`tap_many`), then `_settle_drops` reads a frame inside a pause of at least `CHECK_BUDGET` that follows a one-off drop (or once after the last step, when none did) and resends what the game refused (`_drop_singles`, `_spread_troops`). A refused drop costs nothing, because the card is not consumed. Later pauses check the battle is still on, since some card slots sit where the result screen draws 回營.
- **A drop is judged by the card, never by the screen.** 你無法在紅線區域內派遣部隊 and the other red banners share one colour and are not given reliably. `card_drained` reads the `xN` corner repainting, which happens on the first troop or bottle to leave, so whether a card is *empty* is `live_cards`; a hero landing is `field_units`, reading the green health bar, told from grass by how little blue it has (`HERO_BAR_MAX_BLUE`).
- The card row: `card_groups` is valid only on a full row, so `_deploy` reads it once, off the frame captured after the pinch and before anything is dropped. Known and not fixed: `BattleRow` keeps the x each card had when the row was read, so a card the game adds at the front mid-battle shifts every card after it, and `ability` and `_cast` then tap one slot off; `_dump_leftovers` is the one step written to survive it.
- Timing constants are measured, not cautious. ADB was never the cost (a shell round trip is 47 ms, five chained taps 109 ms, a `screencap -p` 0.6-0.8 s).

**The map and the camera in battle.**

- `DEPLOY_BOUND` is the ground the game accepts a drop on, measured with `ai_coc bounds` rather than detected. `push_out` clamps to it, and every drop point goes through `clear_of_controls`, which keeps it inside `PLAYFIELD`, off the card row and off 放棄. `single_spots` drops a pushed point the game clamps straight back and tries the next spot.
- The game draws its own red stroke around the refused ground. `boundary_reach` reads it along one ray, and `fitted_line` bends the plan's line (or its `deploy_from` preset, when `planned_line` refused the line) onto it through three anchors. It is an opening guess, and `_spread_troops` pushing the flank out when a pass drains nothing is the backstop. `ai_coc probe` measures how good the guess is: its first run agreed on 7 of 12 rays, and four of the five misses put the line inside the real boundary.
- `_settle_camera` centres the village before `_plan`. Known and not fixed: the frame it measures is still the scout screen, whose orange 下一個 button `village_box` masks only below y 600 (`UI_PANELS`), so the box read x 1500 against 1269 for the same village moments later and a run can spend two drags shoving a centred village 72 px. Moving the call onto a battle frame puts it after `_plan`, which would need a second coordinate frame. Also known and open (#65): `village_box` barely follows the camera at all, because its 2% trim lands on village-art speckle near the playfield edges, so `_settle_camera` cannot see a camera that really is off-centre and `_clear_flank` drags 86-102 px on every battle, whatever size the village is. Deployment works with that drag today, so `_clear_flank` and `FLANK_ROOM` stay as they are until #65 is settled. `_clear_flank` drags the village away from whichever edge the flank faces (`FLANK_ROOM`): up, off the card row, for a lower flank, and down for an upper one. `_pan` / `_panned` record every camera move that leaves the village off centre (not `_settle_camera`'s own, which ends centred), measured by `view_shift` along the drag, so the flanks and the plan keep pointing at the village. The loop never selects a card before a drag, since with one selected a swipe deploys along its path; that is also how a machine the game preselects at the start of a builder base second stage gets sent.

## Builder base (`AttackRunner.world`, `_run_night`, `_night_plan`)

The same runner plays both villages. An attack series takes its village from the first round that reads one, and ends `other_village` on two readings of the other village; it never sails.

- **Getting in**: 攻擊 → 開始進攻 → 立即尋找. No scout screen, no thresholds, no fee and no army check, since the army refills the moment a battle ends. The matchmaker waits for a live player (one search took five and a half minutes); `searching_opponent` reads its 取消, and a search past `SEARCH_PATIENCE` is cancelled and started again.
- **The card row** is the machine first and the troops after it; the troops are the cards with a count.
- **`live_cards` means the unit died there**, not that the card is empty. So `_spread_night` decides by whether anything has drained yet: a barren pass before any drain pushes the flank out, and one after stops the passes. It does answer "is the machine still alive" correctly, so a machine that died in stage one is not sent in stage two.
- The machine lands first, with `NightPlan.troops_after` as its head start. Its ability recharges all battle, so `_wait_out_night` taps every one-off card each `ABILITY_TAP` and captures every `ABILITY_POLL`. A machine that survived opens stage two preselected: `_send_selected` taps the field and checks `selected_cards`, since a damaged health bar does not read as one (`CARD_SELECTED_SPAN` lets `card_groups` see the wider selected card).
- A stage can end with no result screen; the card row being repainted marks it, not counting a card whose selection changed, since the ability taps move the selection and that repaints a corner too. A second stage is played when offered, never predicted. Known and not fixed: `_night_plan` runs on every stage and each overwrites the plan the round records, so `plans.jsonl` keeps only the last stage's; under `--plan night_flat.json` both stages play the same plan anyway.
- **The storage limits** are checked before the search, which can take minutes. See **Storage limits** below.
- Known and open: `fitted_line` almost never fits here. Over the 2026-10-16/17 runs (153 rounds) it logged `No boundary on the ray … keeping the preset flank` 249 times and fitted twice, so night battles deploy on the plan's own unbent line (`night_flat.json`'s is the `top_left` preset) and lean on `_spread_night` pushing it out.

**The loot cart** (`collect_cart`, `loot_cart_open`, `loot_cart_ready`, `loot_cart_load`, `CartReport`). Gold lands in the storages; battle elixir goes into a 聖水車 beside the boat, and tapping it opens a sheet.

- The sheet is closed on every path.
- The cart is the builder base's third storage: defence rewards keep paying into it after the storages are full, so full storages end a run only when the cart is also past `stop_at` of its own ceiling (`_visit_cart`, `_cart_has_room`). A look that cannot say how full the cart is goes again at once, and after three the storages decide alone. One schedule, the first round and every `CART_EVERY` battles, empties the cart while the storages have room and reads it once they do not.
- Judged on `read_builder_stock`, never `read_stock`.

## Storage limits

`AppConfig.stop_at` is one share for both villages, measured against ceilings read off the game (`storage_capacity`), so nothing typed in goes stale. Tapping a bar opens a 最大儲存量 tooltip; `AttackRunner._settle_ceilings` and `ScreenRunner.read_ceilings` read it once per run. The tooltip is a toggle, so a row that reads nothing is left alone — unless the read failed and `current_world` no longer names the village, since a tooltip left open hangs over the + buttons, and then the last row is tapped shut. The builder base's third row is the gems bar, whose `+` opens the shop, and is never tapped. **Every watched storage has to be full**, not one of them (`StorageCapacity.full`). A resource with no ceiling is left out and neither stops a run nor holds one open, which is what the builder base's missing dark elixir relies on. A read where any tapped row fails is thrown away whole. **A storage above its ceiling is real, not a misread**: resources that come with a purchase land past the ceiling (gold read 153% on 2026-10-12; the user: 「目前金幣超出上限是正常的 因為購買點數送的金幣 聖水 黑水 是會超過上限的 不用太在意」), and `full` counts it as full.
