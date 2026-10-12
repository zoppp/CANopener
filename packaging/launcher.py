"""Entry point for the frozen exes. Double-click canopener.exe = the app window."""
import sys

if sys.stdout is None or sys.stderr is None:     # canopener.exe is windowed (no console): keep stray output in a file
    import os
    os.environ["CANOPENER_WINDOWED"] = "1"
    from canopener.paths import logs_dir
    _out = open(logs_dir() / "console.log", "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stdout or _out
    sys.stderr = sys.stderr or _out

from canopener.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
