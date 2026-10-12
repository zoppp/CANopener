"""Stamp the version: YYYY.MM.DD.HHMM (local time), the same date-code scheme as PLC projects.

    .venv\Scripts\python tools\bump_version.py            # now
    .venv\Scripts\python tools\bump_version.py 2026.10.11.0915

The version lives in canopener/__init__.py only (pyproject.toml reads it from there). Release: bump, commit
"Release <version>", tag v<version>, push the tag (CI builds the Windows zip).
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

INIT = Path(__file__).resolve().parent.parent / "canopener" / "__init__.py"


def main() -> int:
    version = sys.argv[1] if len(sys.argv) > 1 else time.strftime("%Y.%m.%d.%H%M")
    if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\.\d{4}", version):
        print(f"error: {version!r} is not YYYY.MM.DD.HHMM")
        return 2
    text = INIT.read_text(encoding="utf-8")
    new, n = re.subn(r'^__version__ = "[^"]*"', f'__version__ = "{version}"', text, flags=re.M)
    if n != 1:
        print(f"error: no __version__ line in {INIT}")
        return 1
    INIT.write_text(new, encoding="utf-8")
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
