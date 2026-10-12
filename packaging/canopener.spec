# PyInstaller spec: one-folder Windows build of CANopener (run packaging\build.ps1).
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules

ROOT = Path(SPECPATH).parent

hidden = (
    collect_submodules("uvicorn")
    + collect_submodules("mcp", filter=lambda n: not n.startswith("mcp.cli"))
    + collect_submodules("canopen")
    + collect_submodules("cantools", filter=lambda n: not n.startswith("cantools.subparsers"))   # DBC decoding
    + collect_submodules("canopener")
    # pywebview picks its backend at run time: WinForms + Edge WebView2 (pythonnet)
    + ["webview.platforms.winforms", "webview.platforms.edgechromium", "webview.platforms.mshtml",
       "webview.platforms.win32", "clr", "clr_loader"]
    # python-can backends are imported by name when a channel opens
    + ["can.interfaces.pcan", "can.interfaces.pcan.pcan", "can.interfaces.pcan.basic",
       "can.interfaces.kvaser", "can.interfaces.kvaser.canlib",
       "can.interfaces.slcan", "can.interfaces.gs_usb", "can.interfaces.virtual",
       "can.interfaces.vector", "can.interfaces.vector.canlib",
       "can.interfaces.ixxat", "can.interfaces.ixxat.canlib",
       "can.io.asc", "can.io.blf", "can.io.canutils", "can.io.csv", "can.io.trc",
       "serial", "serial.tools.list_ports", "serial.tools.list_ports_windows",
       "usb", "usb.backend.libusb1", "gs_usb", "libusb_package"]
)
datas = (
    [(str(ROOT / "canopener" / "ui"), "canopener/ui"),
     (str(ROOT / "canopener" / "nodes" / "recipes.json"), "canopener/nodes")]
    + ([(str(ROOT / "canopener" / "nodes" / "recipes-private.json"), "canopener/nodes")]
       if (ROOT / "canopener" / "nodes" / "recipes-private.json").is_file() else [])
    + collect_data_files("mcp")
    + collect_data_files("libusb_package")
)
binaries = collect_dynamic_libs("libusb_package")
if (ROOT / "packaging" / "build_info.json").is_file():     # written by build.ps1 (About box)
    datas.append((str(ROOT / "packaging" / "build_info.json"), "."))
if (ROOT / "plugins").is_dir():
    datas.append((str(ROOT / "plugins"), "plugins"))
if (ROOT / "docs").is_dir():
    datas.append((str(ROOT / "docs"), "docs"))

a = Analysis(
    [str(ROOT / "packaging" / "launcher.py")],
    pathex=[str(ROOT)],
    hiddenimports=hidden,
    datas=datas,
    binaries=binaries,
    excludes=["tkinter", "matplotlib", "numpy", "pytest", "pandas"],
)
pyz = PYZ(a.pure)
# Two exes over one _internal: canopener.exe (no console; double-click opens the app window) and
# canopener-cli.exe (console; CLI commands, serve, troubleshooting).
icon = str(ROOT / "packaging" / "canopener.ico") if (ROOT / "packaging" / "canopener.ico").is_file() else None
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="canopener", console=False, icon=icon)
exe_cli = EXE(pyz, a.scripts, [], exclude_binaries=True, name="canopener-cli", console=True, icon=icon)
coll = COLLECT(exe, exe_cli, a.binaries, a.datas, name="canopener")
