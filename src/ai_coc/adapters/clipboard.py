"""The Windows clipboard, which is how the game's own export leaves the emulator.

Clash of Clans writes its village JSON to the **Android** clipboard and offers no
other way out. MuMu mirrors that clipboard onto Windows, so this is where the
payload arrives.

Reading it on the Android side was measured and rejected: `cmd clipboard get`
answers `No shell command implementation.` on this emulator (Android 15) and
`am get-clipboard` is gone, leaving only the binder service, which would have to
be called by transaction id — a number that moves between Android releases and
fails silently when it does.

**Worth revisiting if that command comes back, but not by trying it first and
falling back to this.** Its refusal is a plain string rather than a non-zero
exit, so "did it work" would be a comparison against text nobody has seen
succeed. Three things have to be established before that path is more reliable
than this one, and every one of them fails by returning a payload that parses:

- It has to answer while Clash of Clans is in the foreground and this process is
  not, since Android 10 stopped background reads and an empty answer here means
  "nothing was copied".
- It has to carry all of a real export — 7 KB and growing — without `adb shell`
  truncating it, because a village JSON cut in half still parses and still has a
  tag on it.
- Its failure has to be distinguishable from an empty clipboard.

Until then the mirror below is the measured path, and the cost of it is one
Windows dependency this project already has.

Plain functions rather than a model, like `mapping.py`: there is no state to
carry, and the ctypes structures a model could not hold anyway.
"""

from __future__ import annotations

import time
import ctypes
from ctypes import wintypes

CF_UNICODETEXT = 13
# The clipboard takes ownership of a moveable block, not of a pointer we keep.
GMEM_MOVEABLE = 0x0002

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
_user32.SetClipboardData.restype = wintypes.HANDLE
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.GetDesktopWindow.restype = wintypes.HWND
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
_kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]


class ClipboardError(RuntimeError):
    """The clipboard could not be opened at all."""


def _opened() -> None:
    """Open the clipboard, naming an owner window.

    The owner is not decoration: opened with NULL, `EmptyClipboard` sets the
    clipboard owner to NULL and every `SetClipboardData` after it fails, so
    putting back what was there would silently leave it empty. This has no
    window of its own — the packaged build runs `--windowed` and a terminal run
    has none at all — so it borrows the desktop's, which is what a process
    without a window of its own is expected to do.
    """
    owner = _user32.GetDesktopWindow()
    for attempt in range(OPEN_TRIES):
        if _user32.OpenClipboard(owner):
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


def write_clipboard(text: str) -> None:
    """Put text back on the clipboard.

    This exists so that reading the game's export can be undone. The clipboard
    belongs to whoever is at the keyboard, and a command that empties it and
    leaves 7 KB of village JSON in its place has taken something that was not
    its to take — the export is wanted, having to copy the thing you were
    copying again is not.

    The block is allocated moveable and handed over: on success the clipboard
    owns it, so freeing it here would be freeing memory the OS is holding.
    """
    if not text:
        clear_clipboard()
        return
    buffer = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(buffer)
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
    if not handle:
        raise ClipboardError(f"配不到剪貼簿要用的記憶體：{ctypes.WinError()}")
    ctypes.memmove(_kernel32.GlobalLock(handle), buffer, size)
    _kernel32.GlobalUnlock(handle)
    _opened()
    try:
        _user32.EmptyClipboard()
        if not _user32.SetClipboardData(CF_UNICODETEXT, handle):
            _kernel32.GlobalFree(handle)
            raise ClipboardError(f"寫不回 Windows 剪貼簿：{ctypes.WinError()}")
    finally:
        _user32.CloseClipboard()
