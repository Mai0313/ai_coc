from __future__ import annotations

import os
import base64
import ctypes
from ctypes import wintypes
from pathlib import Path

from pydantic import Field, BaseModel

from ai_coc.constants import APP_NAME, data_root

# Development fallback for a machine with no key saved yet. Never written to,
# and always outranked by a key the user saved through the settings tab.
ENV_VAR = "GEMINI_API_KEY"
DOTENV_PATH = Path(".env")


class DATA_BLOB(ctypes.Structure):  # noqa: N801 - mirrors the Win32 struct name
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[DATA_BLOB, object]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def dotenv_value(name: str, path: Path = DOTENV_PATH) -> str:
    """One value out of a .env file, so a checkout can run without saving a key first."""
    if not path.is_file():
        return ""
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == name:
            # Quoting a value is ordinary .env syntax, and the quotes are not
            # part of the key: left on, they only surface as a failed request.
            return value.strip().strip("\"'")
    return ""


class SecretStore(BaseModel):
    path: Path = Field(default_factory=lambda: data_root() / "gemini.key.dpapi")

    def save(self, value: str) -> None:
        source, _keep = _blob(value.encode("utf-8"))
        output = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(source), APP_NAME, None, None, None, 0, ctypes.byref(output)
        ):
            raise ctypes.WinError()
        try:
            encrypted = ctypes.string_at(output.pbData, output.cbData)
            self.path.write_bytes(base64.b64encode(encrypted))
        finally:
            ctypes.windll.kernel32.LocalFree(output.pbData)

    def load(self) -> str:
        """The saved key, or the development fallback when nothing has been saved."""
        if not self.path.is_file():
            return os.environ.get(ENV_VAR, "") or dotenv_value(ENV_VAR)
        encrypted = base64.b64decode(self.path.read_bytes())
        source, _keep = _blob(encrypted)
        output = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)
        ):
            raise ctypes.WinError()
        try:
            return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
        finally:
            ctypes.windll.kernel32.LocalFree(output.pbData)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()
