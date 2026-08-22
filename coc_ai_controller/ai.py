from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from abc import ABC, abstractmethod
from typing import Any


class AIProvider(ABC):
    @abstractmethod
    def generate(self, prompt: str, image_png: bytes | None = None) -> str: ...


class GoogleGeminiProvider(AIProvider):
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash") -> None:
        self.api_key = api_key.strip()
        self.model = model.strip() or "gemini-2.5-flash"

    def generate(self, prompt: str, image_png: bytes | None = None) -> str:
        if not self.api_key:
            raise ValueError("尚未設定 Gemini API Key")
        parts: list[dict[str, Any]] = [{"text": prompt}]
        if image_png:
            parts.append({"inline_data": {"mime_type": "image/png", "data": base64.b64encode(image_png).decode("ascii")}})
        payload = json.dumps({"contents": [{"role": "user", "parts": parts}], "generationConfig": {"temperature": 0.2}}).encode("utf-8")
        model = urllib.parse.quote(self.model, safe="-._")
        key = urllib.parse.quote(self.api_key, safe="")
        request = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
            data=payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"Gemini API HTTP {exc.code}: {detail}") from exc
        candidates = raw.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"Gemini 沒有回傳內容：{raw}")
        return "\n".join(part.get("text", "") for part in candidates[0].get("content", {}).get("parts", [])).strip()

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
