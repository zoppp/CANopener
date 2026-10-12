r"""Stamp the version: YYYY.MM.DD.HHMM (local time), the same date-code scheme as PLC projects.

    .venv\Scripts\python tools\bump_version.py            # now
    .venv\Scripts\python tools\bump_version.py 2026.10.11.0915

The version lives in canopener/__init__.py only (pyproject.toml reads it from there). The "## Unreleased"
section of CHANGELOG.md becomes "## <version> - <date>", under a new empty "## Unreleased"; a release needs
notes, so an empty Unreleased section is refused. Release: bump, commit "Release <version>", tag v<version>, push
the tag (CI builds the Windows zip and uses the changelog section as the release notes).
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INIT = ROOT / "canopener" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
UNRELEASED = "## Unreleased"


def stamp_changelog(text: str, version: str, date: str) -> str:
    """Turn the Unreleased section into the version's section (raises ValueError when it is missing or empty)."""
    lines = text.split("\n")
    try:
        at = next(i for i, l in enumerate(lines) if l.strip() == UNRELEASED)
    except StopIteration:
        raise ValueError(f"CHANGELOG.md has no '{UNRELEASED}' section") from None
    end = next((i for i in range(at + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    if not any(l.strip() and not l.startswith("### ") for l in lines[at + 1:end]):
        raise ValueError(f"the '{UNRELEASED}' section of CHANGELOG.md is empty: write the release notes first")
    if any(l.startswith(f"## {version} ") or l.strip() == f"## {version}" for l in lines):
        raise ValueError(f"CHANGELOG.md already has a section for {version}")
    return "\n".join(lines[:at] + [UNRELEASED, "", f"## {version} - {date}"] + lines[at + 1:])


def section(text: str, version: str) -> str:
    """The notes of one version (CI uses them as the GitHub release text)."""
    lines = text.split("\n")
    at = next((i for i, l in enumerate(lines) if l.startswith(f"## {version} ") or l.strip() == f"## {version}"), None)
    if at is None:
        raise ValueError(f"CHANGELOG.md has no section for {version}")
    end = next((i for i in range(at + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "\n".join(lines[at + 1:end]).strip() + "\n"


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--notes":         # print a version's notes (CI)
        try:
            sys.stdout.write(section(CHANGELOG.read_text(encoding="utf-8"), sys.argv[2]))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    version = sys.argv[1] if len(sys.argv) > 1 else time.strftime("%Y.%m.%d.%H%M")
    if not re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\.\d{4}", version):
        print(f"error: {version!r} is not YYYY.MM.DD.HHMM")
        return 2
    text = INIT.read_text(encoding="utf-8")
    new, n = re.subn(r'^__version__ = "[^"]*"', f'__version__ = "{version}"', text, flags=re.M)
    if n != 1:
        print(f"error: no __version__ line in {INIT}")
        return 1
    try:
        log = stamp_changelog(CHANGELOG.read_text(encoding="utf-8"), version,
                              f"{version[0:4]}-{version[5:7]}-{version[8:10]}")
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    INIT.write_text(new, encoding="utf-8")
    CHANGELOG.write_text(log, encoding="utf-8")
    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
