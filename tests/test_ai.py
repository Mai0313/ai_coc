import base64
from typing import Any
import unittest

import pytest

from ai_coc.models import AgentAction, GeminiSetting
from ai_coc.adapters.ai import GeminiClient


class FakeDelta:
    def __init__(self, kind: str, text: str = "") -> None:
        self.type = kind
        self.text = text


class FakeError:
    def __init__(self, message: str) -> None:
        self.message = message


class FakeEvent:
    def __init__(self, event_type: str, **fields: object) -> None:
        self.event_type = event_type
        for name, value in fields.items():
            setattr(self, name, value)


class FakeInteractions:
    """Stands in for `client.interactions`, recording the body it was handed."""

    def __init__(self, events: list[FakeEvent] | None = None, output_text: str = "") -> None:
        self.events = events or []
        self.output_text = output_text
        self.body: dict[str, Any] = {}

    def create(self, **body: object) -> object:
        self.body = body
        if body.get("stream"):
            return iter(self.events)
        return FakeEvent("done", output_text=self.output_text, status="completed")


class FakeGenaiClient:
    def __init__(self, interactions: FakeInteractions) -> None:
        self.interactions = interactions


def _client(interactions: FakeInteractions) -> GeminiClient:
    client = GeminiClient(api_key="k", settings=GeminiSetting(model="gemini-test"))
    client._client = FakeGenaiClient(interactions)
    return client


class StreamTests(unittest.TestCase):
    def test_text_deltas_are_yielded_in_order(self) -> None:
        interactions = FakeInteractions([
            FakeEvent("interaction.created"),
            FakeEvent("step.start"),
            FakeEvent("step.delta", delta=FakeDelta("text", "好")),
            FakeEvent("step.delta", delta=FakeDelta("thought_summary")),
            FakeEvent("step.delta", delta=FakeDelta("text", "的")),
            FakeEvent("interaction.completed"),
        ])
        assert "".join(_client(interactions).stream("在嗎")) == "好的"
        assert interactions.body["stream"] is True
        assert interactions.body["model"] == "gemini-test"
        assert interactions.body["input"] == [{"type": "text", "text": "在嗎"}]

    def test_a_screenshot_travels_as_a_base64_image_part(self) -> None:
        interactions = FakeInteractions([FakeEvent("step.delta", delta=FakeDelta("text", "x"))])
        list(_client(interactions).stream("看畫面", b"PNGDATA"))
        image = interactions.body["input"][1]
        assert image["type"] == "image"
        assert base64.b64decode(image["data"]) == b"PNGDATA"
        assert image["mime_type"] == "image/png"

    def test_an_error_event_stops_the_stream(self) -> None:
        interactions = FakeInteractions([
            FakeEvent("step.delta", delta=FakeDelta("text", "半")),
            FakeEvent("error", error=FakeError("quota exhausted")),
        ])
        with pytest.raises(RuntimeError) as caught:
            list(_client(interactions).stream("在嗎"))
        assert "quota exhausted" in str(caught.value)

    def test_an_error_event_without_a_body_still_raises(self) -> None:
        interactions = FakeInteractions([FakeEvent("error", error=None)])
        with pytest.raises(RuntimeError, match="串流中斷"):
            list(_client(interactions).stream("在嗎"))

    def test_a_stream_with_no_text_is_an_error(self) -> None:
        with pytest.raises(RuntimeError):
            list(_client(FakeInteractions([FakeEvent("interaction.completed")])).stream("在嗎"))


class StructuredTests(unittest.TestCase):
    def test_the_schema_is_sent_under_its_json_name(self) -> None:
        interactions = FakeInteractions(output_text='{"done": true, "message": "完成"}')
        action = _client(interactions).generate_structured("下一步？", AgentAction)
        assert action.done is True
        assert interactions.body["response_format"]["schema"]["title"] == "AgentAction"
        assert interactions.body["response_format"]["mime_type"] == "application/json"

    def test_a_reply_that_does_not_match_the_model_is_rejected(self) -> None:
        interactions = FakeInteractions(output_text="不是 JSON")
        with pytest.raises(RuntimeError):
            _client(interactions).generate_structured("下一步？", AgentAction)


if __name__ == "__main__":
    unittest.main()
