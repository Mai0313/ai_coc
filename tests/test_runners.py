"""The loops under `ui/`, with the emulator and the parsers taken out.

Every runner here is built on a real `AdbController` with no device behind
it, and the taps, captures and parser answers are patched at the module the
runner reads them through. What is left is the runner's own decisions: which
tap follows which reading, and what each report says.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from ai_coc import plans
from ai_coc.ui import clan as clan_ui
from ai_coc.ui import hero as hero_ui
from ai_coc.ui import attack, upkeep
from ai_coc.ui import runner as shared
from ai_coc.models import (
    HeroCard,
    BattleRow,
    LootOffer,
    ScoutView,
    AttackPlan,
    AttackStep,
    BuildQueue,
    GameDialog,
    AdbEndpoint,
    ScreenPoint,
    BuildingName,
    VillageStock,
    DisplayTarget,
    GeminiSetting,
    UpgradeButton,
    BuildCandidate,
    LootThresholds,
    ResourceBubble,
    StorageCapacity,
)
from ai_coc.ui.clan import ClanRunner
from ai_coc.ui.hero import HeroRunner
from ai_coc.constants import COC_PACKAGE
from ai_coc.ui.attack import (
    CARD_ROW_Y,
    END_BATTLE,
    NIGHT_FIND,
    LINE_POINTS,
    DEPLOY_LINES,
    HOME_ATTEMPTS,
    SEARCH_CANCEL,
    DROPS_PER_PASS,
    AttackRunner,
    deploy_line,
)
from ai_coc.ui.runner import GameRunner
from ai_coc.ui.upkeep import UpkeepRunner
from ai_coc.adapters.ai import GeminiClient
from ai_coc.adapters.adb import AdbController, AdbControlError

DISPLAY = DisplayTarget(logical_id="1", physical_id="2")
STOCK = VillageStock(gold=9_000_000, elixir=9_000_000, dark=100_000)
THRESHOLDS = LootThresholds(min_gold=500_000)
POOR = ScoutView(loot=LootOffer(gold=70_000, elixir=50_000, dark=100), can_skip=True)
RICH = ScoutView(loot=LootOffer(gold=900_000, elixir=800_000, dark=9_000), can_skip=True)
FORCED = ScoutView(loot=POOR.loot, can_skip=False)


def _adb() -> AdbController:
    return AdbController(endpoint=AdbEndpoint(port=16384))


def _step(
    act: str, *points: tuple[float, float], who: str = "unknown", seconds: int = 0
) -> AttackStep:
    return AttackStep(
        act=act, who=who, at=[ScreenPoint(x_pct=x, y_pct=y) for x, y in points], seconds=seconds
    )


def _duke(price: int | None = 56_000) -> HeroCard:
    return HeroCard(
        hero="duke",
        point=(1407, 648),
        price=price,
        resource=None if price is None else "dark",
        upgradable=price is not None,
    )


class OpenedWalkTests(unittest.TestCase):
    """The one walk every tap on a raw village coordinate takes."""

    def _runner(self) -> GameRunner:
        return GameRunner(adb=_adb(), display=DISPLAY)

    def test_a_tap_that_covered_the_village_is_backed_out_of_and_skipped(self) -> None:
        runner = self._runner()
        with (
            patch.object(runner, "_after_tap", return_value=b"") as tapped,
            patch.object(shared, "read_stock", side_effect=[STOCK, None, STOCK]),
            patch.object(runner, "_home", return_value=STOCK) as home,
        ):
            opened = list(runner._opened([(1, 1), (2, 2), (3, 3)], "named"))
        assert [spot for spot, _ in opened] == [(1, 1), (3, 3)]
        home.assert_called_once()
        assert tapped.call_args_list[1].args[1] == "named_0002_0002"

    def test_a_village_that_cannot_be_got_back_ends_the_walk(self) -> None:
        runner = self._runner()
        with (
            patch.object(runner, "_after_tap", return_value=b""),
            patch.object(shared, "read_stock", side_effect=[STOCK, None, STOCK]),
            patch.object(runner, "_home", return_value=None),
        ):
            opened = list(runner._opened([(1, 1), (2, 2), (3, 3)], "named"))
        assert [spot for spot, _ in opened] == [(1, 1)]

    def test_a_stop_ends_the_walk_where_the_last_tap_was_already_backed_out_of(self) -> None:
        """The one place a sweep can be interrupted, and so every finder's.

        A tap costs a settle and a capture, so a full grid is a couple of
        minutes with nothing else to check a stop against — which is what
        `ai_coc upgrade` spends whenever it has no key to ask with. Leaving here
        is safe because the walk has already backed out of whatever the last tap
        opened.
        """
        runner = GameRunner(
            adb=_adb(), display=DISPLAY, should_stop=lambda: len(tapped.call_args_list) >= 2
        )
        with (
            patch.object(runner, "_after_tap", return_value=b"") as tapped,
            patch.object(shared, "read_stock", return_value=STOCK),
        ):
            opened = list(runner._opened([(1, 1), (2, 2), (3, 3), (4, 4)], "named"))
        assert [spot for spot, _ in opened] == [(1, 1), (2, 2)]
        assert tapped.call_count == 2

    def test_a_walk_nobody_stopped_taps_every_point_it_was_given(self) -> None:
        runner = self._runner()
        with (
            patch.object(runner, "_after_tap", return_value=b"") as tapped,
            patch.object(shared, "read_stock", return_value=STOCK),
        ):
            opened = list(runner._opened([(1, 1), (2, 2), (3, 3)], "named"))
        assert [spot for spot, _ in opened] == [(1, 1), (2, 2), (3, 3)]
        assert tapped.call_count == 3

    def test_the_sweep_is_that_walk_over_the_grid_with_the_staggered_points_held_to_the_limit(
        self,
    ) -> None:
        runner = self._runner()
        walked: list[list[tuple[int, int]]] = []

        def opened(points: object, label: str) -> object:
            walked.append(list(points))
            return iter(())

        with patch.object(runner, "_opened", side_effect=opened):
            list(runner._sweep("scan"))
            list(runner._sweep("hall", shared.SWEEP_STAGGER))
        plain, staggered = walked
        assert len(plain) == len(shared.SWEEP_X) * len(shared.SWEEP_Y)
        assert all(x <= shared.SWEEP_LIMIT[0] and y <= shared.SWEEP_LIMIT[1] for x, y in staggered)
        assert len(staggered) < len(plain)

    def test_a_dialog_over_the_village_is_answered_with_cancel(self) -> None:
        runner = self._runner()
        dialog = GameDialog(confirm=(973, 562), cancel=(623, 572))
        with (
            patch.object(shared.time, "sleep"),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(shared, "idle_disconnected", return_value=False),
            patch.object(shared, "loading_screen", return_value=False),
            # A frame for the dialog, one for the camera being put back, and one
            # for the village that then reads.
            patch.object(shared, "game_dialog", side_effect=[dialog, None, None]),
            patch.object(shared, "current_world", return_value="day"),
            patch.object(shared, "read_stock", return_value=STOCK),
            patch.object(GameRunner, "_settle_zoom"),
            patch.object(runner, "_tap") as tapped,
        ):
            assert runner._home() == STOCK
        tapped.assert_called_once_with(dialog.cancel)

    def test_the_first_village_a_loop_sees_settles_the_scale_and_the_position(self) -> None:
        """Two separate things, and the second one was missing.

        The far zoom was documented as centring the village as well, so every
        loop below this line aimed its taps at a map whose position nothing had
        actually settled — measured, a pinch at a camera shoved off centre moves
        the view by (0, 0). Parking runs it into a corner where it clamps, which
        three runs from three scattered starts agreed on to the pixel.
        """
        runner = self._runner()
        with (
            patch.object(shared.time, "sleep"),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(shared, "idle_disconnected", return_value=False),
            patch.object(shared, "loading_screen", return_value=False),
            patch.object(shared, "game_dialog", return_value=None),
            patch.object(shared, "current_world", return_value="day"),
            patch.object(shared, "read_stock", return_value=STOCK),
            patch.object(AdbController, "zoom") as zoomed,
            patch.object(shared, "park_camera") as parked,
        ):
            assert runner._home() == STOCK
            # Once per run, hung off the first frame the plate row names as a
            # village — before the storages are read rather than after, since a
            # camera bad enough to hide them is exactly the one this fixes. A
            # second call is the same loop still going rather than a new one.
            assert runner._home() == STOCK
        zoomed.assert_called_once()
        # Always the home village: `_home` sails off the builder base long
        # before it reaches the read that calls this.
        parked.assert_called_once_with(runner.adb, runner.display, "day")

    def test_restarting_the_game_resolves_the_display_again(self) -> None:
        """MuMu opens the game on a display of its own choosing."""
        adb = MagicMock()
        fresh = DisplayTarget(logical_id="3", physical_id="4")
        adb.display_for.return_value = fresh
        with patch.object(shared.time, "sleep"):
            assert shared.restart_game(adb, DISPLAY) == fresh
            adb.display_for.side_effect = AdbControlError("not yet")
            assert shared.restart_game(adb, DISPLAY) == DISPLAY
        adb.stop_app.assert_called_with(COC_PACKAGE)
        adb.launch_app.assert_called_with(COC_PACKAGE)


class ClanRunnerTests(unittest.TestCase):
    def _runner(self, **fields: object) -> ClanRunner:
        return ClanRunner(adb=_adb(), display=DISPLAY, **fields)

    def _donate(
        self,
        runner: ClanRunner,
        button: tuple[int, int] | None,
        panel: bool = True,
        cards: list[list[tuple[int, int]]] | None = None,
        moved: int = 5000,
    ) -> tuple[object, MagicMock]:
        with (
            patch.object(clan_ui.time, "sleep"),
            patch.object(runner, "_home", return_value=STOCK),
            patch.object(runner, "_after_tap", return_value=b"panel"),
            patch.object(runner, "_frame", return_value=b"after"),
            patch.object(runner, "_tap"),
            patch.object(clan_ui, "reinforce_button", return_value=button),
            patch.object(clan_ui, "donation_panel", return_value=panel),
            patch.object(clan_ui, "donatable_cards", side_effect=cards or [[]]),
            patch.object(clan_ui, "panel_difference", return_value=moved),
            patch.object(AdbController, "back") as back,
        ):
            return runner.donate(), back

    def test_nobody_asking_costs_a_look_and_the_way_back_out(self) -> None:
        report, back = self._donate(self._runner(), button=None)
        assert report.message == "部落聊天裡目前沒有人在請求增援"
        assert report.gifts == []
        # The chat tab was opened, so it is closed again.
        assert back.call_count == 2

    def test_a_panel_that_did_not_open_gives_nothing(self) -> None:
        report, _ = self._donate(self._runner(), button=(477, 440), panel=False)
        assert "沒有打開" in report.message

    def test_a_dry_run_counts_what_it_could_give_and_gives_none_of_it(self) -> None:
        report, _ = self._donate(
            self._runner(dry_run=True), button=(477, 440), cards=[[(700, 300), (814, 300)]]
        )
        assert (report.offered, report.gifts) == (2, [])
        assert "沒有捐出去" in report.message

    def test_gifts_are_given_leftmost_first_until_the_cards_run_out(self) -> None:
        report, _ = self._donate(
            self._runner(), button=(477, 440), cards=[[(700, 300), (814, 300)], [(814, 300)], []]
        )
        assert report.gifts == [(700, 300), (814, 300)]
        assert report.message == "捐了 2 次"

    def test_a_tap_the_panel_did_not_answer_ends_the_giving(self) -> None:
        """The cards themselves cannot say a gift landed, so the panel repainting is the evidence."""
        report, _ = self._donate(
            self._runner(), button=(477, 440), cards=[[(700, 300)], [(700, 300)]], moved=0
        )
        assert report.gifts == []

    def test_the_round_limit_holds(self) -> None:
        report, _ = self._donate(
            self._runner(rounds=1), button=(477, 440), cards=[[(700, 300)], [(700, 300)]]
        )
        assert report.gifts == [(700, 300)]

    def test_a_village_that_cannot_be_reached_donates_nothing(self) -> None:
        runner = self._runner()
        with patch.object(runner, "_home", return_value=None):
            report = runner.donate()
        assert "沒辦法回到村莊" in report.message


class CollectTests(unittest.TestCase):
    def _runner(self) -> UpkeepRunner:
        return UpkeepRunner(adb=_adb(), display=DISPLAY)

    def _collect(
        self, homes: list[VillageStock | None], markers: list[list[ResourceBubble]]
    ) -> tuple[object, MagicMock]:
        runner = self._runner()
        with (
            patch.object(upkeep.time, "sleep"),
            patch.object(runner, "_home", side_effect=homes),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(upkeep, "collect_bubbles", side_effect=markers),
            patch.object(AdbController, "tap_many") as tapped,
        ):
            return runner.collect(), tapped

    def test_every_marker_is_tapped_in_one_burst_and_the_gain_comes_off_the_bars(self) -> None:
        coin = ResourceBubble(resource="gold", point=(1000, 300))
        drop = ResourceBubble(resource="elixir", point=(1100, 320))
        before = VillageStock(gold=100, elixir=100, dark=100)
        after = VillageStock(gold=150, elixir=130, dark=100)
        report, tapped = self._collect([before, after, after], [[coin, drop], []])
        assert tapped.call_args_list[0].args[0] == [(1000, 300), (1100, 320)]
        assert (report.markers, report.gold, report.elixir, report.dark) == (2, 50, 30, 0)
        assert "收了 2 個採集器" in report.message

    def test_markers_that_are_still_standing_end_the_passes(self) -> None:
        """A full storage takes none of what it is handed, so the marker never leaves."""
        coin = ResourceBubble(resource="gold", point=(1000, 300))
        report, tapped = self._collect([STOCK, STOCK, STOCK], [[coin], [coin]])
        assert tapped.call_count == 1
        assert report.markers == 1

    def test_nothing_waiting_is_said_rather_than_reported_as_a_haul(self) -> None:
        report, tapped = self._collect([STOCK, STOCK], [[]])
        tapped.assert_not_called()
        assert report.message == "沒有採集器等著收"

    def test_a_village_lost_after_the_taps_still_reports_them(self) -> None:
        """Measured, a run walked into the idle-disconnect dialog between two passes."""
        coin = ResourceBubble(resource="gold", point=(1000, 300))
        # Read once before, once at the top of the second pass, once after.
        report, _ = self._collect([STOCK, None, None], [[coin]])
        assert report.markers == 1
        assert "讀不到儲量" in report.message


class BuildersTests(unittest.TestCase):
    def _runner(self) -> UpkeepRunner:
        return UpkeepRunner(adb=_adb(), display=DISPLAY)

    def _builders(
        self, counted: tuple[int, int] | None, panels: list[BuildQueue | None]
    ) -> tuple[object, MagicMock, MagicMock]:
        runner = self._runner()
        with (
            patch.object(upkeep.time, "sleep"),
            patch.object(runner, "_home", return_value=STOCK),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(runner, "_after_tap", return_value=b"") as opened,
            patch.object(runner, "_tap") as tapped,
            patch.object(upkeep, "free_builders", return_value=counted),
            patch.object(upkeep, "builder_jobs", side_effect=panels),
        ):
            return runner.builders(), opened, tapped

    def test_the_panel_is_read_and_shut_again_behind_the_run(self) -> None:
        queue = BuildQueue(running=2, remaining=[600, 7200])
        report, opened, tapped = self._builders((1, 5), [queue])
        assert (report.free, report.total, report.queue) == (1, 5, queue)
        assert "最快的還要 10 分鐘" in report.message
        opened.assert_called_once()
        tapped.assert_called_once_with(upkeep.BUILDER_BUTTON)

    def test_a_button_that_toggles_is_worth_a_second_tap(self) -> None:
        """A panel left open by an earlier run closes on the first tap."""
        queue = BuildQueue(running=1, remaining=[90_000])
        report, opened, _ = self._builders((0, 5), [None, queue])
        assert opened.call_count == 2
        assert "1 天 1 小時" in report.message

    def test_a_panel_that_will_not_open_and_a_counter_that_will_not_read_both_say_so(self) -> None:
        report, _, _ = self._builders((0, 5), [None, None])
        assert "打不開" in report.message
        report, _, _ = self._builders(None, [])
        assert "讀不到工人數量" in report.message

    def test_no_upgrade_running_is_its_own_message(self) -> None:
        report, _, _ = self._builders((5, 5), [BuildQueue()])
        assert report.message == "工人 5/5，沒有在跑的升級"

    def test_a_countdown_is_spelt_the_way_the_game_writes_it(self) -> None:
        assert upkeep._spell_out(90_000) == "1 天 1 小時"
        assert upkeep._spell_out(3_660) == "1 小時 1 分鐘"
        assert upkeep._spell_out(120) == "2 分鐘"


class UpgradeTests(unittest.TestCase):
    def _runner(self, **fields: object) -> UpkeepRunner:
        return UpkeepRunner(adb=_adb(), display=DISPLAY, **fields)

    def _offer(self, point: tuple[int, int] = (500, 300), price: int = 720_000) -> BuildCandidate:
        return BuildCandidate(
            point=point, resource="gold", price=price, building=BuildingName(name="金礦", level=12)
        )

    def test_every_free_builder_is_put_on_the_dearest_affordable_upgrade(self) -> None:
        runner = self._runner()
        cheap, dear = self._offer((100, 100), 100_000), self._offer((200, 200), 900_000)
        with (
            patch.object(runner, "_home", return_value=STOCK),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(upkeep, "free_builders", return_value=(2, 5)),
            patch.object(runner, "_buildings", return_value=[cheap, dear]),
            patch.object(runner, "_start", return_value=True) as started,
        ):
            report = runner.upgrade()
        assert [call.args[0] for call in started.call_args_list] == [dear, cheap]
        assert report.started == [dear, cheap]
        assert report.message == "開始了 2 個升級：金礦(12級)、金礦(12級)"

    def test_a_refused_upgrade_is_not_asked_about_again(self) -> None:
        runner = self._runner()
        offer = self._offer()
        with (
            patch.object(runner, "_home", return_value=STOCK),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(upkeep, "free_builders", return_value=(3, 5)),
            patch.object(runner, "_buildings", return_value=[offer]),
            patch.object(runner, "_start", return_value=False) as started,
        ):
            report = runner.upgrade()
        assert started.call_count == 1
        assert report.started == []

    def test_nothing_affordable_names_the_cheapest_thing_going(self) -> None:
        runner = self._runner()
        with (
            patch.object(runner, "_home", return_value=VillageStock(gold=1, elixir=1, dark=1)),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(upkeep, "free_builders", return_value=(1, 5)),
            patch.object(runner, "_buildings", return_value=[self._offer(price=720_000)]),
        ):
            report = runner.upgrade()
        assert "最便宜的要 720000" in report.message

    def test_no_free_builder_ends_the_run_before_anything_is_looked_for(self) -> None:
        runner = self._runner()
        with (
            patch.object(runner, "_home", return_value=STOCK),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(upkeep, "free_builders", return_value=(0, 5)),
            patch.object(runner, "_buildings") as looked,
        ):
            report = runner.upgrade()
        looked.assert_not_called()
        assert "都在忙" in report.message

    def _start(
        self,
        offered: list[UpgradeButton],
        dialog: GameDialog | None,
        sheet: tuple[int, int] | None,
        paid: VillageStock | None,
    ) -> tuple[bool, MagicMock]:
        runner = self._runner()
        with (
            patch.object(upkeep.time, "sleep"),
            patch.object(runner, "_after_tap", return_value=b""),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(runner, "_tap") as tapped,
            patch.object(upkeep, "upgrade_buttons", return_value=offered),
            patch.object(upkeep, "game_dialog", return_value=dialog),
            patch.object(upkeep, "upgrade_sheet", return_value=sheet),
            patch.object(runner, "_home", return_value=paid),
        ):
            return runner._start(
                self._offer(price=100), VillageStock(gold=1000, elixir=0, dark=0)
            ), tapped

    def test_a_started_upgrade_is_one_the_storage_paid_for(self) -> None:
        button = UpgradeButton(resource="gold", point=(700, 700), price=100)
        started, tapped = self._start(
            [button], None, (1121, 783), VillageStock(gold=900, elixir=0, dark=0)
        )
        assert started
        assert [call.args[0] for call in tapped.call_args_list] == [(700, 700), (1121, 783)]

    def test_a_walls_dialog_is_confirmed_where_a_buildings_sheet_is_not_up(self) -> None:
        button = UpgradeButton(resource="gold", point=(700, 700), price=100)
        dialog = GameDialog(confirm=(973, 562), cancel=(623, 572))
        started, tapped = self._start(
            [button], dialog, None, VillageStock(gold=900, elixir=0, dark=0)
        )
        assert started
        assert tapped.call_args_list[1].args[0] == dialog.confirm

    def test_a_storage_that_never_moved_is_not_an_upgrade(self) -> None:
        button = UpgradeButton(resource="gold", point=(700, 700), price=100)
        started, _ = self._start(
            [button], None, (1121, 783), VillageStock(gold=1000, elixir=0, dark=0)
        )
        assert not started

    def test_a_menu_that_no_longer_offers_the_price_is_left_alone(self) -> None:
        started, tapped = self._start([], None, None, STOCK)
        assert not started
        tapped.assert_not_called()

    def test_an_offer_is_the_menus_first_upgrade_with_the_name_the_model_read(self) -> None:
        runner = self._runner()
        button = UpgradeButton(resource="elixir", point=(700, 700), price=60_000)
        with (
            patch.object(upkeep, "wall_menu", return_value=None),
            patch.object(upkeep, "upgrade_buttons", return_value=[button]),
            patch.object(runner, "_name", return_value=BuildingName(name="聖水收集器", level=8)),
        ):
            offer = runner._offer((500, 300), b"")
        assert offer == BuildCandidate(
            point=(500, 300),
            resource="elixir",
            price=60_000,
            building=BuildingName(name="聖水收集器", level=8),
        )

    def test_a_wall_or_a_menu_with_no_upgrade_is_no_offer(self) -> None:
        runner = self._runner()
        with patch.object(upkeep, "wall_menu", return_value=MagicMock()):
            assert runner._offer((1, 1), b"") is None
        with (
            patch.object(upkeep, "wall_menu", return_value=None),
            patch.object(upkeep, "upgrade_buttons", return_value=[]),
        ):
            assert runner._offer((1, 1), b"") is None

    def test_the_name_is_read_off_the_strip_by_the_lite_tier_and_dropped_on_a_failure(
        self,
    ) -> None:
        runner = self._runner(
            namer=GeminiClient(api_key="not-a-real-key", settings=GeminiSetting())
        )
        with (
            patch.object(upkeep, "name_strip", return_value=b"strip"),
            patch.object(
                GeminiClient,
                "generate_structured",
                return_value=BuildingName(name="金礦", level=12),
            ) as asked,
        ):
            assert runner._name(b"frame") == BuildingName(name="金礦", level=12)
            assert asked.call_args.args[2] == b"strip"
            asked.side_effect = RuntimeError("timeout")
            assert runner._name(b"frame") == BuildingName()

    def test_the_sweep_prices_whatever_opens_a_menu(self) -> None:
        runner = self._runner()
        offer = self._offer()
        with (
            patch.object(
                runner, "_sweep", return_value=iter([((500, 300), b""), ((660, 300), b"")])
            ),
            patch.object(runner, "_offer", side_effect=[offer, None]),
        ):
            assert runner._scan() == [offer]


class HeroRunnerTests(unittest.TestCase):
    def _runner(self, **fields: object) -> HeroRunner:
        return HeroRunner(adb=_adb(), display=DISPLAY, **fields)

    def test_pages_scroll_until_the_arrow_is_gone_and_stop_when_the_hall_is(self) -> None:
        runner = self._runner()
        king = HeroCard(hero="king", point=(300, 648))
        with (
            patch.object(hero_ui.time, "sleep"),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(hero_ui, "hero_cards", side_effect=[[king], [_duke()], []]),
            patch.object(hero_ui, "can_scroll", side_effect=[True, True, True]),
            patch.object(runner, "_tap") as tapped,
        ):
            pages = list(runner._pages(hero_ui.SCROLL_RIGHT))
        assert pages == [[king], [_duke()]]
        assert tapped.call_count == 2
        with (
            patch.object(runner, "_frame", return_value=b""),
            patch.object(hero_ui, "hero_cards", return_value=[king]),
            patch.object(hero_ui, "can_scroll", return_value=False),
            patch.object(runner, "_tap") as tapped,
        ):
            assert list(runner._pages(hero_ui.SCROLL_RIGHT)) == [[king]]
        tapped.assert_not_called()

    def test_the_walk_keeps_a_priced_sighting_over_an_unpriced_one(self) -> None:
        runner = self._runner()
        with patch.object(runner, "_pages", side_effect=[[[_duke(None)]], [[_duke()]]]):
            found = runner._walk()
        assert found == {"duke": _duke()}

    def test_bringing_a_hero_on_scrolls_until_its_card_is_in_front(self) -> None:
        runner = self._runner()
        king = HeroCard(hero="king", point=(300, 648))
        with patch.object(runner, "_pages", side_effect=[[[king]], [[king], [_duke()]]]):
            assert runner._bring_on("duke") == _duke()
        with patch.object(runner, "_pages", return_value=[[king]]):
            assert runner._bring_on("duke") is None

    def test_a_button_is_the_hall_only_if_cards_come_up_behind_it(self) -> None:
        runner = self._runner()
        with (
            patch.object(hero_ui, "hall_buttons", return_value=[(1151, 700), (1327, 700)]),
            patch.object(runner, "_after_tap", side_effect=[b"barracks", b"hall"]),
            patch.object(hero_ui, "hero_cards", side_effect=[[], [_duke()]]),
            patch.object(runner, "_home", return_value=STOCK) as home,
        ):
            assert runner._try_menu(b"menu") == b"hall"
        home.assert_called_once()

    def test_the_hall_is_closed_only_while_it_is_the_thing_on_screen(self) -> None:
        runner = self._runner()
        with (
            patch.object(hero_ui.time, "sleep"),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(hero_ui, "hero_cards", side_effect=[[_duke()], []]),
            patch.object(AdbController, "back") as back,
        ):
            runner._close()
            runner._close()
        assert back.call_count == 1

    def test_a_remembered_spot_is_tried_more_than_once_because_buildings_overlap(self) -> None:
        runner = self._runner(at=(990, 430))
        with (
            patch.object(runner, "_after_tap", return_value=b"menu"),
            patch.object(runner, "_try_menu", side_effect=[None, b"hall"]) as tried,
        ):
            assert runner._open() == b"hall"
        assert tried.call_count == 2

    def test_starting_needs_the_sheet_and_not_the_cards(self) -> None:
        runner = self._runner()
        with (
            patch.object(hero_ui.time, "sleep"),
            patch.object(runner, "_tap") as tapped,
            patch.object(runner, "_frame", return_value=b""),
            patch.object(hero_ui, "hero_cards", side_effect=[[], [_duke()], []]),
            patch.object(hero_ui, "upgrade_sheet", side_effect=[(1121, 783), None, None]),
        ):
            assert runner._start(_duke())
            assert not runner._start(_duke())
            assert not runner._start(_duke())
        assert tapped.call_args_list[1].args[0] == (1121, 783)

    def test_a_purchase_is_judged_on_the_dark_elixir_really_moving(self) -> None:
        runner = self._runner(hero="duke")
        before = VillageStock(gold=0, elixir=0, dark=200_000)
        with (
            patch.object(runner, "_bring_on", return_value=_duke()),
            patch.object(runner, "_start", return_value=True),
            patch.object(runner, "_close"),
            patch.object(
                runner, "_home", return_value=VillageStock(gold=0, elixir=0, dark=144_000)
            ),
        ):
            started, message = runner._purchase(_duke(), before)
        assert started == _duke()
        assert "開始升級" in message

    def test_a_read_only_run_reports_the_hall_and_touches_nothing(self) -> None:
        runner = self._runner()
        with (
            patch.object(runner, "_home", return_value=STOCK),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(hero_ui, "free_builders", return_value=(1, 5)),
            patch.object(runner, "_open", return_value=b""),
            patch.object(runner, "_walk", return_value={"duke": _duke()}),
            patch.object(runner, "_close") as closed,
        ):
            report = runner.run()
        assert report.cards == [_duke()]
        assert report.started is None
        assert report.message == "英雄殿堂裡讀到 1 個英雄"
        closed.assert_called_once()

    def test_a_hall_nobody_could_find_says_so(self) -> None:
        runner = self._runner()
        with (
            patch.object(runner, "_home", return_value=STOCK),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(hero_ui, "free_builders", return_value=(1, 5)),
            patch.object(runner, "_open", return_value=None),
        ):
            report = runner.run()
        assert report.message == "掃過村莊都沒有找到英雄殿堂"


class OpenAttackMenuTests(unittest.TestCase):
    """One method for both villages, differing only in which way it sails and which menu it trusts."""

    def _open(
        self, world: str, worlds: list[str | None], crossed: str | None = None, **screens: object
    ) -> tuple[bytes | None, dict[str, MagicMock]]:
        """Open the menu against canned readings.

        `screens` is what each frame reads as beyond the village: `day_menu` and
        `night_menu` for the two menu readers, `idle` and `result` for the dialog
        and the result screen, each a list consumed a frame at a time.
        """
        runner = AttackRunner(
            adb=_adb(), display=DISPLAY, world=world, thresholds=LootThresholds()
        )
        with (
            patch.object(attack.time, "sleep"),
            patch.object(runner, "_frame", return_value=b"home"),
            # The loading wait captures straight off the emulator rather than
            # through `_frame`, so a recorded run is not left holding hundreds
            # of copies of the same bar.
            patch.object(AdbController, "screenshot", return_value=b"home"),
            patch.object(
                attack, "idle_disconnected", side_effect=screens.get("idle") or [False] * 20
            ),
            patch.object(
                attack, "loading_screen", side_effect=screens.get("loading") or [False] * 20
            ),
            patch.object(attack, "battle_over", side_effect=screens.get("result") or [False] * 20),
            patch.object(attack, "current_world", side_effect=worlds),
            patch.object(attack, "cross", return_value=crossed) as sailed,
            patch.object(attack, "uncovered") as cleared,
            patch.object(attack, "restart_game", return_value=DISPLAY) as restarted,
            patch.object(runner, "_settle_ceilings") as ceilings,
            patch.object(runner, "_leave_result") as left,
            patch.object(runner, "_tap") as tapped,
            patch.object(attack, "attack_menu_open", return_value=screens.get("day_menu", True)),
            patch.object(
                attack, "night_attack_menu", return_value=screens.get("night_menu", True)
            ),
        ):
            got = runner._open_attack_menu()
        return got, {
            "sailed": sailed,
            "cleared": cleared,
            "restarted": restarted,
            "ceilings": ceilings,
            "left": left,
            "tapped": tapped,
            "runner": runner,
        }

    def test_the_ordinary_case_reads_the_ceilings_once_and_taps_attack(self) -> None:
        got, seen = self._open("day", ["day"])
        assert got == b"home"
        seen["ceilings"].assert_called_once()
        seen["tapped"].assert_called_once_with(attack.HOME_ATTACK)
        seen["sailed"].assert_not_called()

    def test_the_other_village_is_sailed_from_first(self) -> None:
        got, seen = self._open("day", ["night", "day"], crossed="day")
        assert got == b"home"
        assert seen["sailed"].call_args.args[2] == "day"
        got, seen = self._open("night", ["day", "night"], crossed="night", day_menu=False)
        assert got == b"home"
        assert seen["sailed"].call_args.args[2] == "night"

    def test_a_crossing_that_never_lands_ends_the_round(self) -> None:
        got, seen = self._open("day", ["night"], crossed="night")
        assert got is None
        seen["tapped"].assert_not_called()

    def test_a_popup_is_pressed_away_and_a_dropped_session_restarted(self) -> None:
        got, seen = self._open("day", [None, "day"])
        assert got == b"home"
        seen["cleared"].assert_called_once()
        got, seen = self._open("day", ["day"], idle=[True, False])
        assert got == b"home"
        seen["restarted"].assert_called_once()

    def test_a_result_screen_left_up_is_left_first(self) -> None:
        got, seen = self._open("day", ["day"], result=[True, False])
        assert got == b"home"
        seen["left"].assert_called_once()

    def test_a_menu_that_never_opens_gives_up_after_its_attempts(self) -> None:
        got, seen = self._open("day", ["day"] * HOME_ATTEMPTS, day_menu=False)
        assert got is None
        assert seen["tapped"].call_count == HOME_ATTEMPTS

    def test_a_loading_screen_is_waited_out_and_the_round_then_goes_on(self) -> None:
        """The wait reads the screen itself, so the third answer is the attempt after it."""
        got, seen = self._open("day", ["day"], loading=[True, False, False])
        assert got == b"home"
        assert seen["runner"]._stuck == ""
        seen["cleared"].assert_not_called()
        seen["restarted"].assert_not_called()

    def test_a_loading_screen_that_comes_back_ends_the_round_naming_the_server(self) -> None:
        """One wait per load: a game that loaded and dropped back is a server not staying up."""
        got, seen = self._open("day", [], loading=[True, False, True])
        assert got is None
        assert "又回到載入畫面" in seen["runner"]._stuck
        seen["tapped"].assert_not_called()

    def test_a_restart_boots_through_the_loading_screen_and_that_is_a_fresh_wait(self) -> None:
        """A wait that ended on the dropped-session dialog is answered with a restart.

        `restart_game` returns a couple of seconds before the village paints,
        so the frame after it is 正在載入 again — a new load, not a loaded game
        dropped back, and refusing it there ended the round with a message
        about a server that had in fact just come back.
        """
        got, seen = self._open(
            "day",
            ["day"],
            idle=[False, True, False, False],
            loading=[True, False, True, False, False],
        )
        assert got == b"home"
        assert seen["runner"]._stuck == ""
        seen["restarted"].assert_called_once()


class WaitOutLoadingTests(unittest.TestCase):
    """Sitting on 正在載入 without tapping, restarting or pressing at it."""

    def _wait(
        self, reads: list[bool], stop: bool = False, again: bool = False
    ) -> tuple[bool, AttackRunner, MagicMock]:
        runner = AttackRunner(
            adb=_adb(),
            display=DISPLAY,
            world="day",
            thresholds=LootThresholds(),
            should_stop=lambda: stop,
        )
        with (
            patch.object(attack.time, "sleep") as slept,
            patch.object(attack, "loading_screen", side_effect=reads),
            patch.object(AdbController, "screenshot", return_value=b"") as shot,
            patch.object(AdbController, "tap"),
            patch.object(AdbController, "back") as back,
        ):
            went = runner._wait_out_loading(again)
        assert back.call_count == 0
        shot.slept = slept
        return went, runner, shot

    def test_the_wait_ends_when_the_screen_changes(self) -> None:
        went, runner, shot = self._wait([True, True, False])
        assert went
        assert runner._stuck == ""
        assert shot.call_count == 3
        assert shot.slept.call_count == 3

    def test_patience_running_out_names_the_server(self) -> None:
        with patch.object(attack, "SERVER_POLLS", 3):
            went, runner, shot = self._wait([True] * 3)
        assert not went
        assert "載入畫面" in runner._stuck
        assert shot.call_count == 3

    def test_a_stop_ends_the_wait_and_says_so_rather_than_blaming_the_server(self) -> None:
        went, runner, shot = self._wait([], stop=True)
        assert not went
        assert "停止" in runner._stuck
        assert shot.call_count == 0

    def test_a_second_wait_in_one_round_is_refused_without_a_capture(self) -> None:
        went, runner, shot = self._wait([], again=True)
        assert not went
        assert "又回到載入畫面" in runner._stuck
        assert shot.call_count == 0

    def test_the_reason_reaches_the_report_on_both_villages(self) -> None:
        for world, fallback in (("day", "畫面不在主村"), ("night", "畫面不在建築大師基地")):
            runner = AttackRunner(
                adb=_adb(), display=DISPLAY, world=world, thresholds=LootThresholds()
            )
            with patch.object(AttackRunner, "_open_attack_menu", return_value=None):
                assert fallback in runner.run().message
                runner._stuck = "遊戲卡在載入畫面 45 分鐘"
                assert runner.run().message == "遊戲卡在載入畫面 45 分鐘"


class StoodDownTests(unittest.TestCase):
    def _runner(self, world: str) -> AttackRunner:
        runner = AttackRunner(
            adb=_adb(), display=DISPLAY, world=world, thresholds=LootThresholds(), stop_at=90
        )
        runner._capacity = StorageCapacity(
            gold=100, elixir=100, dark=100 if world == "day" else None
        )
        return runner

    def test_a_full_home_village_stands_the_run_down_naming_all_three(self) -> None:
        runner = self._runner("day")
        with (
            patch.object(
                attack, "read_stock", return_value=VillageStock(gold=95, elixir=95, dark=95)
            ),
            patch.object(AdbController, "back") as back,
        ):
            report = runner._stood_down(b"")
        assert report is not None
        assert (report.world, report.stock_full) == ("day", True)
        assert "黑水 95" in report.message
        back.assert_called_once()

    def test_a_full_builder_base_reads_its_own_two_rows(self) -> None:
        runner = self._runner("night")
        with (
            patch.object(
                attack, "read_builder_stock", return_value=VillageStock(gold=95, elixir=95, dark=0)
            ),
            patch.object(AdbController, "back"),
        ):
            report = runner._stood_down(b"")
        assert report is not None
        assert report.world == "night"
        assert "黑水" not in report.message

    def test_a_village_short_of_the_line_or_unreadable_carries_on(self) -> None:
        runner = self._runner("day")
        with patch.object(
            attack, "read_stock", return_value=VillageStock(gold=10, elixir=95, dark=95)
        ):
            assert runner._stood_down(b"") is None
        with patch.object(attack, "read_stock", return_value=None):
            assert runner._stood_down(b"") is None


class DayRoundTests(unittest.TestCase):
    """One home village round from the attack menu to the report."""

    def _round(
        self, views: list[ScoutView | None], *, took: bool = True, **settings: object
    ) -> tuple[object, list[tuple[int, int]], MagicMock, MagicMock]:
        """Play one round over canned scout readings.

        `settings` covers what the round is judged against: `strength` for the
        army screen, `max_skips` and `stop` for the runner, all defaulting to a
        full army that never stops with the 500k gold threshold.
        """
        runner = AttackRunner(
            adb=_adb(),
            display=DISPLAY,
            thresholds=THRESHOLDS,
            max_skips=int(settings.get("max_skips", 20)),
            should_stop=lambda: bool(settings.get("stop", False)),
        )
        taps: list[tuple[int, int]] = []
        with (
            patch.object(attack.time, "sleep"),
            patch.object(runner, "_open_attack_menu", return_value=b"home"),
            patch.object(runner, "_stood_down", return_value=None),
            patch.object(runner, "_tap", side_effect=taps.append),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(
                attack, "army_strength", return_value=settings.get("strength", (305, 305))
            ),
            patch.object(
                runner,
                "_scout",
                side_effect=[(view, b"scout") if view else None for view in views],
            ),
            patch.object(runner, "_deploy") as deployed,
            patch.object(runner, "_wait_out_battle", return_value=took),
            patch.object(AdbController, "back") as back,
        ):
            report = runner.run()
        return report, taps, deployed, back

    def test_a_poor_opponent_is_skipped_and_a_rich_one_attacked(self) -> None:
        report, taps, deployed, _ = self._round([POOR, RICH])
        assert report.attacked == RICH.loot
        assert report.skipped == 1
        assert report.message == "戰利品達標，已進攻並回營"
        deployed.assert_called_once_with(b"scout")
        assert taps == [attack.FIND_MATCH, attack.ARMY_ATTACK, attack.NEXT_TARGET]

    def test_a_half_trained_army_backs_out_before_the_fee(self) -> None:
        report, taps, deployed, back = self._round([RICH], strength=(100, 305))
        assert report.attacked is None
        assert "兵力只有 100/305" in report.message
        deployed.assert_not_called()
        back.assert_called_once()
        assert taps == [attack.FIND_MATCH]

    def test_too_many_skips_ends_the_search_through_the_button(self) -> None:
        report, taps, deployed, _ = self._round([POOR, POOR], max_skips=1)
        assert "連續跳過 1 個對手" in report.message
        assert taps[-1] == END_BATTLE
        deployed.assert_not_called()

    def test_a_stop_leaves_a_skippable_opponent_without_attacking(self) -> None:
        report, taps, deployed, _ = self._round([RICH], stop=True)
        assert report.message == "已停止，未開打就離開搜尋"
        assert taps[-1] == END_BATTLE
        deployed.assert_not_called()

    def test_a_forced_battle_is_played_whatever_the_loot_and_the_stop(self) -> None:
        report, _, deployed, _ = self._round([FORCED], stop=True)
        assert report.attacked == POOR.loot
        assert report.message == "倒數結束被強制開戰，已進攻並回營"
        deployed.assert_called_once()

    def test_a_battle_nobody_could_read_is_not_called_a_failure(self) -> None:
        report, _, _, _ = self._round([RICH], took=False)
        assert "無從判斷" in report.message

    def test_no_opponent_at_all_is_reported_and_left_through_the_button_only_if_one_is_up(
        self,
    ) -> None:
        report, taps, _, _ = self._round([None])
        assert "等不到對手畫面" in report.message
        assert END_BATTLE not in taps


class BattleWaitTests(unittest.TestCase):
    def _runner(self, **fields: object) -> AttackRunner:
        return AttackRunner(adb=_adb(), display=DISPLAY, thresholds=LootThresholds(), **fields)

    def test_the_countdown_ending_hands_back_the_first_battle_frame(self) -> None:
        runner = self._runner()
        with (
            patch.object(attack.time, "sleep"),
            patch.object(runner, "_frame", return_value=b"battle"),
            patch.object(attack, "read_scout", side_effect=[RICH, FORCED]),
        ):
            assert runner._wait_for_battle() == b"battle"

    def test_a_panel_that_stops_reading_ends_the_wait_without_a_frame(self) -> None:
        runner = self._runner()
        with (
            patch.object(attack.time, "sleep"),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(attack, "read_scout", return_value=None),
        ):
            assert runner._wait_for_battle() is None
        with (
            patch.object(attack.time, "sleep"),
            patch.object(attack, "COUNTDOWN_ATTEMPTS", 2),
            patch.object(runner, "_frame", return_value=b""),
            patch.object(attack, "read_scout", return_value=RICH),
        ):
            assert runner._wait_for_battle() is None

    def test_the_matchmaker_is_held_until_a_card_row_appears(self) -> None:
        runner = self._runner(world="night")
        with (
            patch.object(attack.time, "sleep"),
            patch.object(runner, "_tap") as tapped,
            patch.object(runner, "_frame", return_value=b"battle"),
            patch.object(attack, "searching_opponent", side_effect=[True, False]),
            patch.object(attack, "card_groups", return_value=[[164]]),
        ):
            assert runner._find_opponent() == b"battle"
        tapped.assert_called_once_with(NIGHT_FIND)

    def test_a_stop_cancels_the_search_and_a_search_that_drags_is_restarted(self) -> None:
        runner = self._runner(world="night", should_stop=lambda: True)
        with (
            patch.object(attack.time, "sleep"),
            patch.object(runner, "_tap") as tapped,
            patch.object(runner, "_frame", return_value=b""),
        ):
            assert runner._find_opponent() is None
        assert [call.args[0] for call in tapped.call_args_list] == [NIGHT_FIND, SEARCH_CANCEL]
        patient = self._runner(world="night")
        with (
            patch.object(attack.time, "sleep"),
            patch.object(attack, "SEARCH_PATIENCE", 0),
            patch.object(patient, "_tap") as tapped,
        ):
            assert patient._find_opponent() is None
        assert tapped.call_count == 2 * attack.SEARCH_ATTEMPTS

    def test_the_stage_after_a_result_is_a_village_or_another_card_row(self) -> None:
        runner = self._runner(world="night")
        with (
            patch.object(runner, "_leave_result"),
            patch.object(runner, "_frame", return_value=b"next"),
            patch.object(attack, "current_world", side_effect=["night", None, None]),
            patch.object(attack, "card_groups", side_effect=[[[164]], []]),
        ):
            assert runner._next_stage() is None
            assert runner._next_stage() == b"next"
            assert runner._next_stage() is None


class DeploymentTests(unittest.TestCase):
    def _runner(self) -> AttackRunner:
        return AttackRunner(adb=_adb(), display=DISPLAY, thresholds=LootThresholds())

    def _row(self, **kw: object) -> BattleRow:
        fields: dict[str, object] = {
            "troops": [100, 200],
            "machine": [400],
            "heroes": [500, 600],
            "rages": [900],
            "freezes": [1000],
            "rage_count": 2,
            "freeze_count": 3,
            "frame": b"",
        }
        fields.update(kw)
        return BattleRow(**fields)  # type: ignore[arg-type]

    def test_the_row_is_classified_off_the_opening_frame_and_the_plan_names_the_machine(
        self,
    ) -> None:
        runner = self._runner()
        siege = AttackPlan(steps=[_step("siege", (25, 27)), _step("troops", (37.5, 12), (14, 42))])
        rows: list[BattleRow] = []
        with (
            patch.object(runner, "_settle_zoom", side_effect=lambda frame: frame),
            patch.object(runner, "_settle_camera", side_effect=lambda frame: frame),
            patch.object(attack, "card_groups", return_value=[[171, 293], [557, 683], [1181]]),
            patch.object(attack, "counted_cards", return_value=[1181]),
            patch.object(attack, "freeze_cards", return_value=[]),
            patch.object(attack, "card_count", return_value=4),
            patch.object(runner, "_plan", return_value=siege),
            patch.object(runner, "_wait_for_battle", return_value=b"battle"),
            patch.object(runner, "_flank", return_value=DEPLOY_LINES["top_left"]),
            patch.object(
                runner, "_play_tactic", side_effect=lambda plan, anchors, row: rows.append(row)
            ),
        ):
            runner._deploy(b"opening")
        [row] = rows
        assert (row.troops, row.machine, row.heroes) == ([171, 293], [557], [683])
        assert (row.rages, row.freezes, row.rage_count) == ([1181], [], 4)

    def test_a_plan_with_no_siege_step_reads_every_one_off_card_as_a_hero(self) -> None:
        runner = self._runner()
        rows: list[BattleRow] = []
        with (
            patch.object(runner, "_settle_zoom", side_effect=lambda frame: frame),
            patch.object(runner, "_settle_camera", side_effect=lambda frame: frame),
            patch.object(attack, "card_groups", return_value=[[171], [557, 683]]),
            patch.object(attack, "counted_cards", return_value=[]),
            patch.object(attack, "freeze_cards", return_value=[]),
            patch.object(
                runner,
                "_plan",
                return_value=plans.flat().model_copy(
                    update={"steps": [_step("troops", (37.5, 12), (14, 42))]}
                ),
            ),
            patch.object(runner, "_wait_for_battle", return_value=None),
            patch.object(runner, "_flank", return_value=DEPLOY_LINES["top_left"]),
            patch.object(
                runner, "_play_tactic", side_effect=lambda plan, anchors, row: rows.append(row)
            ),
        ):
            runner._deploy(b"opening")
        assert (rows[0].machine, rows[0].heroes) == ([], [557, 683])

    def test_no_cards_on_the_row_means_nothing_to_deploy(self) -> None:
        runner = self._runner()
        with (
            patch.object(runner, "_settle_zoom", side_effect=lambda frame: frame),
            patch.object(runner, "_settle_camera", side_effect=lambda frame: frame),
            patch.object(attack, "card_groups", return_value=[]),
            patch.object(runner, "_play_tactic") as played,
        ):
            runner._deploy(b"")
        played.assert_not_called()

    def test_the_flank_is_the_preset_bent_onto_the_boundary_where_it_reads(self) -> None:
        runner = self._runner()
        bent = ((600, 120), (400, 260), (230, 390))
        with (
            patch.object(runner, "_clear_flank", side_effect=lambda frame, preset: frame),
            patch.object(attack, "fitted_line", return_value=bent),
        ):
            assert runner._flank(b"battle", None) == bent
        with (
            patch.object(runner, "_clear_flank", side_effect=lambda frame, preset: frame),
            patch.object(attack, "fitted_line", return_value=None),
        ):
            assert runner._flank(b"battle", None) == DEPLOY_LINES["top_left"]
        assert runner._flank(None, None) == DEPLOY_LINES["top_left"]

    def test_each_act_reaches_its_own_tap(self) -> None:
        runner = self._runner()
        line = deploy_line(LINE_POINTS, *DEPLOY_LINES["top_left"])
        row = self._row()
        with (
            patch.object(runner, "_drop_at") as dropped,
            patch.object(runner, "_pour") as poured,
            patch.object(runner, "_cast") as cast,
        ):
            runner._act(_step("siege", (25, 27)), row, line)
            runner._act(_step("troops", (37.5, 12), (14, 42)), row, line)
            runner._act(_step("rage", (30, 30), (45, 30), (30, 43)), row, line)
            runner._act(_step("freeze", (50, 46), (60, 46)), row, line)
        dropped.assert_called_once_with([400], (400, 243))
        poured.assert_called_once_with([100, 200], line, b"")
        # Two bottles' worth of rage against three points asked for, and a
        # freeze card holding three against the two it was given.
        assert len(cast.call_args_list[0].args[1]) == 2
        assert len(cast.call_args_list[1].args[1]) == 2

    def test_a_freeze_card_spends_its_bottles_on_separate_points(self) -> None:
        """One card holds three, and slicing to the card count stacked all three.

        `wanted` counted the freeze *cards* where rage counted its bottles, so a
        single card kept `targets[:1]` and `_cast`, which taps once per bottle,
        put the whole cargo on that one spot — one spell's worth of effect for
        three bottles. Measured over a day of recorded farming, `held 3, tapped
        4` on 65 rounds against a tactic line reading `freeze x3` on each.
        """
        runner = self._runner()
        line = deploy_line(LINE_POINTS, *DEPLOY_LINES["top_left"])
        row = self._row(freezes=[1000], freeze_count=3)
        with patch.object(runner, "_cast") as cast:
            runner._act(_step("freeze", (25, 25), (50, 30), (70, 55)), row, line)
        targets = cast.call_args.args[1]
        assert len(set(targets)) == 3

    def test_a_one_off_drop_is_one_shell_round_trip_and_is_written_down(self) -> None:
        runner = self._runner()
        with patch.object(AdbController, "tap_many") as tapped:
            runner._drop_at([400, 500], (600, 300))
            runner._drop_at([], (600, 300))
        tapped.assert_called_once()
        assert tapped.call_args.args[0] == [
            (400, CARD_ROW_Y),
            (600, 300),
            (500, CARD_ROW_Y),
            (600, 300),
        ]
        assert runner._sending == {400: (600, 300), 500: (600, 300)}

    def test_pouring_taps_a_counted_card_as_many_times_as_it_holds_and_the_rest_a_whole_pass(
        self,
    ) -> None:
        runner = self._runner()
        line = deploy_line(LINE_POINTS)
        with (
            patch.object(attack, "card_count", side_effect=[3, None]),
            patch.object(runner, "_tap"),
            patch.object(AdbController, "tap_many") as tapped,
        ):
            runner._pour([100, 200], line, b"")
        assert [len(call.args[0]) for call in tapped.call_args_list] == [4, DROPS_PER_PASS]

    def test_the_one_reading_sends_what_never_landed_again_and_spreads_what_is_still_held(
        self,
    ) -> None:
        runner = self._runner()
        line = deploy_line(LINE_POINTS)
        runner._sending = {500: (1, 1), 600: (2, 2)}
        with (
            patch.object(runner, "_frame", return_value=b""),
            patch.object(attack, "field_units", return_value=[500]),
            patch.object(attack, "live_cards", return_value=[100]),
            patch.object(runner, "_spread_troops") as spread,
            patch.object(runner, "_drop_singles", return_value=([600], [600])) as singles,
        ):
            runner._settle_drops([100, 200], line, DEPLOY_LINES["top_left"])
        assert runner._sending == {}
        assert runner._onfield == [500, 600]
        spread.assert_called_once_with([100], DEPLOY_LINES["top_left"], 0)
        assert singles.call_args.args[0] == [600]

    def test_an_outcome_tells_a_battle_nobody_read_from_one_that_never_landed(self) -> None:
        runner = self._runner()
        assert runner._outcome("戰利品達標", True) == "戰利品達標，已進攻並回營"
        assert "無從判斷" in runner._outcome("戰利品達標", False)
        runner._seen = POOR.loot
        assert "沒有變化" in runner._outcome("戰利品達標", False)


if __name__ == "__main__":
    unittest.main()
