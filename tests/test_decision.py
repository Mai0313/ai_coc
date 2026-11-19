"""`ai_coc decision`: one yes/no question about one image, put to the Decisions API.

The API call sits behind `GeminiClient.decide`, which every test here replaces,
and behind `client.decisions` for the test of `decide` itself, so nothing
reaches the network.
"""

from __future__ import annotations

import io
import json
from types import SimpleNamespace
import base64
from typing import TYPE_CHECKING, Any, get_args
import hashlib
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from ai_coc import cli, commands, logging_setup
from ai_coc.models import (
    RunLog,
    AppConfig,
    GeminiSetting,
    DecisionAnswer,
    DecisionReport,
    GeminiSettings,
    DecisionOutcome,
    DecisionSetting,
)
from ai_coc.adapters.ai import GeminiClient
from ai_coc.adapters.config import ConfigStore

if TYPE_CHECKING:
    from pathlib import Path

PNG = b"\x89PNG not really"


@pytest.fixture
def image(tmp_path: Path) -> Path:
    path = tmp_path / "frame.png"
    path.write_bytes(PNG)
    return path


@pytest.fixture
def key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands, "gemini_key", lambda _setting: "k")


def _said(probability: float | None) -> DecisionAnswer:
    confidence = None if probability is None else max(probability, 1 - probability)
    return DecisionAnswer(
        probability=probability, confidence=confidence, seconds=0.42, attempts=1, tokens=1120
    )


def _ask(image: Path, probability: float | None, truth: str | None = None) -> DecisionReport:
    with patch.object(GeminiClient, "decide", return_value=_said(probability)):
        return commands.decision("視窗開著嗎", image, truth)


class FakeDecisions:
    """Stands in for `client.decisions`, recording the body it was handed."""

    def __init__(self, answers: dict[str, Any], blocked: str | None = None) -> None:
        self.answers = answers
        self.blocked = blocked
        self.body: dict[str, Any] = {}

    def create(self, **body: object) -> object:
        self.body = body
        return SimpleNamespace(
            answers=self.answers,
            prompt_feedback=SimpleNamespace(block_reason=self.blocked),
            usage=SimpleNamespace(total_input_tokens=1120),
        )


def _client(decisions: FakeDecisions) -> GeminiClient:
    client = GeminiClient(api_key="k", settings=GeminiSetting(model="gemini-test"))
    client._client = SimpleNamespace(decisions=decisions)
    return client


def test_the_question_and_the_image_travel_to_the_decision_model() -> None:
    decisions = FakeDecisions({"q": SimpleNamespace(noul=0.97, confidence=0.97)})
    said = _client(decisions).decide("視窗開著嗎", PNG, "image/png", 30)
    assert (said.probability, said.confidence, said.tokens) == (0.97, 0.97, 1120)
    # The model `decision` hands it from the settings file.
    assert decisions.body["model"] == "gemini-test"
    assert decisions.body["questions"] == {"q": {"type": "noul", "instructions": "視窗開著嗎"}}
    [part] = decisions.body["input"]
    assert (part["type"], part["mime_type"]) == ("image", "image/png")
    assert base64.b64decode(part["data"]) == PNG
    assert (decisions.body["api_version"], decisions.body["timeout"]) == ("v1beta", 30)


def test_a_reply_with_no_answer_carries_no_probability() -> None:
    said = _client(FakeDecisions({})).decide("視窗開著嗎", PNG, "image/png", 30)
    assert (said.probability, said.confidence) == (None, None)


def test_a_blocked_input_is_a_failed_call_and_not_an_unsure_model() -> None:
    with pytest.raises(RuntimeError, match="image_safety"):
        _client(FakeDecisions({}, blocked="image_safety")).decide(
            "視窗開著嗎", PNG, "image/png", 30
        )


@pytest.mark.usefixtures("key")
@pytest.mark.parametrize(
    ("probability", "answer", "outcome"),
    [
        (0.99, "yes", "answered"),
        (0.7, "yes", "answered"),
        (0.69, None, "unclear"),
        (0.5, None, "unclear"),
        (0.31, None, "unclear"),
        (0.3, "no", "answered"),
        (0.01, "no", "answered"),
        (None, None, "unclear"),
    ],
)
def test_only_a_sure_probability_is_a_yes_or_a_no(
    image: Path, probability: float | None, answer: str | None, outcome: str
) -> None:
    report = _ask(image, probability)
    assert (report.answer, report.outcome) == (answer, outcome)
    # Kept either way, so a tally can cut it somewhere else.
    assert report.probability == probability


@pytest.mark.usefixtures("key")
def test_the_report_holds_what_a_tally_needs_once_the_image_is_gone(image: Path) -> None:
    report = _ask(image, 0.95, "yes")
    assert report.prompt == "視窗開著嗎"
    assert report.image == image.resolve()
    assert (report.sha256, report.size) == (hashlib.sha256(PNG).hexdigest(), len(PNG))
    # The model names who answered, as on every earlier line.
    assert report.model == "gemini-decision-1"
    assert "tier" not in report.model_dump()
    assert (report.seconds, report.attempts, report.tokens) == (0.42, 1, 1120)
    assert report.total_seconds is not None
    assert report.total_seconds > 0


@pytest.mark.usefixtures("key")
def test_the_callers_own_answer_is_kept_and_compared(image: Path) -> None:
    assert _ask(image, 0.95).agrees is None
    assert _ask(image, 0.95, "yes").agrees is True
    disagreed = _ask(image, 0.95, "no")
    assert (disagreed.answer, disagreed.ground_truth, disagreed.agrees) == ("yes", "no", False)
    # Nothing to compare an unclear reply with, and the caller's answer stays.
    unclear = _ask(image, 0.5, "no")
    assert (unclear.ground_truth, unclear.agrees) == ("no", None)


@pytest.mark.usefixtures("key")
def test_a_missing_image_asks_nothing(tmp_path: Path) -> None:
    with patch.object(GeminiClient, "decide") as asked:
        report = commands.decision("視窗開著嗎", tmp_path / "gone.png", "yes")
    asked.assert_not_called()
    assert (report.outcome, report.answer, report.sha256) == ("no_image", None, None)


def test_a_missing_key_asks_nothing(image: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commands, "gemini_key", lambda _setting: "")
    with patch.object(GeminiClient, "decide") as asked:
        report = commands.decision("視窗開著嗎", image)
    asked.assert_not_called()
    assert (report.outcome, report.answer) == ("no_key", None)
    # The image was read, so the line still says which frame went unasked.
    assert report.sha256 == hashlib.sha256(PNG).hexdigest()


@pytest.mark.usefixtures("key")
def test_a_failed_call_is_no_answer(image: Path) -> None:
    with patch.object(GeminiClient, "decide", side_effect=ConnectionError("network down")):
        report = commands.decision("視窗開著嗎", image, "yes")
    assert (report.outcome, report.answer, report.agrees) == ("failed", None, None)


@pytest.mark.usefixtures("key")
def test_it_leaves_the_state_file_alone(image: Path) -> None:
    """It runs beside a farming loop: no claim, so nothing is stood down."""
    commands.STATE_PATH.write_text("the loop's record", encoding="utf-8")
    with (
        patch.object(GeminiClient, "decide", return_value=_said(0.95)),
        patch.object(commands, "claim") as claimed,
        patch("sys.stdout", new_callable=io.StringIO),
    ):
        line = ["decision", "--prompt", "視窗開著嗎", "--image-path", str(image)]
        run = RunLog(directory=image.parent)
        assert cli._run_command(cli._parser().parse_args(line), run) == 0
    claimed.assert_not_called()
    assert commands.STATE_PATH.read_text(encoding="utf-8") == "the loop's record"
    assert "decision" in cli.WITHOUT_CLAIM


def test_every_outcome_has_a_line_of_its_own() -> None:
    assert set(commands.DECISION_LINES) == set(get_args(DecisionOutcome))
    assert len(set(commands.DECISION_LINES.values())) == len(commands.DECISION_LINES)


def test_the_text_is_the_answer_then_the_seconds_then_the_comparison(image: Path) -> None:
    report = DecisionReport(
        prompt="p", image=image, model="m", answer="no", seconds=0.42, outcome="answered"
    )
    assert commands.decision_text(report) == "no\nAPI 0.42 秒"
    compared = report.model_copy(update={"ground_truth": "yes", "agrees": False})
    assert commands.decision_text(compared) == "no\nAPI 0.42 秒\nground truth 是 yes, 不一致"
    unclear = report.model_copy(update={"answer": None, "probability": 0.5, "outcome": "unclear"})
    shown = commands.decision_text(unclear).splitlines()[0]
    assert shown == f"{commands.DECISION_LINES['unclear']} (是的機率 0.50)"
    assert shown not in ("yes", "no")


@pytest.mark.usefixtures("key")
@pytest.mark.parametrize(
    ("probability", "code", "printed"),
    [
        (0.95, 0, "yes\nAPI 0.42 秒\nground truth 是 yes, 一致\n"),
        (0.05, 0, "no\nAPI 0.42 秒\nground truth 是 yes, 不一致\n"),
        (0.5, 1, "模型沒有給出明確的是或否 (是的機率 0.50)\nAPI 0.42 秒\n"),
    ],
)
def test_the_line_prints_the_answer_and_exits_non_zero_without_one(
    image: Path, probability: float, code: int, printed: str, capsys: pytest.CaptureFixture[str]
) -> None:
    line = ["decision", "--prompt", "視窗開著嗎", "--image-path", str(image)]
    asked = cli._parser().parse_args([*line, "--ground-truth", "yes"])
    with patch.object(GeminiClient, "decide", return_value=_said(probability)):
        assert cli._run_command(asked, RunLog(directory=image.parent)) == code
    # Whatever else pytest's live logging put on stdout, the answer ends it.
    assert capsys.readouterr().out.endswith(printed)

    # The journal's `end` line is the trial's record.
    end = json.loads(logging_setup.JOURNAL_PATH.read_text(encoding="utf-8").splitlines()[-1])
    assert (end["exit"], end["result"]["probability"]) == (code, probability)
    assert end["result"]["ground_truth"] == "yes"
    assert end["result"]["sha256"] == hashlib.sha256(PNG).hexdigest()


def test_a_ground_truth_is_yes_or_no_and_the_question_and_image_are_required(image: Path) -> None:
    whole = ["decision", "--prompt", "p", "--image-path", str(image)]
    assert cli._parser().parse_args([*whole, "--ground-truth", "no"]).ground_truth == "no"
    for bad in ([*whole, "--ground-truth", "maybe"], whole[:3], ["decision", *whole[3:]]):
        with patch("sys.stderr", new_callable=io.StringIO), pytest.raises(SystemExit):
            cli._parser().parse_args(bad)


def test_the_decision_block_left_blank_borrows_mains_key_and_address() -> None:
    main = GeminiSetting(model="gemini-main", base_url="https://proxy.example", api_key="mk")
    setting = commands.decision_setting(GeminiSettings(main=main))
    assert (setting.model, setting.base_url, setting.api_key) == (
        "gemini-decision-1",
        main.base_url,
        "mk",
    )
    own = GeminiSettings(
        main=main,
        decision=DecisionSetting(model="d", base_url="https://other.example", api_key="dk"),
    )
    assert commands.decision_setting(own).model_dump() == {
        "model": "d",
        "base_url": "https://other.example",
        "api_key": "dk",
        "thinking_level": "low",
    }


MAIN = {"model": "gemini-main", "base_url": "", "api_key": "mk", "thinking_level": "medium"}
DECISION = {"model": "gemini-decision-1", "base_url": "https://other.example", "api_key": "dk"}


def test_a_settings_file_from_before_the_decision_block_keeps_main_and_gains_it() -> None:
    store = ConfigStore()
    store.path.write_text(
        json.dumps({"adb_serial": "s", "gemini": {"main": MAIN}}), encoding="utf-8"
    )
    assert store.load().gemini.main.model_dump() == MAIN
    written = json.loads(store.path.read_text(encoding="utf-8"))["gemini"]
    assert written == {
        "main": MAIN,
        "decision": {"model": "gemini-decision-1", "base_url": "", "api_key": ""},
    }


def test_a_settings_file_with_a_lite_block_keeps_main_and_decision_and_drops_it() -> None:
    """Written while `lite` was a tier beside `decision`: it loads, and the next write leaves it out."""
    store = ConfigStore()
    lite = {
        "model": "gemini-3.5-flash-lite",
        "base_url": "",
        "api_key": "",
        "thinking_level": "low",
    }
    store.path.write_text(
        json.dumps({
            "adb_serial": "s",
            "gemini": {"main": MAIN, "decision": DECISION, "lite": lite},
        }),
        encoding="utf-8",
    )
    gemini = store.load().gemini
    assert (gemini.main.model_dump(), gemini.decision.model_dump()) == (MAIN, DECISION)
    written = json.loads(store.path.read_text(encoding="utf-8"))["gemini"]
    assert written == {"main": MAIN, "decision": DECISION}


def test_a_build_with_the_lite_block_reads_the_new_file_and_keeps_main_and_decision() -> None:
    """`GeminiSettings` as it was with `lite` beside `decision`: the missing block is filled in."""

    class LiteGemini(BaseModel):
        main: GeminiSetting = GeminiSetting()
        decision: DecisionSetting = DecisionSetting()
        lite: GeminiSetting = GeminiSetting(model="gemini-3.5-flash-lite")

    class LiteConfig(AppConfig):
        gemini: LiteGemini = LiteGemini()  # ty: ignore[invalid-assignment]

    new = AppConfig(
        gemini=GeminiSettings(
            main=GeminiSetting.model_validate(MAIN),
            decision=DecisionSetting.model_validate(DECISION),
        )
    ).model_dump_json()
    old = LiteConfig.model_validate_json(new)
    assert (old.gemini.main.model_dump(), old.gemini.decision.model_dump()) == (MAIN, DECISION)
    assert old.gemini.lite.model == "gemini-3.5-flash-lite"


def test_a_build_from_before_the_decision_block_reads_the_new_file_and_keeps_main() -> None:
    """`GeminiSettings` as it was, with `main` alone: the `decision` block is ignored, not an error."""

    class OldGemini(BaseModel):
        main: GeminiSetting = GeminiSetting()

    class OldConfig(AppConfig):
        gemini: OldGemini = OldGemini()  # ty: ignore[invalid-assignment]

    new = AppConfig(
        gemini=GeminiSettings(main=GeminiSetting.model_validate(MAIN))
    ).model_dump_json()
    old = OldConfig.model_validate_json(new)
    assert old.gemini.main.model_dump() == MAIN
    assert json.loads(old.model_dump_json())["gemini"] == {"main": MAIN}
