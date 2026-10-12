"""Where things live, from source and when frozen by PyInstaller.

app_dir:    the folder the user sees (repo root from source; the exe's folder when frozen): logs, recordings,
            user recipes, plugins.
bundle_dir: read-only files shipped with the program (repo root, or PyInstaller's _internal).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)


def app_dir() -> Path:
    if os.environ.get("CANOPENER_HOME"):
        return Path(os.environ["CANOPENER_HOME"])
    return Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent


def bundle_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def package_dir() -> Path:
    """The canopener package folder (ui/, recipes.json): inside _internal when frozen."""
    return bundle_dir() / "canopener" if FROZEN else Path(__file__).resolve().parent


def logs_dir() -> Path:
    d = app_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def recordings_dir() -> Path:
    d = app_dir() / "recordings"
    d.mkdir(parents=True, exist_ok=True)
    return d
