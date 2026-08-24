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


def render(name: str, **values: object) -> str:
    """One prompt with its placeholders filled in."""
    return PROMPTS[name].format(**values)
