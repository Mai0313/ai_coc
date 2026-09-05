import base64
from typing import Any
import unittest

import pytest

from ai_coc.models import BuildingName, GeminiSetting
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

    def create(self, **body: object) -> object:
        self.body = body
        return FakeEvent("done", output_text=self.output_text, status="completed")


class FakeGenaiClient:
    def __init__(self, interactions: FakeInteractions) -> None:
        self.interactions = interactions


def _client(interactions: FakeInteractions) -> GeminiClient:
    client = GeminiClient(api_key="k", settings=GeminiSetting(model="gemini-test"))
    client._client = FakeGenaiClient(interactions)
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
