# Python — Google Style Docstrings

The Google Python Style Guide is the source of truth: <https://google.github.io/styleguide/pyguide.html#38-comments-and-docstrings>. This page is a working summary, not a replacement.

Examples below show **structure**. They are not a length target; keep real docstrings to what `SKILL.md` describes.

## What every public function gets

A docstring sits **inside** the function body as its first statement, in triple double-quotes:

1. **Summary line** — one line, ending with a period. It sits on the same physical line as the opening `"""` when it fits; otherwise the opening `"""` stands alone and the summary follows on the next line.
2. **Extended description** (optional) — paragraphs after a blank line.
3. **Sections** in this order, each a `:` heading with an indented body. Only the ones that apply.
    - `Args:` — one entry per parameter, skipping `self` and `cls`.
    - `Returns:` — skip it when the function returns `None`.
    - `Yields:` — for generators, *instead of* `Returns:`.
    - `Raises:` — exceptions callers should be ready for.

Indent section bodies one level, matching the file. Long descriptions wrap with a hanging indent lined up with the start of the description, not with the argument name.

### Local deviation: always keep `name (Type):`

Upstream says the type may be omitted when the signature already carries an annotation. **We keep it anyway.** Write `name (Type): description` in `Args:` and `Attributes:`, copying the annotation from the signature verbatim (Rule 3 in `SKILL.md`). This is a deliberate departure from the style guide, not an oversight, so do not "correct" it back.

Where the signature has no annotation, leave the type out rather than inventing one.

## Worked example — function

```python
def fetch_rows(
    table: smalltable.Table, keys: Sequence[bytes | str], require_all: bool = False
) -> Mapping[bytes, tuple[str, ...]]:
    """Fetches rows from a Smalltable.

    String keys are UTF-8 encoded before lookup. Returned keys are always
    bytes; a key missing from the result was not found in the table.

    Args:
        table (smalltable.Table): An open table instance.
        keys (Sequence[bytes | str]): Keys for each row to fetch.
        require_all (bool): If True, raise unless every key resolves.

    Returns:
        A mapping of key to row data, each row a tuple of column strings.

    Raises:
        IOError: The table could not be reached.
    """
```

Note what is absent. Nothing restates the signature, and the prose covers only what a caller cannot see from it.

## Class

```python
class Cache:
    """An in-memory cache of User records, keyed by id.

    Not thread-safe; callers sharing an instance must synchronise.

    Attributes:
        max_entries (int): Entries kept before eviction begins.
    """

    def __init__(self, max_entries: int = 128) -> None:
        """Creates an empty cache.

        Args:
            max_entries (int): Entries kept before eviction begins.
        """
```

`__init__` is documented separately: the class docstring describes the class, `__init__` describes construction.

## Generator

```python
def stream_lines(path: str) -> Iterator[str]:
    """Yields lines from a file with trailing newlines stripped.

    Args:
        path (str): Filesystem path to read from.

    Yields:
        Each line of the file, in order.

    Raises:
        FileNotFoundError: `path` does not exist.
    """
```

Use `Yields:` for generators, never both `Yields:` and `Returns:`.

## Module

```python
"""Utilities for talking to the Smalltable service."""
```

Module docstrings go at the very top of the file, before imports. Add a usage block only when the module's entry point is not obvious.

## Private helpers and tests

A private helper gets one line, as a docstring inside the body. No `Args:` or `Returns:`.

```python
def _normalise_email(s: str) -> str:
    """Lowercases the local part and strips surrounding whitespace."""
```

A test gets one line naming the behaviour verified, not the mechanics.

```python
def test_fetch_returns_empty_dict_on_missing_keys():
    """Missing keys produce an empty dict rather than an error."""
```

## Idioms and pitfalls

- **Examples inside `Returns:`** must be indented into a code block. Sphinx and Google's parser both rely on that indent.
- **`@overload` stubs** get a one-line summary of which overload they cover; the implementation carries the full docstring.
- **`async def`** documents identically to `def`. Describe what awaiting it produces; there is no `Coroutine:` section.
- **Escapes in a docstring are live.** A docstring is not a raw string, so `\n` written in prose inserts a newline. Use `\\n`, a raw docstring, or the word "newline".

## What "stale" looks like

- An `Args:` name that no longer matches the signature, or a parameter since removed.
- A type in `Args:` that has drifted from the annotation, most often a dropped `| None`.
- `Returns:` describing a different type than the annotation.
- `Raises:` listing exceptions no longer raised, or missing ones now raised.
- Example code calling an old API shape.

Fix any of these by rewriting the whole docstring to the current state. Half-old, half-new is worse than stale, because it looks maintained.
