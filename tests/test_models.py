"""The models' own logic: parsing what the outside world hands over, and the arithmetic they carry."""

from __future__ import annotations

import os
import json
import math
from pathlib import Path
from datetime import UTC, datetime, timedelta
import tempfile
import unittest
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from ai_coc import models
from ai_coc.models import (
    Frame,
    RunLog,
    MapEdge,
    MapFrame,
    AppConfig,
    MapSurvey,
    NightPlan,
    AttackPlan,
    AttackStep,
    PlayedPlan,
    WallReport,
    AdbEndpoint,
    BuildReport,
    ScreenPoint,
    WallUpgrade,
    BuildingName,
    EntityMapping,
    GeminiRequest,
    BuildCandidate,
    GeminiTextPart,
    GeminiImagePart,
    VillageDocument,
    EmulatorInstance,
    MuMuInstanceInfo,
    MuMuInstanceTable,
    GeminiResponseFormat,
    tolerant_int,
)


class EndpointTests(unittest.TestCase):
    def test_a_serial_splits_into_host_and_port(self) -> None:
        endpoint = AdbEndpoint.parse(" 127.0.0.1:16384 ")
        assert (endpoint.host, endpoint.port) == ("127.0.0.1", 16384)
        assert endpoint.serial == "127.0.0.1:16384"
        assert endpoint.ready

    def test_a_serial_with_no_port_is_not_ready(self) -> None:
        """MuMu reports port 0 until the instance's ADB bridge is listening."""
        endpoint = AdbEndpoint.parse("junk")
        assert (endpoint.host, endpoint.port) == ("127.0.0.1", 0)
        assert not endpoint.ready


class MuMuPayloadTests(unittest.TestCase):
    """What the CLI answers, which gains and loses fields between releases."""

    def test_junk_in_the_numeric_fields_reads_as_zero(self) -> None:
        info = MuMuInstanceInfo.model_validate({
            "index": 0,
            "adb_port": "not a port",
            "pid": None,
            "main_wnd": "1A",
            "render_wnd": "zz",
            "future": True,
        })
        assert (info.adb_port, info.pid) == (0, 0)
        assert not info.endpoint.ready
        assert (info.hwnd("main_wnd"), info.hwnd("render_wnd")) == (26, 0)
        assert info.model_extra == {"future": True}

    def test_the_table_keeps_only_the_rows_that_are_instances(self) -> None:
        table = MuMuInstanceTable.model_validate({
            "0": {"index": 0, "name": "A"},
            "1": {"index": 1},
            "errcode": 0,
            "note": {"no index": True},
        })
        assert [item.index for item in table.instances()] == [0, 1]
        assert table.instances()[0].name == "A"

    def test_an_instance_carries_its_endpoint(self) -> None:
        instance = EmulatorInstance(
            emulator_id="mumu:0",
            index=0,
            name="MuMu 0",
            android_version="12",
            adb_serial="127.0.0.1:16384",
            process_started=True,
            android_started=True,
            state="running",
            pid=1,
            main_hwnd=0,
            render_hwnd=0,
        )
        assert instance.endpoint == AdbEndpoint(port=16384)

    def test_tolerant_int_falls_back_rather_than_raising(self) -> None:
        assert tolerant_int("7") == 7
        assert tolerant_int(None) is None
        assert tolerant_int("x", 3) == 3


class VillageDocumentTests(unittest.TestCase):
    def test_a_section_that_is_not_a_list_reads_as_empty(self) -> None:
        document = VillageDocument.model_validate({"tag": "#T", "units": {"data": 1}})
        assert document.entries("units") == []
        assert document.entries("absent") == []

    def test_rows_without_a_data_id_are_skipped_rather_than_fatal(self) -> None:
        document = VillageDocument.model_validate({
            "buildings": [{"data": 1000055, "lvl": 3}, "not a row", {"lvl": 2}]
        })
        [entity] = document.entries("buildings")
        assert (entity.data_id, entity.level, entity.section) == (1000055, 3, "buildings")

    def test_a_blank_tag_reads_as_unknown(self) -> None:
        assert VillageDocument.model_validate({"tag": "  "}).tag == "UNKNOWN"
        assert VillageDocument.model_validate({}).tag == "UNKNOWN"


class EntityMappingTests(unittest.TestCase):
    def test_the_community_table_flattens_to_one_name_per_id(self) -> None:
        """The groups separate the two villages, which is not what a caller asks."""
        mapping = EntityMapping.model_validate({
            "th_buildings": {1000001: "Cannon", 28000000: "Barbarian King"},
            "bh_troops": {4000041: "Baby Dragon"},
            "heroes": {28000000: "Barbarian King"},
        })
        assert mapping.names() == {
            1000001: "Cannon",
            28000000: "Barbarian King",
            4000041: "Baby Dragon",
        }


class FrameTests(unittest.TestCase):
    def test_a_frame_id_names_the_emulator_and_the_sequence(self) -> None:
        frame = Frame.create("mumu:0", "#T", b"png", 3)
        assert frame.frame_id.startswith("mumu:0:3:")
        assert frame.png == b"png"


class MapFrameTests(unittest.TestCase):
    def test_a_point_on_the_edge_is_on_the_map_and_left_where_it_is(self) -> None:
        diamond = MapFrame(centre=(800, 400), half_width=1000, half_height=450)
        assert diamond.contains((1800, 400))
        assert diamond.clamp((1800, 400)) == (1800, 400)
        assert not diamond.contains((1801, 400))

    def test_a_survey_fits_the_diamond_its_rays_measured(self) -> None:
        radius = 1 / (math.cos(math.radians(45)) / 1000 + math.sin(math.radians(45)) / 450)
        survey = MapSurvey(
            edges=[
                MapEdge(degrees=0, reached=1000, predicted=800),
                MapEdge(degrees=90, reached=450, predicted=800),
                MapEdge(degrees=45, reached=round(radius), predicted=800),
                # A ray taken at the screen edge measured the screen, not the map.
                MapEdge(degrees=180, reached=None, predicted=800),
            ]
        )
        fitted = survey.fitted
        assert fitted is not None
        assert abs(fitted.half_width - 1000) <= 3
        assert abs(fitted.half_height - 450) <= 3
        assert survey.summary.startswith("3 of 4 ray(s) found the map edge")

    def test_fewer_than_three_measured_rays_fit_nothing(self) -> None:
        survey = MapSurvey(
            edges=[
                MapEdge(degrees=0, reached=1000, predicted=800),
                MapEdge(degrees=90, reached=None, predicted=800),
            ]
        )
        assert survey.fitted is None
        assert "accepted at the screen edge" not in survey.summary
        assert MapSurvey(edges=[MapEdge(degrees=0, reached=None, predicted=800)]).summary == (
            "1 ray(s) accepted at the screen edge; the map reaches past it"
        )


class PlanTests(unittest.TestCase):
    def _step(self, act: str, *points: tuple[float, float], seconds: int = 0) -> AttackStep:
        return AttackStep(
            act=act,
            who="unknown",
            at=[ScreenPoint(x_pct=x, y_pct=y) for x, y in points],
            seconds=seconds,
        )

    def test_a_plan_with_no_troops_step_has_no_line(self) -> None:
        plan = AttackPlan(steps=[self._step("hero", (20, 20)), self._step("wait", seconds=3)])
        assert plan.deploy_start is None
        assert plan.deploy_end is None
        assert [step.act for step in plan.acts("wait")] == ["wait"]

    def test_the_line_is_the_first_troops_step(self) -> None:
        plan = AttackPlan(
            steps=[
                self._step("troops", (37.5, 12.22), (14.38, 42.22)),
                self._step("troops", (1, 1)),
            ]
        )
        assert plan.deploy_start == ScreenPoint(x_pct=37.5, y_pct=12.22)
        assert plan.deploy_end is not None
        assert plan.deploy_end.pixels() == (230, 380)

    def test_a_step_refuses_an_act_or_a_wait_the_loop_could_not_play(self) -> None:
        with pytest.raises(ValidationError):
            self._step("teleport", (1, 1))
        with pytest.raises(ValidationError):
            self._step("wait", seconds=181)
        with pytest.raises(ValidationError):
            ScreenPoint(x_pct=101, y_pct=0)

    def test_a_played_plan_round_trips_either_village(self) -> None:
        """The union has to come back as the plan that went in, not the other kind."""
        night = NightPlan(
            deploy_start=ScreenPoint(x_pct=37.5, y_pct=12),
            deploy_end=ScreenPoint(x_pct=14, y_pct=42),
            hero_points=[],
            troops_after=4,
        )
        day = AttackPlan(steps=[self._step("troops", (37.5, 12), (14, 42))])
        for plan in (night, day):
            line = PlayedPlan(round=2, plan=plan).model_dump_json()
            back = PlayedPlan.model_validate_json(line)
            assert back.round == 2
            assert back.plan == plan
            assert type(back.plan) is type(plan)


class ReportArithmeticTests(unittest.TestCase):
    def test_a_building_name_reads_the_way_the_game_writes_it(self) -> None:
        assert str(BuildingName(name="金礦", level=12)) == "金礦(12級)"
        assert str(BuildingName(name="金礦")) == "金礦"
        assert str(BuildingName()) == "?"

    def test_wall_and_build_reports_add_up_what_was_paid(self) -> None:
        walls = WallReport(
            outcome="bought",
            upgrades=[
                WallUpgrade(unit=1_600_000, count=3, resource="gold"),
                WallUpgrade(unit=1_600_000, count=2, resource="elixir"),
            ],
        )
        assert walls.walls == 5
        assert (walls.paid("gold"), walls.paid("elixir")) == (4_800_000, 3_200_000)
        build = BuildReport(
            outcome="started",
            started=[
                BuildCandidate(point=(1, 1), resource="gold", price=100),
                BuildCandidate(point=(2, 2), resource="elixir", price=250),
            ],
        )
        assert (build.paid("gold"), build.paid("elixir"), build.paid("dark")) == (100, 250, 0)


class GeminiRequestTests(unittest.TestCase):
    def test_the_body_uses_the_wire_names_and_drops_what_was_not_set(self) -> None:
        request = GeminiRequest(
            model="m",
            input=[GeminiTextPart(text="hi"), GeminiImagePart(data="AAAA")],
            response_format=GeminiResponseFormat(json_schema={"type": "object"}),
        )
        body = request.body()
        assert body["response_format"]["schema"] == {"type": "object"}
        assert "json_schema" not in body["response_format"]
        assert "generation_config" not in body
        assert body["input"][1] == {"type": "image", "data": "AAAA", "mime_type": "image/png"}


class ConfigDefaultTests(unittest.TestCase):
    def test_the_defaults_are_the_ones_worth_farming_with(self) -> None:
        config = AppConfig()
        assert config.thresholds.min_gold > 0
        assert config.stop_at == 90
        assert config.restart_every > 0
        assert json.loads(config.model_dump_json())["gemini"]["main"]["thinking_level"] == "low"


class RunLogTests(unittest.TestCase):
    def test_two_runs_in_one_second_get_different_directories(self) -> None:
        """A shell loop over `ai_coc read` is how that really happens."""
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(models, "LOG_DIR", Path(folder)),
            patch.object(models, "datetime") as clock,
        ):
            clock.now.return_value.astimezone.return_value.strftime.return_value = (
                "2026-09-03-000000"
            )
            first = RunLog.open("read")
            second = RunLog.open("read")
            third = RunLog.open("read")
            assert first.directory.name == "2026-09-03-000000-read"
            assert second.directory.name == "2026-09-03-000000-read-2"
            assert third.directory.name == "2026-09-03-000000-read-3"
            assert third.directory.is_dir()

    def test_a_label_becomes_the_third_part_of_the_name(self) -> None:
        """A session saving evidence wants to find that run again afterwards,
        and inventing a path was the alternative: measured on this machine, 20
        of 438 run directories had been hand-named, four of them ending in
        `.png` because somebody read a directory argument as a filename.
        """
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(models, "LOG_DIR", Path(folder)),
            patch.object(models, "datetime") as clock,
        ):
            clock.now.return_value.astimezone.return_value.strftime.return_value = (
                "2026-09-04-000000"
            )
            named = RunLog.open("capture", label="baseline")
            # A separator getting through would put the run somewhere nobody
            # goes looking, which is the failure a label replaces rather than
            # reproduces in a new place.
            awkward = RunLog.open("capture", label="night/run 2")
            plain = RunLog.open("read")
        assert named.directory.name == "2026-09-04-000000-capture-baseline"
        assert awkward.directory.name == "2026-09-04-000000-capture-night-run-2"
        assert plain.directory.name == "2026-09-04-000000-read"

    def test_old_frames_are_culled_and_the_log_beside_them_is_kept(self) -> None:
        """Measured across 438 runs here, 11 456 PNGs came to 27.2 GB while
        every `run.log`, `result.json` and `plans.jsonl` together came to 4.6 MB
        — so the listing stays the history and only the frames age out.
        """
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(models, "LOG_DIR", Path(folder)),
        ):
            old, fresh = Path(folder) / "old-attack", Path(folder) / "fresh-attack"
            for run in (old, fresh):
                (run / "frames").mkdir(parents=True)
                (run / "frames" / "0001.png").write_bytes(b"png")
                (run / "run.log").write_text("kept", encoding="utf-8")
            stale = (
                datetime.now(UTC) - timedelta(days=models.FRAME_RETENTION_DAYS + 1)
            ).timestamp()
            os.utime(old / "frames", (stale, stale))

            models._cull_old_frames()

            assert not (old / "frames").exists()
            assert (old / "run.log").read_text(encoding="utf-8") == "kept"
            assert (fresh / "frames" / "0001.png").exists()

    def test_the_paths_hang_off_the_directory(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            run = RunLog(directory=Path(folder))
            assert run.log_path == Path(folder) / "run.log"
            assert run.plan_log == Path(folder) / "plans.jsonl"
            assert run.frames is None


if __name__ == "__main__":
    unittest.main()
