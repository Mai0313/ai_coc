"""Generate the Windows version resource that PyInstaller stamps into the executable.

Generated rather than committed: the version comes from the git tag at build time,
so a checked-in copy could only ever go stale. Needs the project installed, which
is why it carries no PEP 723 metadata block: run it as `uv run python <path>`.
"""

import re
import sys
from pathlib import Path

from ai_coc import __version__, package_name
from ai_coc.constants import APP_NAME, ORGANISATION

OUTPUT = Path(__file__).resolve().parents[1] / "version_info.txt"

TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0,0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', '{organisation}'),
    StringStruct('FileDescription', '{name}'),
    StringStruct('FileVersion', '{version}'),
    StringStruct('InternalName', '{package}'),
    StringStruct('OriginalFilename', '{package}.exe'),
    StringStruct('ProductName', '{name}'),
    StringStruct('ProductVersion', '{version}')
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"""


def main() -> None:
    # Take the version CI resolved from the tag. Reading it back from the installed
    # metadata instead would stamp pyproject's placeholder without a word whenever
    # the sync that rewrites that metadata did not happen.
    version = sys.argv[1] if len(sys.argv) > 1 else __version__
    # Windows wants exactly four integers, so pad; a dev version such as
    # 0.2.3.dev2 already carries four and keeps its build number.
    found = [int(number) for number in re.findall(r"\d+", version)]
    numbers = tuple(([*found, 0, 0, 0, 0])[:4])
    OUTPUT.write_text(
        TEMPLATE.format(
            numbers=numbers,
            version=version,
            name=APP_NAME,
            package=package_name,
            organisation=ORGANISATION,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
