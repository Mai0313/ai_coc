from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from pathlib import Path

from .constants import data_root


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[DATA_BLOB, object]:
    buffer = ctypes.create_string_buffer(data)
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


class SecretStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_root() / "gemini.key.dpapi"

    def save(self, value: str) -> None:
        source, keep = _blob(value.encode("utf-8"))
        output = DATA_BLOB()
        if not ctypes.windll.crypt32.CryptProtectData(
            ctypes.byref(source), "CoC AI Controller", None, None, None, 0, ctypes.byref(output)
        ):
            raise ctypes.WinError()
        try:
            encrypted = ctypes.string_at(output.pbData, output.cbData)
            self.path.write_bytes(base64.b64encode(encrypted))
        finally:
            ctypes.windll.kernel32.LocalFree(output.pbData)

    def load(self) -> str:
        if not self.path.is_file():
            return ""
        encrypted = base64.b64decode(self.path.read_bytes())
        source, keep = _blob(encrypted)
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
