import re

import canopener


def test_version_is_a_date_code():
    """YYYY.MM.DD.HHMM, stamped by tools/bump_version.py (the PLC projects' scheme)."""
    assert re.fullmatch(r"\d{4}\.\d{2}\.\d{2}\.\d{4}", canopener.__version__)
