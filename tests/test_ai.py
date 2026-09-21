import base64
from typing import Any
import unittest
from unittest.mock import patch

import pytest

from ai_coc.models import BuildingName, GeminiSetting
from ai_coc.adapters import ai
from ai_coc.adapters.ai import GeminiClient


class FakeEvent:
    def __init__(self, event_type: str, **fields: object) -> None:
        self.event_type = event_type
        for name, value in fields.items():
            setattr(self, name, value)


class FakeInteractions:
    """Stands in for `client.interactions`, recording the body it was handed."""

    def __init__(self, output_text: str = "") -> None:
        self.output_text = output_text
        self.body: dict[str, Any] = {}

    on_create: object = None

    def create(self, **body: object) -> object:
        self.body = body
        if callable(self.on_create):
            self.on_create()
        return FakeEvent("done", output_text=self.output_text, status="completed")


class FakeGenaiClient:
    def __init__(self, interactions: FakeInteractions) -> None:
        self.interactions = interactions


def _client(interactions: FakeInteractions, sends: int = 1) -> GeminiClient:
    """A client whose transport is faked, and which has `sends` HTTP requests behind it.

    The fake replaces httpx, so the event hook that counts real requests never
    fires — seeding the counter is what keeps the fake faithful to an SDK that
    sent one request and answered.
    """
    client = GeminiClient(api_key="k", settings=GeminiSetting(model="gemini-test"))
    client._client = FakeGenaiClient(interactions)
    interactions.on_create = lambda: client._sends.extend([0.0] * sends)
    return client


class RequestTests(unittest.TestCase):
    def test_the_prompt_travels_as_the_first_text_part(self) -> None:
        interactions = FakeInteractions(output_text="好的")
        assert _client(interactions).generate("在嗎") == "好的"
        assert interactions.body["model"] == "gemini-test"
        assert interactions.body["input"] == [{"type": "text", "text": "在嗎"}]

    def test_a_screenshot_travels_as_a_base64_image_part(self) -> None:
        interactions = FakeInteractions(output_text="x")
        _client(interactions).generate("看畫面", b"PNGDATA")
        image = interactions.body["input"][1]
        assert image["type"] == "image"
        assert base64.b64decode(image["data"]) == b"PNGDATA"
        assert image["mime_type"] == "image/png"

    def test_a_reply_that_outlasted_its_deadline_says_so(self) -> None:
        """Because `timeout` is not a deadline on the call and never was.

        The SDK hands it to httpx, which spends it as four separate
        per-operation waits, and `httpx.Timeout` has no total-deadline field at
        all. The read clock restarts on every chunk received, so a reply that
        keeps trickling in comes back successfully at any multiple of the number
        it was given, looking like any other reply. Measured, one planning call
        returned after 143.0 s against a 30 s deadline with the battle it was
        planning already two minutes old.

        **This docstring used to blame the SDK's retries, and that is measurably
        wrong**: a timeout is never retried — `APITimeoutError` is wrapped as a
        `PermanentError` and re-raised on the spot, one request only. Retries
        multiply a *failing* call, which is why the attempt count is in the line
        rather than the explanation.
        """
        interactions = FakeInteractions(output_text="遲到了")
        client = _client(interactions)
        clock = iter([0.0, 91.0])
        with (
            patch.object(ai.time, "monotonic", side_effect=lambda: next(clock)),
            self.assertLogs("ai_coc.adapters.ai", level="WARNING") as logged,
        ):
            assert client._create("在嗎", None, timeout=20) == "遲到了"
        line = "\n".join(logged.output)
        assert "91.0s against a 20.0s deadline" in line
        # The count is what separates the two ways to outlast a deadline: one
        # attempt is a slow reply, more is the SDK having retried something.
        assert "in 1 HTTP attempt(s)" in line

    def test_a_reply_inside_its_deadline_says_nothing_extra(self) -> None:
        interactions = FakeInteractions(output_text="準時")
        client = _client(interactions)
        clock = iter([0.0, 11.0])
        with (
            patch.object(ai.time, "monotonic", side_effect=lambda: next(clock)),
            self.assertNoLogs("ai_coc.adapters.ai", level="WARNING"),
        ):
            assert client._create("在嗎", None, timeout=20) == "準時"

    def test_an_empty_reply_is_an_error_rather_than_an_empty_string(self) -> None:
        with pytest.raises(RuntimeError):
            _client(FakeInteractions()).generate("在嗎")


class StructuredTests(unittest.TestCase):
    def test_the_schema_is_sent_under_its_json_name(self) -> None:
        interactions = FakeInteractions(output_text='{"name": "金礦"}')
        target = _client(interactions).generate_structured("這是什麼建築？", BuildingName)
        assert target.name == "金礦"
        assert interactions.body["response_format"]["schema"]["title"] == "BuildingName"
        assert interactions.body["response_format"]["mime_type"] == "application/json"

    def test_a_reply_that_does_not_match_the_model_is_rejected(self) -> None:
        interactions = FakeInteractions(output_text="不是 JSON")
        with pytest.raises(RuntimeError):
            _client(interactions).generate_structured("這是什麼建築？", BuildingName)


if __name__ == "__main__":
    unittest.main()
