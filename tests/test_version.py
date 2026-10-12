import re

import canopener


def test_version_is_a_date_code():
    """YYYY.MM.DD.HHMM, stamped by tools/bump_version.py (the PLC projects' scheme)."""
    assert re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\.\d{4}", canopener.__version__)


def _bump():
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parent.parent / "tools" / "bump_version.py"
    spec = importlib.util.spec_from_file_location("bump_version", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_changelog_has_an_unreleased_section_first():
    from pathlib import Path
    text = (Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = [l for l in text.splitlines() if l.startswith("## ")]
    assert sections[0] == "## Unreleased"
    assert all(re.fullmatch(r"## (\d{4}\.\d{2}\.\d{2}\.\d{4}|\d+\.\d+\.\d+) - \d{4}-\d{2}-\d{2}", s) for s in sections[1:])


def test_bump_stamps_the_changelog():
    import pytest
    b = _bump()
    text = "# Changelog\n\n## Unreleased\n\n### Added\n\n- a thing\n\n## 2026.01.01.0000 - 2026-01-01\n\n- old\n"
    out = b.stamp_changelog(text, "2026.02.03.0405", "2026-02-03")
    assert out.split("\n")[2:5] == ["## Unreleased", "", "## 2026.02.03.0405 - 2026-02-03"]
    assert b.section(out, "2026.02.03.0405").strip() == "### Added\n\n- a thing"
    assert b.section(out, "2026.01.01.0000").strip() == "- old"
    with pytest.raises(ValueError, match="empty"):
        b.stamp_changelog(out, "2026.02.04.0000", "2026-02-04")       # nothing new since the last release
    with pytest.raises(ValueError, match="already"):
        b.stamp_changelog(text.replace("2026.01.01.0000", "2026.02.03.0405"), "2026.02.03.0405", "x")
