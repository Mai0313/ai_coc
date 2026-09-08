"""Read a scouted opponent's screen: the loot on offer and whether it can be skipped.

The scout screen only stands for 30 seconds before the game forces the battle to
start and takes the 下一個 button away, and every skipped opponent would cost
another call. Position, font and size are fixed by the 1600x900 layout, so the
numbers go through `parsers.glyphs` rather than through Gemini.

What is left here after that engine moved out is this file's real subject: which
of the game's screens is on show, and what the ones a battle passes through are
holding — the loot panel, the army bar, the card row, the storage bars.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
import logging

from PIL import Image

from ai_coc.models import LootOffer, ScoutView, VillageStock
from ai_coc.parsers.frame import open_frame

# The digit reader every number on these screens goes through. It used to live
# in this module, which is why `parsers.home`, `parsers.building` and
# `parsers.hero` each reached in here by private name for it; it has its own
# module now and this file is one of its four callers rather than its owner.
from ai_coc.parsers.glyphs import (
    INK_BRIGHTNESS,
    nearest,
    ink_mask,
    signature,
    row_glyphs,
    glyph_columns,
    split_numbers,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

# The loot panel sits under the opponent's name, one row per resource. It is
# transparent, so the village behind it shows through to the right of the digits
# and its brighter speckles segment as glyphs of their own. The box cannot simply
# be pulled in tight against the text: loot runs to seven figures, and the widest
# reading measured ran to x 207, so this leaves room for that and no more.
PANEL_LEFT, PANEL_RIGHT = 74, 215
ROW_BOUNDS = ((126, 156), (173, 203), (220, 250))
# What is left of the village inside the box is dropped by how badly it matches:
# measured over 21 rows of live frames, all 115 real digits land within 28 bits
# of their template while the blobs read 42 and 57, so the line sits between.
LOOT_DIGIT_TOLERANCE = 35
# The dark row is dimmer than the other two and on the scout screen it peaks at
# 206, where the shared INK_BRIGHTNESS of 200 left almost none of it standing:
# the glyphs came out too short to measure, the row read as nothing, and the
# whole opponent was judged on dark=0.
LOOT_INK_BRIGHTNESS = 190
# **The game dims the whole screen behind a popup**, and the loot panel goes with
# it. Measured on a battle the event's 選擇一項獎勵 cards appeared over: the
# digits peaked at 128 where they read 255 moments later, so a fixed floor of 190
# left zero ink pixels and the panel read as no opponent at all — for the whole
# battle, because the cards stay up until somebody answers them. That is the
# round `_wait_out_battle` then reports as 整場都讀不到戰利品面板.
#
# A lower fixed floor is not the answer: at 110 the *undimmed* village behind the
# panel comes through too, since its own background sits at 107 to 163. What is
# stable across both is the gap between the digits and what they are drawn over,
# so the retry scales its floor to the brightest thing in the row — 128 dimmed,
# 255 not — and asks for 85% of it. Dimmed that puts the line at 109, above the
# 102 background; undimmed it lands at 217, above the 163.
#
# It is a **retry rather than a replacement** so an ordinary frame reads exactly
# as it did: the relative floor only ever runs on a row the fixed one could not
# read.
#
# **What keeps a screen with no opponent from reading as one is the glyph match,
# not this floor**, and it is worth being exact about that because the floor
# offers no margin at all here. 正在搜尋對手 is itself a dim screen — measured on
# the committed frame, its loot box peaks at 102 to 105, so the retry runs on
# every poll of it with a floor of 86 to 89, *lower* than the 109 a genuinely
# dimmed panel gets. It still answers None, on every recorded frame and after
# darkening each scout fixture down to a twentieth of its brightness, because
# whatever ink comes through does not resolve as digits within
# `LOOT_DIGIT_TOLERANCE`. Widening that tolerance would be the change that makes
# this unsafe, not lowering the floor further.
DIM_INK_RATIO = 0.85

# How bright the loot panel is once the scout screen has finished fading in. The
# game fades the whole screen up when it puts a new opponent on it, and a frame
# caught in that fade carries the panel and the button at partial opacity —
# **which is a different thing from a panel a popup has dimmed**, the case
# `DIM_INK_RATIO` exists for, because there the numbers are settled underneath
# and here they are still arriving. Scaled to a half-drawn row, that retry
# answers rather than failing, and what it answers is wrong: measured on one
# such frame, elixir came back 4 and dark 62 against the 262 884 and 7 962 the
# same opponent read seconds later.
#
# **The population this has to separate is the frames `read_scout` answers on**,
# and only those, since its one caller asks it about nothing else — a frame that
# answered None takes the same path whatever this says. Among those, over 56
# recorded scout frames and every committed fixture, fading tops out at 191 and
# drawn bottoms out at 227, so the line goes midway across that gap rather than
# beside either edge. The spread is wider than it looks at either end: most
# drawn panels read 247, but `scout_dim_dark` is a dark-themed village at 227,
# and the brightest fading fixture is `scout_faint_panel` at 200, nine below
# this line.
#
# So the margin is 18 either side rather than the 26 a first pass claimed by
# reading the drawn frames as 246 to 247 and missing the dark theme. The panel
# is transparent, so its peak carries some of the village behind it and a theme
# darker than any measured here would read as still fading: that costs the
# opponent, which is skipped and honestly reported, rather than the army the
# other direction costs. A popup over the scout screen rather than over a
# battle is refused for the whole window for the same reason, and is the one
# dim state this treats as always a fade.
PANEL_DRAWN_BRIGHTNESS = 209

# The game paints these buttons in one saturated orange that nothing behind them
# comes close to, so a box around either doubles as a check on which screen is
# up: measured, the button's own screen reads 0.49 and 0.63, every other 0.004.
BUTTON_ORANGE = 0.2
NEXT_BUTTON_BOX = (1380, 595, 1525, 668)
FIND_MATCH_BOX = (150, 635, 400, 695)

# The builder base's own two screens, which have no orange on them at all: its
# 開始進攻 dialog offers 立即尋找 in green, and the matchmaker that opens is a
# near-empty pale field whose only feature is a red 取消.
#
# **The matchmaker is the one that has to be recognised**, because the builder
# base is real-time — it waits for another live player, and measured here that
# took five and a half minutes on one search. A loop that cannot tell that
# screen from a battle spends the wait tapping at it.
#
# The matchmaker takes one box: the red reads 0.87 on it against at most 0.20
# over 4320 recorded home village frames, and the closest of those is the 放棄
# button, which is red and sits in the corner of every battle.
SEARCHING_BOX = (720, 768, 880, 806)
SEARCHING_RED = 0.5

# **The dialog takes two**, and the reason is measured: its 立即尋找 reads 0.63
# of button green, and a battlefield read through the same box reads up to 0.69.
# A village is mostly grass, so one green button is not a screen. What no
# battlefield has is the dialog's own cream panel behind it, which fills its box
# outright — and what the matchmaker has instead is that pale field with no green
# button on it at all. Either test alone has false positives across those 4320
# frames; the two together have none.
NIGHT_FIND_BOX = (1100, 570, 1280, 615)
NIGHT_FIND_GREEN = 0.45
NIGHT_PANEL_BOX = (300, 465, 900, 500)
NIGHT_PANEL_PALE = 0.8

# The 聖水車 panel, which is what tapping the builder base's loot cart opens —
# it is not a collect-on-tap marker like the home village's collectors. The
# elixir sits in it until its 收集 is pressed, and the bar beside that button
# says how full it is; measured on the first one this loop opened, 300 000 of a
# 1 000 000 ceiling. One box does it: that button reads 0.82 of button green,
# against at most 0.07 over 4300 recorded frames of both villages.
CART_COLLECT_BOX = (1110, 740, 1245, 782)
CART_COLLECT_GREEN = 0.4

# A troop card keeps its artwork in colour while it still has something to put on
# the field and turns fully greyscale once it is spent. Measured across live
# frames, a spent card reads 0 and a live one 44 or more, which brightness alone
# does not separate at all.
CARD_TOP, CARD_BOTTOM = 760, 860
CARD_HALF_WIDTH = 45
CARD_SPENT_SATURATION = 10

# The row is laid out in groups — troops, siege machine, heroes, spells — and
# the only boundary that has to hold is the first one, because `_deploy` reads
# group 0 as the troops and flattens everything after it. Measured, cards sit
# 11 px apart inside a group, and across one the gaps are 34 (troops to siege),
# 16 (siege to heroes) and 25 (heroes to spells) — so the siege machine lands in
# with the heroes, which is exactly what the flattening is for. It only ever
# read as a group of its own while a seam was breaking the card beside it into
# pieces; see `CARD_SPAN`.
CARD_GROUP_GAP = 20
# Cards measure 109-112 px across, though a dark seam over one leaves an 87 px
# piece, so the floor stays low. Once the narrow fragments are gone every real
# card sits at least CARD_EDGE_GAP from its neighbour — which is what exposes
# the row's backing plate where it runs to the screen edge, since that abuts the
# first card instead of keeping a gap.
CARD_MIN_WIDTH = 80
CARD_EDGE_GAP = 5
# What one whole card spans, which is how a card broken in two is put back
# together. The strip is judged on a single averaged row of brightness, so a
# **dark band in a card's own artwork cuts that card in half** — and both halves
# can land under CARD_MIN_WIDTH, at which point the card is gone and nothing
# downstream can tell it was ever there. Measured live on a row of four heroes,
# the third came apart into 46 px and 63 px pieces and was dropped on every
# frame of a five-round run: all five battles reported "3 of 3 hero card(s)
# landed" while that hero sat in a card nothing knew about, and the five run
# afterwards reported 4 of 4. The first hero on the same row was cut the same
# way and survived only by luck, its remaining piece measuring 87.
#
# Two pieces are only joined where their combined span is one card wide, and
# only while neither is already wide enough to be a card on its own. Swept over
# 43 recorded frames a card at rest spans 105 to 112 px and every piece a seam
# leaves is 87 or less, so the floor sits between the two: a card that already
# reads is never joined to the speckle beside it, which on a battle frame is
# what would move its centre off the card and take every reader with it.
#
# The ceiling is that resting width rather than the 118 to 120 a **selected**
# card lights up to, because a card and the 11 px gap to its neighbour come to
# 121 and a ceiling reaching that far would join two cards into one. Nothing is
# selected when `card_groups` runs — `_deploy` reads the row before it taps
# anything — so the wider reading is out of its way.
CARD_SPAN = (100, 116)
# Except that the row is not always unselected when `card_groups` runs. The
# builder base's second stage opens with the surviving machine's card
# **preselected** — 118 px wide with a white border, so that the first tap or
# swipe on the field sends it — and its seam then holds a 1 px sliver of that
# border as well. Read at the resting ceiling the card came apart into 34 and
# 72 px pieces the sliver kept from rejoining, both under CARD_MIN_WIDTH, and
# the machine was gone from the row: `0 machine card(s) still alive` on every
# recorded second stage, so it was offered no ability at all there.
#
# The wider ceiling is allowed only between two white columns, which a
# fragment's own edges never are, and the sliver is only stepped over inside
# such a card. Swept over 2 297 recorded frames that changes exactly the frames
# holding a selected card — 124 with the machine, plus one home-village pass
# whose selected card had been reading 12 px off-centre — and nothing else,
# where the same ceiling for every span invented cards on the attack dialog and
# moved a real one. The border reads 249 to 255 against at most 130 for a
# resting card's edge, so the line sits well clear of both.
CARD_SELECTED_SPAN = 120
CARD_SELECTED_EDGE = 200
CARD_SLIVER = 4
CARD_LIT_BRIGHTNESS = 60
# Every real card carries its level in a badge at the bottom-left corner. The
# empty slot the row ends with does not: it is a dashed outline with the
# battlefield showing through, so it segments as a card of its own whenever the
# ground behind it is bright enough, and then arrives downstream as one more
# hero — which cost a run all five of that card's attempts, tapping nothing.
# Measured on a full row, every real card lights 0.15 of that badge or more and
# the empty slot lights none of it at all.
BADGE_LEFT, BADGE_RIGHT = -44, -4
BADGE_TOP, BADGE_BOTTOM = 838, 874
BADGE_BRIGHTNESS = 175
BADGE_LIT = 0.05

# Troop and spell cards carry an `xN` count in their top-right corner; hero and
# siege cards do not. Reading the number itself is not reliable — on a card over
# a pale illustration the count merges into the artwork — but its presence is.
#
# **What is white here is the count's own text, and a hero's artwork can be
# white too.** The ratio was 0.10, drawn between a counted card's 0.21 and the
# 0.03 an uncounted one read on the frames available then. 飛龍公爵's card
# breaks that upper figure on its own: its horns and teeth land squarely in this
# box and read 0.1386 — measured on `battle_in_progress.png` and again live on a
# battle where the loop then poured four rounds of twelve taps into that hero
# card, 56 seconds of a battle that had already deployed everything.
#
# **The floor is a builder base second stage rather than anything in the home
# village**, and it is much lower than the home village's own: swept over every
# committed frame, the 24 cards whose number `card_count` resolves read 0.2067
# or more, but `night_stage2_cards.png`, whose six troop cards
# `test_a_selected_machine_card_is_still_a_card` already asserts are counted,
# reads down to 0.1835. That is the number this line has to clear, because a
# troop card lost there is read as a machine and can take the whole stage with
# it. So the line goes at 0.16, midway across 0.1386 to 0.1835 rather than
# beside either edge, and the reading it now refuses is a hero's face.
COUNT_TOP, COUNT_BOTTOM = 748, 772
COUNT_LEFT, COUNT_RIGHT = 6, 58
COUNT_WHITE_RATIO = 0.16
# The count itself is readable on a card with a dark, saturated illustration and
# not on a pale one, so `card_count` reports a failed read rather than a guess.
# It needs a higher floor than the loot panel: the card art is brighter than a
# village. `x` leads every count and is 16 px wide, which is the sanity check.
COUNT_INK_BRIGHTNESS = 225
COUNT_X_WIDTH = (13, 19)
# A real count's digits sit at 18 and 9 bits off their templates over this
# artwork while the scraps of it that survive read 31, so the line goes between.
COUNT_DIGIT_TOLERANCE = 30

# Freeze is the one spell CoC draws in cyan. Measured over the recorded rows, a
# freeze card's lower half reads (141, 224, 242) against rage's (163, 137, 201),
# and every other card that reaches this test sits at 145 of green or below.
#
# Cyan is high green **and** high blue, and only the green half of that used to
# be asked. What the missing half lets through is a spell that is merely green,
# and heal is one: its bottle is green with none of the blue, so it would read
# as freeze and be held back for the defences when what it is for is the troops
# — where a spell this call does not claim already goes. Measured, blue runs 242
# on freeze against at most 201 on everything else, so the line sits between.
# Heal itself has not been measured here, because this village has never flown
# one; the blue floor comes from what cyan is rather than from a sample of it,
# which is why it is set off freeze's own margin and not off a guess at heal's.
SPELL_ART_TOP, SPELL_ART_BOTTOM = 790, 860
SPELL_ART_HALF_WIDTH = 40
FREEZE_GREEN = 190
FREEZE_BLUE = 220

# `305/305` on the 我的軍隊 screen, which is the last point before the search fee
# is charged. The troop icon before it and the slash between the two numbers are
# not digits, and that is exactly how they are found: every real digit matches a
# template within ARMY_DIGIT_TOLERANCE (measured 12-18) while the slash reads 25
# and the icon 74, so anything over the line splits one number from the next.
ARMY_BOX = (700, 192, 880, 230)
ARMY_INK_BRIGHTNESS = 200
ARMY_DIGIT_TOLERANCE = 22

# What a drop that landed leaves behind, which is the only evidence the game
# gives that can be trusted. A counted card repaints its `xN` corner, and it does
# so even where the number itself will not read: measured live, a corner whose
# card lost something differs in 368 to 1139 of its pixels, and one whose card
# did not differs in exactly none, selecting the card included.
#
# The red banner used to stand in for this and cannot. Measured live, a troop
# tapped inside the boundary is as often swallowed in silence as it is answered
# with 你無法在紅線區域內派遣部隊, while 請選擇其他兵種, 已部署所有兵力 and 該法術
# 已用完 are the same red in the same place — and so is a burning building, which
# put four flanks in a row through a push they never needed.
CARD_CORNER_INK = 40
CARD_CORNER_PIXELS = 20

# A hero's card does not empty when the hero lands: it turns into the ability
# button and keeps its colour, so `live_cards` cannot tell one that went down
# from one still waiting. The health bar the game draws over the card can —
# measured, it fills 0.33 to 0.37 of this strip while a hero still in the card
# leaves at most 0.02. It takes a second or so to appear, so read it after a wait.
#
# The bar sits above the card row, which means the battlefield shows through this
# strip until the bar is drawn, and grass is green too. A brightness-and-hue test
# read that as a bar — a hero reported as landed while it sits in its card, which
# is the exact failure this reader exists to catch. Swept over 126 recorded
# frames taken before anything had been deployed, across eleven battles and their
# themes, it called a hero landed on 52 of them; this test calls none.
#
# Blue is what separates them: measured over the two, the bar runs (101, 231, 9)
# and grass (131, 184, 53), so the bar is both greener and has almost no blue in
# it at all where grass keeps a third of a channel.
HERO_BAR_TOP, HERO_BAR_BOTTOM = 714, 738
HERO_BAR_HALF_WIDTH = 50
HERO_BAR_GREEN = 0.15
HERO_BAR_MIN_GREEN = 200
HERO_BAR_MAX_BLUE = 30

# The village's own storages, on the four bars down the home screen's right edge.
# Only the first three are read; the fourth is gems. The numbers are right-aligned
# against the icons, so the box reaches far enough left for eight digits and their
# separators, which is more than any storage holds.
#
# The tolerance was 22 on the reasoning that a real digit here matches within 11,
# and that turned out to be a measurement of one village rather than of the font:
# a gold row reading 10 649 867 matched every glyph but its 9, which came in at
# 25 and failed the whole row. It failed it on every frame, because the number
# does not change between them — so this was not a flicker to be retried, it was
# a village whose storages simply could not be read, and both callers treat that
# as "not now": the farming loop stops watching for a full storage and the wall
# loop cannot see what it has to spend.
#
# Swept over 48 live village frames the worst real glyph is that 25 and the rest
# sit at 15 or below, so the line goes above it. What the tolerance is not
# holding back is the other screens: measured over 18 frames of loading screens,
# the army screen and spend dialogs, not one produces a single glyph in any of
# these three boxes, so a screen that is not the home village reads as no screen
# whatever this is set to.
#
# The bars carry a gloss of their own along the top of the filled part, and it
# reaches into the row above the digits. Under the shared ink test it survives as
# a four-row blob, which is far too short to be a digit and would be thrown away
# for it — except that a blob landing in the gap between two digits joins them
# into one span too wide to be a glyph and too poor a match to be cut, and the
# whole row fails. Measured live on a village holding 130 480 dark, the gloss
# bridged the 1 and the 3 on every frame; every cut of that 24 px span left one
# half 33 bits or worse off its template, so the row read as nothing and `_home`
# spent five rounds pressing `back` at a village that was already up.
#
# The gloss is blue-grey, (143, 180, 203) give or take, where a digit is white —
# so what separates them is saturation rather than brightness, which the gloss
# clears by a handful of levels. Measured, the gloss reads 56 to 62 while all
# three rows of six recorded villages read the same number at every ceiling from
# 20 to 55, so the line goes between with room on both sides. Scoped to these
# rows because it is these bars the text is painted over; the loot panel and the
# army screen are painted over other things and keep the shared ceiling.
STOCK_LEFT, STOCK_RIGHT = 1300, 1512
# **The dark row gets its own left edge, because its number is shorter.** Gold
# and elixir cap at 24M and their eight figures reach back to about x 1338; dark
# caps at 370 000, so six figures put its leftmost digit at 1356 across every
# one of 107 recorded readings. That difference is the only room there is, and
# it is needed: measured live, a collector's own full marker floated behind this
# row and laid ink from 1300 to 1338, which failed the row — and `read_stock`
# failing is how `_home` decides it is not on the home village, so `walls`,
# `collect` and the attack loop all stood down together on a village plainly on
# screen, each reporting it could not get back to one.
#
# 1348 sits ten pixels clear of that marker and eight clear of the digits. The
# same edge **cannot** be given to the other two rows: tried, and a village
# holding 14 000 000 gold read back 4 000 000, the leading digit cut off.
#
# A gap threshold does not separate the marker from the row, because a real
# number carries gaps of up to 85 px between its digit groups, wider than the
# 41 px that separated this marker from its row.
#
# **This edge is no longer the only thing holding the row up**, and it is kept
# because it is free rather than because it is sufficient. `_read_row` drops the
# village off the left end now, which is what this comment used to say could not
# be done: the reading it named as the cost — a map-corner frame whose dark row
# led with a glyph 66 off its template — turns out to be village bleed and not a
# digit at all, because `world_day.png` is the same village away from the corner
# and reads back the same 311 298 without it. What forced the question is that
# the camera is now parked in that corner on purpose, so a bar this edge does not
# happen to clear stopped reading on every frame rather than on some.
STOCK_DARK_LEFT = 1348
STOCK_ROW_BOUNDS = ((33, 72), (117, 156), (200, 239))
STOCK_DIGIT_TOLERANCE = 30
# **A row that needed the village trimmed off it has to read cleanly to be
# believed**, which is a tighter bar than the one a row that read straight off
# clears. Dropping the leading glyph is also dropping the rule that made
# `STOCK_DIGIT_TOLERANCE` safe on screens that are not the home village — any
# poor glyph failed the row — so something has to take that job back for the
# rows this trims. Measured over every recorded frame, the rows where the trim
# is real read a worst glyph of 7, 7 and 11, while the five where it is a
# battle screen's own white text (the attacker's name, whose wide first letter
# trims as village) all read exactly 30. The line goes in that gap.
#
# It can only ever hand a trimmed row back the answer it had before any of this
# existed, which is None, so nothing that reads today can be reached from here.
STOCK_TRIMMED_TOLERANCE = 20
STOCK_INK_SATURATION = 45

# 最大儲存量 on the tooltip a tapped storage bar drops open, which is the one
# place the game writes down how much that storage holds. The panel hangs under
# the bar that opened it, so its first line moves with the row by the same pitch
# the bars themselves are spaced at.
#
# The line reads 最大儲存量 ： 24 000 000, and what makes it readable is that
# the label is not a number: swept over both villages, every real digit lands
# within 3 bits of its template while the Chinese and the colon read 52 and up,
# so `split_numbers` cuts the line at the label and the capacity is the last
# number left. `CAPACITY_TOLERANCE` goes well under that gap rather than beside
# it, because a value read too generously is worse than none — at 40 the colon
# came back as a 2 and the builder base's 2 450 000 read as 24 500 000, a
# ceiling ten times the real one that no farming run could ever fill.
#
# The box starts left of where any of these numbers do. It reaches past the
# label on the dark row, whose panel sits further right, and that costs nothing:
# label glyphs are cut away by the same tolerance.
CAPACITY_BOX = (1340, 103, 1585, 132)
CAPACITY_PITCH = 84
CAPACITY_TOLERANCE = 15

# The 回營 button on the battle result screen. It is the one screen a farming
# loop reliably ends on and the one it could not get off: the button only comes
# alive once the stars have finished flying in, so the single tap fired the
# moment the loot panel disappeared landed on nothing, and four runs in a row
# then found the village covered and stood down without attacking. Measured, the
# button fills 0.33 of this box in green against at most 0.05 of any other
# screen, the attack menu's own buttons included.
#
# **Except one, and it cost a farming run 40 minutes.** The game's event
# reward screens — 周期挑戰獎勵之路已完成 and its like — are pages of green
# tick marks, and one of them lands squarely in this box: measured, 0.2009,
# comfortably over the old 0.15 threshold. That screen closes on a red X in its
# own corner and answers nothing at all at 回營's position, so a loop reading it
# as a result screen taps a spot with no button on it for as long as it is
# allowed to. Measured live, 18 rounds of it, every one reporting
# 畫面不在建築大師基地 while a battle it had already matched into ran out
# underneath. So the line goes above that 0.2009, and `_leave_result` presses
# `back` when the tapping does not work, which is what gets out of the ones this
# reader has never seen.
#
# **The game draws this button two ways, and only one of them was ever
# measured.** The 0.3283 that set the old 0.25 is `battle_result.png`, a plain
# green plate. The live game also draws it lit — a white border and a washed-out
# fill, which is the same button offered as the primary action — and that reads
# **0.2488**, just under the line. Swept over every recorded frame of a day of
# farming, 20 of 21 result screens were the lit kind and not one cleared 0.25,
# so `battle_over` was answering False on the real thing: `_wait_out_battle`
# spent its whole `BATTLE_TIMEOUT` on every round, and `_leave_result` returned
# without ever tapping 回營. Four minutes a round, and the loop limped home on
# `_home`'s `back` instead. The tests passed throughout, because the only
# fixture was the plain kind — which is why `battle_result_lit.png` is committed
# beside it now.
#
# The same sweep is what sizes the new line: every frame that is not a result
# screen reads at most 0.2057 (a mid-deployment capture), so 0.23 sits between
# that and the lit button's 0.2488 with the plain one far above both.
RETURN_HOME_BOX = (690, 738, 910, 796)
RETURN_HOME_GREEN = 0.23

# The 放棄 plate, which is what says a battle is still being fought. **A card row
# is not that, and reading it as one is what left a loop tapping inside the
# shop.** `card_groups` segments a band across the bottom of the frame into
# card-shaped bright patches, so anything with a row of portraits or tiles there
# answers it: measured, the 探礦者 panel reads three cards off its builder
# portraits and the shop's 外觀 page reads a row off the skins it is selling.
# Both were then taken for battles — the loop kept casting into them, and
# `uncovered` refused to press `back` at what it thought was a battle, which is
# a deadlock rather than a wasted round. One incident held a run for ten minutes
# across ten rounds; another spent twenty frames browsing hero skins at $330.
#
# **The box is where the two villages overlap, not where either button is.** The
# home village draws 放棄 at about y 645-690 and the builder base draws 結束戰鬥
# higher, at about y 600-655, so a box cut to the home village's own button
# reads the builder base at 0.2690 — and this is asked in both worlds, so a line
# drawn above that would press `back` at a builder base battle, which is the one
# thing the filter exists to prevent. The 24 px both buttons cross reads 0.6065
# to 0.6153 over every recorded home battle and 0.7539 on the builder base
# fixture, against 0.0000 for the two panels this fixes.
#
# Swept over every recorded frame this is ever asked about — `current_world`
# naming no village, no result screen up — a battle never reads under 0.6065 and
# the panels never read over 0.0000. The line goes at 0.45, which is a fifth of
# the way down from the lowest battle and nowhere near anything else.
#
# A scout screen is the one thing that lands on both sides: its own 結束戰鬥 sits
# where 放棄 does, so an opponent on offer reads 0.6065 like a battle while
# 正在搜尋對手 reads nothing at all. Both answers are safe — the first leaves a
# scout screen alone, which is what `_scout` wants, and the second presses
# `back` at a search that has nothing to lose.
#
# **The builder base draws no plate at all until its countdown ends**, which is
# the one place this reads False over a live battle: measured across a recorded
# builder base round, the 40 s of 離戰鬥開始剩下 read 0.0000 with a full card row
# and the fighting that followed read 0.7539 to 0.7715 on every frame. That
# window was measured rather than reasoned about, by pressing `back` in it on
# the live game: it raises 確認退出遊戲, the same dialog a clear village raises,
# not a surrender — and the countdown carried on underneath. `uncovered` only
# ever presses `back` and never answers a dialog, so the worst it costs there is
# the None it already returned before any of this.
ABANDON_BOX = (20, 636, 200, 660)
ABANDON_RED = 0.45

# 還在嗎 / 你因閒置過久而中斷連線. A loop that spends minutes waiting for barracks
# will meet this, and nothing else clears it: the game stops responding to taps
# until 重新登入遊戲 is pressed. Measured, its flat grey panel fills 0.97 of this
# box where a village reads 0.14 and even the result screen only 0.45.
#
# 連線已中斷 / 與伺服器連線中斷 is the same sheet with 再試一次 where the button
# was, and it reads here too — measured at 0.94 dark and 1.00 flat on a live
# capture. Nothing tells the two apart, and nothing needs to: a restart is the
# answer to both, since it logs in again exactly as either button would.
IDLE_DIALOG_BOX = (400, 340, 1200, 560)
IDLE_DIALOG_DARK = 0.7

# 正在載入. The bar the game draws while it connects, which is where a game sits
# when the server will not answer: measured twice in one night at 25 to 40
# minutes each, with the fill never moving off 1%. The bar is UI painted over a
# splash that changes with the season, so what is read is the bar alone — the
# right-hand end of its grey plate, clear of the 正在載入 label written across
# its middle, and the purple fill at its left end, which is what no other screen
# carries. A plate pixel counts whether it is still grey or already purple, so
# a bar that has crept along reads the same as one stuck at the start. Swept
# over 11 464 recorded frames: the twenty that were this screen read 0.887 on
# the fill, a grey strip alone reaches 1.000 on a result screen, and no other
# frame puts more than 0.006 of purple in the fill box. Both are required, and
# the fill is what makes it clean.
LOADING_PLATE = (860, 763, 985, 772)
LOADING_FILL = (602, 760, 612, 776)
LOADING_PLATE_FLAT = 0.9
LOADING_FILL_PURPLE = 0.5


def battle_over(png: bytes) -> bool:
    """Whether the battle result screen is up with its 回營 button waiting."""
    data = open_frame(png).crop(RETURN_HOME_BOX).tobytes()
    green = sum(
        data[i + 1] > 150 and data[i + 1] - data[i] > 45 and data[i + 1] - data[i + 2] > 60
        for i in range(0, len(data), 3)
    )
    return green / (len(data) // 3) >= RETURN_HOME_GREEN


def idle_disconnected(png: bytes) -> bool:
    """Whether a dropped-session dialog is covering the game.

    Two dialogs read here and both mean the same thing to a caller: the idle
    one (還在嗎, with 重新登入遊戲) and the lost-connection one (連線已中斷, with
    再試一次). Either way the session is gone and restarting the game is what
    gets it back.
    """
    data = open_frame(png).crop(IDLE_DIALOG_BOX).tobytes()
    panel = sum(
        max(data[i], data[i + 1], data[i + 2]) < 95
        and max(data[i], data[i + 1], data[i + 2]) - min(data[i], data[i + 1], data[i + 2]) < 30
        for i in range(0, len(data), 3)
    )
    return panel / (len(data) // 3) >= IDLE_DIALOG_DARK


def _purple(r: int, g: int, b: int) -> bool:
    return r > 110 and b > 110 and g < r - 40


def loading_screen(png: bytes) -> bool:
    """Whether the game is on 正在載入, which is where it waits for the server.

    Nothing on this screen answers a tap, so a loop that reads it has nothing
    to press and nothing to open: the only thing to do is wait. See the
    constants above for what is measured and why both halves are needed.
    """
    image = open_frame(png)
    plate = image.crop(LOADING_PLATE).tobytes()
    flat = sum(
        _purple(plate[i], plate[i + 1], plate[i + 2])
        or (
            max(plate[i], plate[i + 1], plate[i + 2]) - min(plate[i], plate[i + 1], plate[i + 2])
            < 25
            and 60 < max(plate[i], plate[i + 1], plate[i + 2]) < 230
        )
        for i in range(0, len(plate), 3)
    )
    fill = image.crop(LOADING_FILL).tobytes()
    purple = sum(_purple(fill[i], fill[i + 1], fill[i + 2]) for i in range(0, len(fill), 3))
    return (
        flat / (len(plate) // 3) >= LOADING_PLATE_FLAT
        and purple / (len(fill) // 3) >= LOADING_FILL_PURPLE
    )


def _read_row(image: Image.Image, box: tuple[int, int, int, int], tolerance: int) -> int | None:
    """One storage bar's number, read off the bar the game paints it on.

    The tighter saturation ceiling belongs to those bars rather than to rows in
    general; see `STOCK_INK_SATURATION` for what it is holding back.

    **The village shows through to the left of the digits, so that is the one end
    a poor match is dropped from.** This is `_read_loot_row` mirrored, and the
    mirror is the bar itself: the unfilled part of a storage bar is translucent
    and the number is right-aligned against the bar's far end, so whatever the
    camera leaves behind that track lands to the left of every digit. A poor
    match with digits still to its left is a different thing — a digit this frame
    cannot read — and fails the row rather than being dropped, because dropping
    one there divides the reading by ten.

    **What is dropped is village and not a badly drawn leading digit**, and the
    only thing that settles that from a still frame is another frame of the same
    village without the bleed on it. Both cases here have one:
    `world_day_corner.png` reads what `world_day.png` reads, and the frame that
    prompted this reads what a capture of the same village two minutes earlier
    read, before the camera was parked.

    **The trim is judged by what survives it, not by what it removed**, and
    `STOCK_TRIMMED_TOLERANCE` is where that is measured. Going by the dropped
    glyph alone does not hold up: a real leading digit lands within 12 of its
    template and the glyphs dropped here start at 31, but the builder base's
    gems bar puts its green `+` in the dark row at exactly 30 — one bit under
    the line, and the reason `read_stock` reports the documented `dark=410152`
    there. A bound drawn against that is a bound where one bit picks between two
    numbers rather than between a number and no answer.
    """
    glyphs = list(row_glyphs(ink_mask(image.crop(box), saturation=STOCK_INK_SATURATION)))
    trimmed = False
    while glyphs and glyphs[0][1] > tolerance:
        glyphs.pop(0)
        trimmed = True
    if trimmed:
        tolerance = STOCK_TRIMMED_TOLERANCE
    if not glyphs or any(distance > tolerance for _, distance in glyphs):
        return None
    return int("".join(digit for digit, _ in glyphs))


def _read_loot_row(image: Image.Image, box: tuple[int, int, int, int]) -> int | None:
    """One row of the loot panel, with the village showing through it dropped.

    The right of the digits is the one end a poor match can be dropped from,
    because it is the only end with room to spare: loot runs to seven figures and
    the leftmost of those starts at the box's own edge, so a left-end intruder
    cannot be told from the first digit by position. It is not that the village
    never bleeds in there — `_lit_floor` was written for a theme that does — only
    that nothing here can trim it. Dropping a right-end match rather than failing
    the row is the opposite of `_read_row` and deliberately so: `read_scout`
    returning None means "no opponent on screen", so one speckle of village would
    leave the loop waiting out a search it had already paid for.

    A poor match with digits still to its right is different in kind: it is a
    digit this frame cannot read, and dropping it silently divides the number by
    ten. Measured on one opponent's gold row, the second 7 of 1 047 758 swung
    between 14 and 42 bits off its template from frame to frame, so four readings
    in ten came back as 104 758 — which a 500k threshold skips outright. The row
    fails instead, and the caller reads the next frame.
    """
    crop = image.crop(box)
    for floor in (_lit_floor(crop), _dimmed_floor(crop)):
        if floor is None:
            break
        glyphs = list(row_glyphs(ink_mask(crop, floor)))
        while glyphs and glyphs[-1][1] > LOOT_DIGIT_TOLERANCE:
            glyphs.pop()
        if any(distance > LOOT_DIGIT_TOLERANCE for _, distance in glyphs):
            continue
        if digits := "".join(digit for digit, _ in glyphs):
            return int(digits)
    return None


def _lit_floor(crop: Image.Image) -> int:
    """The fixed floor, raised where this row is bright enough to need it.

    **A bright village theme bleeds into the panel from both ends**, and the
    fixed floor is what lets it. Measured on a football-stadium opponent, the
    gold row's thousands space filled in so `02 9` came through as one 58 px
    span — no cut leaves two halves the size of a digit, so it read
    `NOT_A_GLYPH` — while the elixir row picked up an extra glyph at its *left*
    end, which nothing can trim: a seven-figure reading starts at the box's own
    edge, so `PANEL_LEFT` cannot move right. The whole panel then read as no
    opponent for an entire battle, and `_wait_out_battle` could only report that
    it had no idea how the round went.

    The digits are the brightest thing in their row and what bleeds in is not,
    so the same ratio `_dimmed_floor` already scales by separates them here —
    this is that mechanism pointed the other way. **It only ever raises**, which
    is what keeps every dim frame reading exactly as it did: a row peaking below
    `LOOT_INK_BRIGHTNESS / DIM_INK_RATIO` gets the fixed floor unchanged, so a
    popup's dimmed panel and a half-painted one in a transition are untouched.

    Scaling in both directions was measured and is not safe. Always scaling
    reads a transition frame's half-drawn gold row — peaking at 168 where its
    neighbours reach 195 — as 1289 where the number is 1 289 828, which a
    threshold then skips as poor. Swept over 4 700 frames, raise-only at this
    ratio reads 87 rows that failed outright and leaves every other reading
    identical; at 0.90 it starts truncating (1 660 000 as 166 000) and drops
    `scout_dim_dark`, and at 0.80 it recovers 39.

    **What the sweep cannot say is whether a row it gained is right**, since
    there was no prior reading to compare one against. Most of the 87 are
    consecutive frames of one battle whose numbers fall monotonically as the
    loot drains, which no speckle produces; two were checked against the digits
    visible on the frame and are the fixtures behind this. The one direction it
    does settle outright is the builder base, where this box is battlefield: 0
    of 3 214 frames gained anything.

    **This replaces the fixed attempt rather than adding one**, so a bright row
    whose digits sit between the fixed floor and the raised one is lost rather
    than retried. Nothing in the corpus does, and the alternative is worse: a
    row that failed at the raised floor failed with the bleed already excluded,
    so dropping back to the fixed floor could only re-admit it.
    """
    return max(LOOT_INK_BRIGHTNESS, int(max(crop.tobytes()) * DIM_INK_RATIO))


def _dimmed_floor(crop: Image.Image) -> int | None:
    """An ink floor scaled to this row, or None when the row is not dimmed at all.

    **None for anything that reaches the fixed floor**, and that guard is the
    whole of what keeps this safe. Scaling it on every row instead lowers the
    floor slightly on ordinary frames too — a row peaking at 220 lands at 187
    rather than 190 — which is enough village speckle to turn a row that had
    honestly failed into a short number. Swept over every frame here, that read
    a scout panel's gold as 1 and another's elixir as 2, and a loop believing
    those skips an opponent holding half a million.
    """
    # Every byte of an RGB crop, which is the brightest channel of the brightest
    # pixel — the same answer a per-pixel scan gives, an order of magnitude faster.
    brightest = max(crop.tobytes())
    if brightest >= LOOT_INK_BRIGHTNESS:
        return None
    return int(brightest * DIM_INK_RATIO)


def _orange_ratio(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    data = image.crop(box).tobytes()
    orange = sum(
        data[i] > 190 and 110 < data[i + 1] < 205 and data[i + 2] < 110
        for i in range(0, len(data), 3)
    )
    return orange / (len(data) // 3)


def skip_offered(png: bytes) -> bool:
    """Whether 下一個 is on screen, which takes none of the loot digits to answer.

    `read_scout` says None both for 正在搜尋對手 and for an opponent whose loot
    panel this frame cannot read, and those want opposite things from a caller:
    the first is worth waiting out and the second is worth leaving. This is the
    only part of that screen that separates them, because it is read off one
    saturated orange rather than off the digits — the same test `can_skip`
    already uses, asked without needing a whole `ScoutView` to exist first.
    """
    image = open_frame(png)
    return _orange_ratio(image, NEXT_BUTTON_BOX) >= BUTTON_ORANGE


def panel_drawn(png: bytes) -> bool:
    """Whether the scout screen has finished fading in, read off the loot panel's own peak.

    **`can_skip` means two opposite things on a frame that has not**, and only
    one of them is safe to act on. The 下一個 button being absent is how the
    countdown having expired is recognised, and a caller reads that as a battle
    it has no choice but to play; a button that has simply not painted yet reads
    exactly the same. Measured over 56 recorded scout frames, every reading of
    `can_skip=False` came from the second kind, and two rounds of one evening
    sent an army at an opponent nobody had evaluated because of it.

    Only the search loop asks this, and only about a frame `read_scout` has
    already answered on. `_wait_out_battle` polls the same panel through that
    reader while a battle runs, and there a dim panel is an event popup over
    settled numbers — which `DIM_INK_RATIO` is built to read and this would
    refuse.
    """
    image = open_frame(png)
    return (
        max(
            max(image.crop((PANEL_LEFT, top, PANEL_RIGHT, bottom)).convert("L").tobytes())
            for top, bottom in ROW_BOUNDS
        )
        >= PANEL_DRAWN_BRIGHTNESS
    )


def attack_menu_open(png: bytes) -> bool:
    """Whether the 多人遊戲 menu is up with its 尋找對戰目標 button.

    The attack loop opens with taps that only mean anything on the home village.
    An agent job that finished somewhere else would otherwise send them into
    whatever screen was left showing.
    """
    image = open_frame(png)
    return _orange_ratio(image, FIND_MATCH_BOX) >= BUTTON_ORANGE


def _button_ratio(image: Image.Image, box: tuple[int, int, int, int], hue: str) -> float:
    """How much of this box is the game's own button green or red."""
    data = image.crop(box).tobytes()
    lit = sum(
        (data[i + 1] > 150 and data[i + 1] - max(data[i], data[i + 2]) > 45)
        if hue == "green"
        else (data[i] > 150 and data[i] - max(data[i + 1], data[i + 2]) > 80)
        for i in range(0, len(data), 3)
    )
    return lit / (len(data) // 3)


def _panel_ratio(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    """How much of this box is the cream a full-screen dialog is drawn on."""
    data = image.crop(box).tobytes()
    pale = sum(
        data[i] > 215
        and data[i + 1] > 205
        and data[i + 2] > 185
        and max(data[i], data[i + 1], data[i + 2]) - min(data[i], data[i + 1], data[i + 2]) < 45
        for i in range(0, len(data), 3)
    )
    return pale / (len(data) // 3)


def night_attack_menu(png: bytes) -> bool:
    """Whether the builder base's 開始進攻 dialog is up with its 立即尋找 button.

    The builder base's own attack button sits in the same corner as the home
    village's, so the tap that opens this is the tap that opens the other; only
    what comes up separates them, and `attack_menu_open` does not recognise this
    one. Without this, a loop pointed at the builder base spends every attempt
    tapping a dialog it cannot see and reports the game as stuck.

    Two features rather than one, because a village is mostly grass and the
    button is green; see the constants for the frames each half lets through.
    """
    image = open_frame(png)
    return (
        _button_ratio(image, NIGHT_FIND_BOX, "green") >= NIGHT_FIND_GREEN
        and _panel_ratio(image, NIGHT_PANEL_BOX) >= NIGHT_PANEL_PALE
    )


def loot_cart_open(png: bytes) -> bool:
    """Whether the builder base's 聖水車 panel is up with its 收集 button.

    Tapping the cart opens this rather than collecting outright, so a caller
    that stopped at the tap has collected nothing at all — which is what the
    storage bars said the first time this was tried.
    """
    image = open_frame(png)
    return _button_ratio(image, CART_COLLECT_BOX, "green") >= CART_COLLECT_GREEN


def in_battle(png: bytes) -> bool:
    """Whether a battle is on screen, by the red plate that leaves one.

    The question `card_groups` was standing in for, and could not answer: a card
    row says something card-shaped is along the bottom of the frame, which the
    game's own panels have as readily as a battle does. The plate in the corner
    — 放棄 in the home village, 結束戰鬥 in the builder base and on the scout
    screen — is on none of those panels.

    **False does not mean the battle is over**, and a caller reusing this on its
    own has to know it: the game dims the whole screen behind its own popups,
    which takes the plate under the red floor as readily as it takes the loot
    digits (`battle_dimmed_by_popup.png`, a home battle at 66% with two minutes
    left, reads 0.0000), and the builder base draws no plate at all until its
    countdown ends. What this answers is "a battle is definitely on screen",
    which is what a filter guarding a `back` press wants; "the battle has ended"
    is `battle_over`'s question and stays with it.
    """
    return _button_ratio(open_frame(png), ABANDON_BOX, "red") >= ABANDON_RED


def searching_opponent(png: bytes) -> bool:
    """Whether the builder base matchmaker is still looking, by its 取消 button.

    This screen has no timer on it and no other feature to read: it is a pale
    field, the word 正在搜尋對手, and one red button. The button is what says the
    search is still running, and therefore what a caller waits on — and what it
    taps when the wait has gone on long enough to be worth restarting.
    """
    image = open_frame(png)
    return _button_ratio(image, SEARCHING_BOX, "red") >= SEARCHING_RED


def _badged(image: Image.Image, centre: int) -> bool:
    """Whether this slot carries a card's level badge, which the empty one does not."""
    data = image.crop((
        centre + BADGE_LEFT,
        BADGE_TOP,
        centre + BADGE_RIGHT,
        BADGE_BOTTOM,
    )).tobytes()
    lit = sum(
        max(data[i], data[i + 1], data[i + 2]) > BADGE_BRIGHTNESS for i in range(0, len(data), 3)
    )
    return lit / (len(data) // 3) >= BADGE_LIT


def _rejoined(spans: list[tuple[int, int]], columns: bytes) -> list[tuple[int, int]]:
    """Pieces a dark seam cut one card into, put back together; see `CARD_SPAN`.

    `columns` is the strip's brightness per column, which is what says whether
    the piece being joined onto starts on a selected card's white border; see
    `CARD_SELECTED_SPAN` for what that changes.
    """
    joined: list[tuple[int, int]] = []
    for left, right in spans:
        fragment = bool(joined) and joined[-1][1] - joined[-1][0] < CARD_SPAN[0]
        selected = fragment and columns[joined[-1][0]] >= CARD_SELECTED_EDGE
        if selected and right - left < CARD_SLIVER:
            continue
        ceiling = CARD_SPAN[1]
        if selected and columns[right - 1] >= CARD_SELECTED_EDGE:
            ceiling = CARD_SELECTED_SPAN
        if (
            fragment
            and right - left < CARD_SPAN[0]
            and CARD_SPAN[0] <= right - joined[-1][0] <= ceiling
        ):
            joined[-1] = (joined[-1][0], right)
        else:
            joined.append((left, right))
    return joined


def card_groups(png: bytes) -> list[list[int]]:
    """Card centres in the battle row, split into the groups the game lays them out in.

    How many cards fall in each group depends on the army, so callers are meant
    to read the first group as the main troops and the rest as one-off drops,
    rather than trying to name which group is which.

    Only valid on a full row. A spent card greys out below the detection floor
    and the row fragments, at which point the battlefield visible past its ends
    reads as a card too.

    A card whose own artwork is dark enough to break the strip is put back
    together before anything is measured, because a piece narrow enough to be
    dropped takes the whole card with it; `CARD_SPAN` is what that costs and how
    it is judged.
    """
    image = open_frame(png)
    strip = image.crop((0, CARD_TOP, image.width, CARD_BOTTOM)).convert("L")
    columns = strip.resize((image.width, 1), Image.Resampling.BILINEAR).tobytes()
    pieces: list[tuple[int, int]] = []
    start: int | None = None
    for x in range(image.width + 1):
        lit = x < image.width and columns[x] > CARD_LIT_BRIGHTNESS
        if lit and start is None:
            start = x
        elif not lit and start is not None:
            pieces.append((start, x))
            start = None
    spans = [span for span in _rejoined(pieces, columns) if span[1] - span[0] >= CARD_MIN_WIDTH]
    if len(spans) > 1 and spans[1][0] - spans[0][1] < CARD_EDGE_GAP:
        spans = spans[1:]
    spans = [span for span in spans if _badged(image, (span[0] + span[1]) // 2)]
    groups: list[list[int]] = []
    for index, (left, right) in enumerate(spans):
        if index == 0 or left - spans[index - 1][1] > CARD_GROUP_GAP:
            groups.append([])
        groups[-1].append((left + right) // 2)
    return groups


def counted_cards(png: bytes, slots: Sequence[int]) -> list[int]:
    """Which of these cards show an `xN` count, which is to say troops or spells.

    Heroes and the siege machine are the ones without it, which is what lets the
    attack loop drop those and leave spells alone.
    """
    image = open_frame(png)
    counted: list[int] = []
    for centre in slots:
        data = image.crop((
            centre + COUNT_LEFT,
            COUNT_TOP,
            centre + COUNT_RIGHT,
            COUNT_BOTTOM,
        )).tobytes()
        white = sum(
            max(data[i], data[i + 1], data[i + 2]) > INK_BRIGHTNESS + 25
            and max(data[i], data[i + 1], data[i + 2]) - min(data[i], data[i + 1], data[i + 2])
            < 60
            for i in range(0, len(data), 3)
        )
        if white / (len(data) // 3) >= COUNT_WHITE_RATIO:
            counted.append(centre)
    return counted


def card_count(png: bytes, slot: int) -> int | None:
    """The `xN` on one card, or None when the artwork behind it swallows the digits.

    Lets a one-off drop be tapped as many times as the card actually holds
    instead of a fixed guess. A pale illustration merges into the count, and
    that shows up as an `x` glyph of the wrong width, so it reports the failure.

    The `x`'s width was the only check here, and it is not enough: what follows
    it was matched against the templates with no tolerance at all, so any scrap
    of card art left standing became a digit. Measured, a four-pixel sliver off
    the `x` matched a 1 at 31 bits and turned a card of twelve into one of a
    hundred and twenty-one.

    **This reads the home village only, and reports the builder base as
    unreadable rather than wrongly.** The two write the count differently — `x4`
    there against `4x` here — and the builder base draws it half as big again,
    18 px against 9 to 15. Both of those were measured while trying to make one
    reader serve both, and both say not to: finding the `x` by which end matches
    no digit template breaks a home card whose own digit is as wide as its `x`
    (`cards_full`'s third card reads 2 and would stop reading at all), and the
    builder base's digits miss every template so far that its 4 comes back as a
    9, 23 bits off — inside `COUNT_DIGIT_TOLERANCE`, so it would be believed. A
    count nobody can read costs the fallback tap count; a count read as more than
    twice what the card holds costs the burst that follows it.
    """
    image = open_frame(png)
    band = image.crop((slot + COUNT_LEFT, COUNT_TOP, slot + COUNT_RIGHT, COUNT_BOTTOM))
    mask = ink_mask(band, COUNT_INK_BRIGHTNESS)
    spans = [(a, b) for a, b in glyph_columns(mask) if b - a > 3]
    if not spans or not COUNT_X_WIDTH[0] <= spans[0][1] - spans[0][0] <= COUNT_X_WIDTH[1]:
        return None
    digits = ""
    for left, right in spans[1:]:
        pattern = signature(mask, left, right)
        if pattern is None:
            continue
        digit, distance = nearest(pattern)
        if distance > COUNT_DIGIT_TOLERANCE:
            return None
        digits += digit
    return int(digits) if digits else None


def _corner(image: Image.Image, slot: int) -> bytes:
    return (
        image
        .crop((slot + COUNT_LEFT, COUNT_TOP, slot + COUNT_RIGHT, COUNT_BOTTOM))
        .convert("L")
        .tobytes()
    )


def card_drained(before: bytes, after: bytes, slots: Sequence[int]) -> list[int]:
    """Which of these counted cards actually put something on the field.

    The `xN` corner is repainted whenever a card loses one, which answers the
    question the loop keeps asking — did that drop land — without needing to read
    the number, and without believing a banner that means four different things.
    """
    first, second = open_frame(before), open_frame(after)
    drained: list[int] = []
    for centre in slots:
        moved = sum(
            abs(a - b) > CARD_CORNER_INK
            for a, b in zip(_corner(first, centre), _corner(second, centre), strict=False)
        )
        if moved >= CARD_CORNER_PIXELS:
            drained.append(centre)
    return drained


def field_units(png: bytes, slots: Sequence[int]) -> list[int]:
    """Which of these cards have their unit alive on the field, by its health bar.

    This is the only thing such a card says. It stays lit and stays counted
    once the unit is down — a hero's has become the ability button — so nothing
    else on it moves.

    A unit, not a hero: measured on a recorded run, the game draws the same bar
    over a siege machine, so the caller cannot use this to tell the two apart.
    """
    image = open_frame(png)
    down: list[int] = []
    for centre in slots:
        data = image.crop((
            centre - HERO_BAR_HALF_WIDTH,
            HERO_BAR_TOP,
            centre + HERO_BAR_HALF_WIDTH,
            HERO_BAR_BOTTOM,
        )).tobytes()
        green = sum(
            data[i + 1] > HERO_BAR_MIN_GREEN
            and data[i + 2] < HERO_BAR_MAX_BLUE
            and data[i + 1] - data[i] > 60
            for i in range(0, len(data), 3)
        )
        if green / (len(data) // 3) >= HERO_BAR_GREEN:
            down.append(centre)
    return down


def army_strength(png: bytes) -> tuple[int, int] | None:
    """Trained and total army size off the 我的軍隊 screen, or None if not on it.

    Checked before the attack is confirmed, because the search fee is charged
    after that and an army still being trained is not worth paying it for.
    """
    image = open_frame(png)
    mask = ink_mask(image.crop(ARMY_BOX), ARMY_INK_BRIGHTNESS)
    found = split_numbers(mask, ARMY_DIGIT_TOLERANCE)
    if len(found) != 2:
        return None
    return found[0], found[1]


def freeze_cards(png: bytes, slots: Sequence[int]) -> list[int]:
    """Which of these spell cards hold freeze, the one spell worth holding back.

    Both halves of cyan are asked for; see `FREEZE_BLUE` for what the green one
    alone lets through.
    """
    image = open_frame(png)
    frozen: list[int] = []
    for centre in slots:
        data = image.crop((
            centre - SPELL_ART_HALF_WIDTH,
            SPELL_ART_TOP,
            centre + SPELL_ART_HALF_WIDTH,
            SPELL_ART_BOTTOM,
        )).tobytes()
        pixels = len(data) // 3
        green = sum(data[i + 1] for i in range(0, len(data), 3)) / pixels
        blue = sum(data[i + 2] for i in range(0, len(data), 3)) / pixels
        if green > FREEZE_GREEN and blue > FREEZE_BLUE:
            frozen.append(centre)
    return frozen


def live_cards(png: bytes, slots: Sequence[int]) -> list[int]:
    """Which of these card-row positions still have something left to deploy.

    Lets the attack loop keep emptying only the cards that are not done yet,
    rather than guessing a tap count that a bulk troop card would outlast.
    """
    image = open_frame(png)
    live: list[int] = []
    for centre in slots:
        data = image.crop((
            centre - CARD_HALF_WIDTH,
            CARD_TOP,
            centre + CARD_HALF_WIDTH,
            CARD_BOTTOM,
        )).tobytes()
        spread = sum(
            max(data[i], data[i + 1], data[i + 2]) - min(data[i], data[i + 1], data[i + 2])
            for i in range(0, len(data), 3)
        )
        if spread / (len(data) // 3) > CARD_SPENT_SATURATION:
            live.append(centre)
    return live


def selected_cards(png: bytes, slots: Sequence[int]) -> list[int]:
    """Which of these cards the game is drawing selected, by the white border.

    A selected card is the one the next tap on the field deploys from, and the
    builder base's second stage opens with the surviving machine's card in that
    state. The border is what `CARD_SELECTED_EDGE` measures: 249 to 255 bright
    at both edges of the card against at most 130 for one at rest, read off the
    same averaged strip `card_groups` cuts the row on. A three-column window
    either side covers the pixel or two a centre moves between frames.
    """
    image = open_frame(png)
    strip = image.crop((0, CARD_TOP, image.width, CARD_BOTTOM)).convert("L")
    columns = strip.resize((image.width, 1), Image.Resampling.BILINEAR).tobytes()
    half = CARD_SELECTED_SPAN // 2
    selected: list[int] = []
    for centre in slots:
        left = columns[centre - half - 1 : centre - half + 2]
        right = columns[centre + half - 2 : centre + half + 1]
        if max(left) >= CARD_SELECTED_EDGE and max(right) >= CARD_SELECTED_EDGE:
            selected.append(centre)
    return selected


def read_scout(png: bytes) -> ScoutView | None:
    """The opponent on screen, or None when this screenshot shows no opponent at all.

    Tapping 下一個 leaves the game on 正在搜尋對手 for a moment, and that screen
    has no digits where the panel would be, so a failed read is how the caller
    learns to keep waiting rather than a separate screen classifier.
    """
    image = open_frame(png)
    gold, elixir, dark = (
        _read_loot_row(image, (PANEL_LEFT, top, PANEL_RIGHT, bottom)) for top, bottom in ROW_BOUNDS
    )
    if gold is None or elixir is None or dark is None:
        return None
    view = ScoutView(
        loot=LootOffer(gold=gold, elixir=elixir, dark=dark),
        can_skip=_orange_ratio(image, NEXT_BUTTON_BOX) >= BUTTON_ORANGE,
    )
    logger.info(
        "Scouted gold=%d elixir=%d dark=%d skippable=%s",
        view.loot.gold,
        view.loot.elixir,
        view.loot.dark,
        view.can_skip,
    )
    return view


def read_builder_stock(png: bytes) -> VillageStock | None:
    """The builder base's storages, which are its first two rows and nothing else.

    **`read_stock` cannot be used there.** That village has no dark elixir, and
    its gems bar sits at exactly the y the dark row is read from — measured, a
    builder base holding 10 152 gems reports `dark=410152`, the green `+` beside
    the number reading as a leading 4. A number that wrong travelling as a
    resource is how a limit ends up checked against a bar belonging to something
    else, so it is not read at all: `dark` comes back 0, and that village's
    `StorageCapacity` carries no dark ceiling either, so nothing compares them.
    """
    image = open_frame(png)
    gold, elixir = (
        _read_row(image, (STOCK_LEFT, top, STOCK_RIGHT, bottom), STOCK_DIGIT_TOLERANCE)
        for top, bottom in STOCK_ROW_BOUNDS[:2]
    )
    if gold is None or elixir is None:
        return None
    logger.info("Builder base holds gold=%d elixir=%d", gold, elixir)
    return VillageStock(gold=gold, elixir=elixir, dark=0)


def storage_capacity(png: bytes, row: int) -> int | None:
    """What this row's tooltip says the storage holds, or None where none is open.

    `row` is the storage bar's own index down the corner, the same 0/1/2 the
    stock rows are read at. Tapping a bar drops the tooltip open under it and
    tapping again puts it away, so the caller owns that toggle; this only reads
    whatever is on the frame it is handed.

    None is the answer for a frame with no tooltip on it, which the caller wants
    rather than a guess: the village showing through where the panel would be is
    not a number, and a resource without a ceiling is simply left out of the
    comparison.
    """
    image = open_frame(png)
    left, top, right, bottom = CAPACITY_BOX
    shift = CAPACITY_PITCH * row
    mask = ink_mask(
        image.crop((left, top + shift, right, bottom + shift)), saturation=STOCK_INK_SATURATION
    )
    # The last number on the line, because everything before it is the label:
    # 最大儲存量 and its colon are cut away by the tolerance, and anything they
    # leave behind lands to the left of the capacity itself.
    found = split_numbers(mask, CAPACITY_TOLERANCE)
    return found[-1] if found else None


def read_stock(png: bytes) -> VillageStock | None:
    """The village's own storages, or None when this screenshot is not showing them.

    Anything other than the home village reads as None, as does a home village
    with a panel over the bars, so a caller is meant to treat it as "not now"
    rather than as an empty village. Three rows all resolving into digits is
    itself the evidence that the home screen is up.
    """
    image = open_frame(png)
    gold, elixir, dark = (
        _read_row(image, (left, top, STOCK_RIGHT, bottom), STOCK_DIGIT_TOLERANCE)
        for left, (top, bottom) in zip(
            (STOCK_LEFT, STOCK_LEFT, STOCK_DARK_LEFT), STOCK_ROW_BOUNDS, strict=True
        )
    )
    if gold is None or elixir is None or dark is None:
        return None
    stock = VillageStock(gold=gold, elixir=elixir, dark=dark)
    logger.info("Village holds gold=%d elixir=%d dark=%d", stock.gold, stock.elixir, stock.dark)
    return stock
