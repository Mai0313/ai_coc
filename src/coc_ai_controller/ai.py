from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any


class AIProvider(ABC):
    @abstractmethod
    def generate(self, prompt: str, image_png: bytes | None = None) -> str: ...


class GoogleGeminiProvider(AIProvider):
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash", base_url: str = "") -> None:
        self.api_key = api_key.strip()
        self.model = model.strip() or "gemini-2.5-flash"
        self.base_url = base_url.strip().rstrip("/")
        self.last_model = self.model

    @property
    def openai_compatible(self) -> bool:
        return "/openai" in self.base_url.lower()

    def _openai_request(self, path: str, payload: dict | None = None, timeout: int | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(  # noqa: S310 - endpoint is an HTTPS API configured by the user
            self.base_url + path,
            data=data,
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="GET" if payload is None else "POST",
        )
        request_timeout = timeout or (180 if payload is not None else 30)
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=request_timeout) as response:  # noqa: S310
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:1200]
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2 + attempt * 2)
                    continue
                raise RuntimeError(f"Gemini OpenAI-compatible HTTP {exc.code}: {detail}") from exc
            except (TimeoutError, urllib.error.URLError) as exc:
                if attempt < 2:
                    time.sleep(2 + attempt * 2)
                    continue
                raise RuntimeError("Gemini 等待超過限制，已自動重試 3 次。請稍後再試。") from exc
        raise RuntimeError("Gemini 請求失敗")

    def list_models(self) -> list[dict[str, Any]]:
        if not self.api_key:
            raise ValueError("尚未設定 Gemini API Key")
        if self.openai_compatible:
            return [
                {"name": item.get("id", ""), "supportedGenerationMethods": ["generateContent"]}
                for item in self._openai_request("/models").get("data", [])
            ]
        key = urllib.parse.quote(self.api_key, safe="")
        request = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={key}",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - fixed Google HTTPS endpoint
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"Gemini model list HTTP {exc.code}: {detail}") from exc
        return [
            item for item in raw.get("models", []) if "generateContent" in item.get("supportedGenerationMethods", [])
        ]

    def _generate_once(self, model_name: str, prompt: str, image_png: bytes | None) -> str:
        parts: list[dict[str, Any]] = [{"text": prompt}]
        if image_png:
            parts.append(
                {"inline_data": {"mime_type": "image/png", "data": base64.b64encode(image_png).decode("ascii")}}
            )
        payload = json.dumps(
            {"contents": [{"role": "user", "parts": parts}], "generationConfig": {"temperature": 0.2}}
        ).encode("utf-8")
        model = urllib.parse.quote(model_name, safe="-._")
        key = urllib.parse.quote(self.api_key, safe="")
        request = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=45) as response:  # noqa: S310 - fixed Google HTTPS endpoint
            raw = json.loads(response.read().decode("utf-8"))
        candidates = raw.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"Gemini 沒有回傳內容：{raw}")
        return "\n".join(part.get("text", "") for part in candidates[0].get("content", {}).get("parts", [])).strip()

    def generate(self, prompt: str, image_png: bytes | None = None) -> str:
        if not self.api_key:
            raise ValueError("尚未設定 Gemini API Key")
        if self.openai_compatible:
            # CC Switch's compatibility endpoint may expose different model IDs
            # than Google's native endpoint. Resolve a text-capable model first.
            available = self.list_models()
            names = [str(item.get("name", "")).removeprefix("models/") for item in available]
            text_names = [n for n in names if n and not any(tag in n.lower() for tag in ("tts", "audio", "speech"))]
            if text_names and self.model not in text_names:
                preferred = [n for n in text_names if "flash" in n.lower()] or text_names
                self.model = preferred[0]
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            if image_png:
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64," + base64.b64encode(image_png).decode("ascii")},
                    }
                )
            payload = {"model": self.model, "messages": [{"role": "user", "content": content}], "temperature": 0.2}
            try:
                raw = self._openai_request("/chat/completions", payload)
            except RuntimeError as exc:
                if "HTTP 404" not in str(exc):
                    if "HTTP 503" not in str(exc) and "HTTP 400" not in str(exc):
                        raise
                available = self.list_models()
                names = [str(item.get("name", "")).removeprefix("models/") for item in available]
                text_names = [n for n in names if n and not any(tag in n.lower() for tag in ("tts", "audio", "speech"))]
                preferred = [n for n in text_names if "flash" in n.lower()] or text_names
                fallback = next((n for n in preferred if n != self.model), None)
                if not fallback:
                    raise RuntimeError(
                        f"模型 {self.model} 不存在，且端點沒有回傳可用模型。請在 Settings 填入 CC Switch 顯示的模型名稱。"
                    ) from exc
                payload["model"] = fallback
                raw = self._openai_request("/chat/completions", payload)
                self.last_model = fallback
                prefix = f"[自動改用端點可用模型：{fallback}]\n"
            else:
                prefix = ""
            choices = raw.get("choices") or []
            if not choices:
                raise RuntimeError(f"Gemini OpenAI-compatible 沒有回傳內容：{raw}")
            self.last_model = payload["model"]
            return prefix + str(choices[0].get("message", {}).get("content", "")).strip()
        try:
            result = self._generate_once(self.model, prompt, image_png)
            self.last_model = self.model
            return result
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            if exc.code != 404:
                raise RuntimeError(f"Gemini API HTTP {exc.code}: {detail}") from exc
            models = self.list_models()
            preferred = ("gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash")
            candidates = [str(item.get("name", "")).removeprefix("models/") for item in models]
            fallback = next((name for name in preferred if name in candidates), None) or (
                candidates[0] if candidates else None
            )
            if not fallback:
                raise RuntimeError(f"指定模型 {self.model} 不可用，且 API 沒有可用 generateContent 模型。") from exc
            result = self._generate_once(fallback, prompt, image_png)
            self.last_model = fallback
            return f"[自動改用可用模型：{fallback}]\n{result}"

    def test(self) -> str:
        return self.generate("Reply with exactly: CoC AI Controller connected")


AGENT_PROFILE = """You are the CoC AI Controller general operator. You understand Clash of Clans screens, account state, UI, buildings, troops and heroes. You navigate, prepare armies, search opponents, verify actions, teach and discover. You do not perform live battle tactics; at Enemy Preview you reserve handoff to an RL battle controller. Never invent account or master-data facts. For proposed actions include emulator_id and frame_id and explain the verification condition. Treat user teaching as USER_CONFIRMED knowledge."""


def vision_prompt(emulator_id: str, frame_id: str, account_context: str = "") -> str:
    return f"""{AGENT_PROFILE}
Analyze the attached current screenshot semantically. Return concise JSON with keys emulator_id, frame_id, world, screen, objects, dialogs, possible_actions, confidence, needs_user_help. Do not rely on template similarity.
emulator_id={emulator_id}
frame_id={frame_id}
Known account context:
{account_context[:12000]}"""
