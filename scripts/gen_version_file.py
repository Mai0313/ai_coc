# /// script
# requires-python = ">=3.12"
# ///
"""Generate the Windows version resource that PyInstaller stamps into the executable.

Generated rather than committed: the version comes from the git tag at build time,
so a checked-in copy could only ever go stale.
"""

import re
from pathlib import Path

from ai_coc import package_name, __version__
from ai_coc.constants import APP_NAME

OUTPUT = Path(__file__).resolve().parents[1] / "version_info.txt"

TEMPLATE = """VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0,0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'Hsien0818666'),
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
    # Windows wants exactly four integers, so pad; a dev version such as
    # 0.2.3.dev2 already carries four and keeps its build number.
    found = [int(number) for number in re.findall(r"\d+", __version__)]
    numbers = tuple((found + [0, 0, 0, 0])[:4])
    OUTPUT.write_text(
        TEMPLATE.format(
            numbers=numbers, version=__version__, name=APP_NAME, package=package_name
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
