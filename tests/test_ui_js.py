"""The UI scripts parse (node --check) and the DOM-free helpers of core.js work (tests/js/core_test.js).
Needs Node.js; skipped without it."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
SCRIPTS = sorted((ROOT / "canopener" / "ui" / "js").glob("*.js")) + sorted(ROOT.glob("plugins/*/ui/*.js"))

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is not installed")


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_syntax(script):
    r = subprocess.run([NODE, "--check", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_core_helpers():
    r = subprocess.run([NODE, str(ROOT / "tests" / "js" / "core_test.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
