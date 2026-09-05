"""The Windows clipboard, which is how the game's own export leaves the emulator.

Clash of Clans writes its village JSON to the **Android** clipboard and offers no
other way out. MuMu mirrors that clipboard onto Windows, so this is where the
payload arrives.

Reading it on the Android side was measured and rejected: `cmd clipboard get`
has no shell implementation on this emulator (Android 15), leaving only the
binder service, which would have to be called by transaction id — a number that
moves between Android releases and fails silently when it does.

Plain functions rather than a model, like `mapping.py`: there is no state to
carry, and the ctypes structures a model could not hold anyway.
"""

from __future__ import annotations

import time
import ctypes
from ctypes import wintypes

CF_UNICODETEXT = 13

# How long to keep trying when another process holds the clipboard open. It is a
# single global lock on Windows, so a clipboard manager or the emulator itself
# mirroring a payload across can hold it for a moment.
OPEN_TRIES = 10
OPEN_GAP = 0.1

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# Without these the handles come back through the default `c_int` and are
# truncated to 32 bits, which on a 64-bit build is a pointer that reads as
# garbage rather than as a failure.
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
_kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]


class ClipboardError(RuntimeError):
    """The clipboard could not be opened at all."""


def _opened() -> None:
    for attempt in range(OPEN_TRIES):
        if _user32.OpenClipboard(None):
            return
        if attempt < OPEN_TRIES - 1:
            time.sleep(OPEN_GAP)
    raise ClipboardError(f"開不了 Windows 剪貼簿：{ctypes.WinError()}")


def read_clipboard() -> str:
    """Whatever text is on the clipboard, or an empty string when there is none.

    Empty is a normal answer rather than a failure: it is what `clear_clipboard`
    leaves behind, and telling the two apart is how a caller knows whether the
    thing it just tapped actually copied anything.
    """
    _opened()
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = _kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.c_wchar_p(pointer).value or ""
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def clear_clipboard() -> None:
    """Empty the clipboard, so that what turns up next is known to be new."""
    _opened()
    try:
        _user32.EmptyClipboard()
    finally:
        _user32.CloseClipboard()
