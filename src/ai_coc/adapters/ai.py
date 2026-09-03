from __future__ import annotations

import time
import base64
from typing import TYPE_CHECKING, Any, TypeVar, cast
import logging

from google import genai
from pydantic import BaseModel, PrivateAttr, ValidationError
from google.genai import types, errors, interactions

from ai_coc.models import (
    GeminiRequest,
    GeminiSetting,
    GeminiTextPart,
    GeminiImagePart,
    GeminiResponseFormat,
    GeminiGenerationConfig,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# The endpoint also lists image, speech and embedding models. The controller
# only ever sends a prompt plus a screenshot and expects text back.
NON_TEXT_MODEL_TAGS = (
    "tts",
    "audio",
    "speech",
    "image",
    "video",
    "veo",
    "imagen",
    "embedding",
    "aqa",
)


class GeminiClient(BaseModel):
    """The single place the application talks to Gemini, through google-genai."""

    # Its own field rather than part of `settings`, because the two come from
    # different places and only one of them may be written down: the settings are
    # the plaintext config file and the key is the DPAPI store.
    api_key: str = ""
    settings: GeminiSetting

    _client: genai.Client | None = PrivateAttr(default=None)

    @property
    def client(self) -> genai.Client:
        """Built lazily so a missing key surfaces on the worker thread, not in a slot."""
        if self._client is None:
            if not self.api_key.strip():
                raise ValueError("尚未設定 Gemini API Key")
            self._client = genai.Client(
                api_key=self.api_key.strip(),
                http_options=types.HttpOptions(base_url=self.settings.base_url)
                if self.settings.base_url.strip()
                else None,
            )
        return self._client

    def _request(
        self,
        prompt: str,
        image_png: bytes | None,
        response_format: GeminiResponseFormat | None = None,
    ) -> GeminiRequest:
        parts: list[GeminiTextPart | GeminiImagePart] = [GeminiTextPart(text=prompt)]
        if image_png:
            parts.append(GeminiImagePart(data=base64.b64encode(image_png).decode("ascii")))
        logger.info(
            "Gemini request: model=%s prompt=%d chars image=%s structured=%s thinking=%s",
            self.settings.model,
            len(prompt),
            f"{len(image_png)} bytes" if image_png else "none",
            bool(response_format),
            self.settings.thinking_level,
        )
        logger.debug("Gemini prompt: %s", prompt)
        return GeminiRequest(
            model=self.settings.model,
            input=parts,
            response_format=response_format,
            generation_config=GeminiGenerationConfig(thinking_level=self.settings.thinking_level),
        )

    def _create(
        self,
        prompt: str,
        image_png: bytes | None,
        response_format: GeminiResponseFormat | None = None,
        timeout: float | None = None,
    ) -> str:
        """One request; `timeout` is seconds, and giving up is the caller's to handle.

        The deadline belongs to the call rather than to the client because one
        client serves the chat, the agent loop and the attack planner, and only
        the planner has a window it can miss. A timed-out call raises the SDK's
        own `APITimeoutError`, which is **not** a `google.genai.errors.APIError`
        and so does not reach the handler below — that is deliberate, since the
        only caller passing a deadline is the one that answers a failure by
        falling back to a plan it already has.
        """
        request = self._request(prompt, image_png, response_format)
        started = time.monotonic()
        try:
            # create() also returns a Stream when stream=True, which this never sets.
            interaction = cast(
                "interactions.Interaction",
                self.client.interactions.create(**request.body(), timeout=timeout),
            )
        except errors.APIError as exc:
            logger.exception("Gemini request failed on model %s", self.settings.model)
            raise RuntimeError(f"Gemini 請求失敗（{self.settings.model}）：{exc}") from exc
        text = (interaction.output_text or "").strip()
        logger.info(
            "Gemini replied in %.1fs with %d chars (status=%s)",
            time.monotonic() - started,
            len(text),
            interaction.status,
        )
        logger.debug("Gemini reply: %s", text)
        if not text:
            raise RuntimeError(f"Gemini 沒有回傳內容，status={interaction.status}")
        return text

    def generate(self, prompt: str, image_png: bytes | None = None) -> str:
        return self._create(prompt, image_png)

    def stream(self, prompt: str, image_png: bytes | None = None) -> Iterator[str]:
        """Yield the reply in the chunks Gemini sends, so the UI can show it as it arrives."""
        request = self._request(prompt, image_png)
        started = time.monotonic()
        received = 0
        try:
            events = cast(
                "Iterator[Any]", self.client.interactions.create(**request.body(), stream=True)
            )
            for event in events:
                # Anything else is a step boundary or a tool event this app never asks for.
                if event.event_type == "error":
                    # ErrorEvent.error is optional; fall back to the event itself.
                    reason = event.error.message if event.error else event
                    raise RuntimeError(f"Gemini 串流中斷：{reason}")
                if event.event_type == "step.delta" and event.delta.type == "text":
                    received += len(event.delta.text)
                    yield event.delta.text
        except errors.APIError as exc:
            logger.exception("Gemini stream failed on model %s", self.settings.model)
            raise RuntimeError(f"Gemini 串流失敗（{self.settings.model}）：{exc}") from exc
        logger.info("Gemini streamed %d chars in %.1fs", received, time.monotonic() - started)
        if not received:
            raise RuntimeError("Gemini 沒有回傳內容")

    def generate_structured(
        self,
        prompt: str,
        schema: type[T],
        image_png: bytes | None = None,
        timeout: float | None = None,
    ) -> T:
        """Ask for one JSON object and hand back the validated Pydantic model."""
        text = self._create(
            prompt,
            image_png,
            GeminiResponseFormat(json_schema=schema.model_json_schema()),
            timeout,
        )
        try:
            return schema.model_validate_json(text)
        except ValidationError as exc:
            logger.error("Gemini reply did not match %s: %s", schema.__name__, text)
            raise RuntimeError(f"Gemini 回覆不符合 {schema.__name__} 格式：{text[:500]}") from exc

    def list_text_models(self) -> list[str]:
        """Model IDs on this endpoint that can answer a prompt with text."""
        try:
            models = list(self.client.models.list())
        except errors.APIError as exc:
            logger.exception("Listing Gemini models failed")
            raise RuntimeError(f"無法取得模型清單：{exc}") from exc
        candidates: list[tuple[str, list[str]]] = []
        for model in models:
            name = (model.name or "").removeprefix("models/")
            if name and not any(tag in name.lower() for tag in NON_TEXT_MODEL_TAGS):
                candidates.append((name, list(model.supported_actions or [])))
        # Older endpoints report generateContent; newer ones may report nothing.
        # Falling back to the name filter keeps the picker from coming up empty.
        generative = [name for name, actions in candidates if "generateContent" in actions]
        names = sorted(set(generative or [name for name, _ in candidates]))
        logger.info("Gemini endpoint exposes %d text models", len(names))
        return names

    def test(self) -> str:
        return self._create("Reply with exactly: AI CoC connected", None)
