"""Every prompt the application sends, as Markdown files rather than string literals.

A prompt is content, not code: it gets read, argued over and reworded far more
often than the code around it, and a wall of quoted Chinese wedged into a module
hides both. Keeping them here means a change to what the model is told shows up
as a diff to one readable file.

Each file is a `str.format` template, so a literal brace in a prompt has to be
doubled. `PROMPTS` reads them once at import; `render` fills one in.
"""

from __future__ import annotations

from pathlib import Path

# Beside this module rather than through importlib.resources: PyInstaller lays
# the bundle out as a directory tree, so `--add-data` puts these files exactly
# here and a path relative to the module finds them frozen or not.
PROMPT_DIR = Path(__file__).parent

PROMPTS: dict[str, str] = {
    path.stem: path.read_text(encoding="utf-8").strip() for path in sorted(PROMPT_DIR.glob("*.md"))
}

if not PROMPTS:
    # Every AI call in the application reads one of these, and the first read is
    # at import time, so an empty directory takes the whole app down. Saying why
    # here beats a `KeyError` on a dictionary lookup three modules away: what it
    # means in practice is a PyInstaller build whose `--add-data` line is missing.
    raise RuntimeError(f"找不到任何 prompt 檔案：{PROMPT_DIR}（打包時可能漏掉 --add-data）")


def render(name: str, **values: object) -> str:
    """One prompt with its placeholders filled in."""
    return PROMPTS[name].format(**values)
