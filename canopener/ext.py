"""Plugins: extend CANopener with REST routes, MCP tools, UI tabs and live frame consumers.

A plugin is a folder with a manifest.json ({"id", "title", "version", "description", "module": "plugin.py"})
whose module exports EXTENSION (an Extension instance) or make_extension(). Folders are searched in order
(the first plugin with an id wins):

  1. CANOPENER_PLUGIN_DIRS (separated by os.pathsep),
  2. plugins/ next to canopener.exe (from source: the repo's plugins/),
  3. the plugins/ shipped inside the release.

Plugins are trusted code with full access: only install what you reviewed. A broken plugin never stops the
app; the Plugins tab lists it with the error. See docs/PLUGINS.md.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import traceback
from pathlib import Path

from .paths import app_dir, bundle_dir

log = logging.getLogger(__name__)


class Extension:
    id: str = ""
    title: str = ""
    version: str = "0"
    description: str = ""
    kind: str = "builtin"                # builtin | plugin
    ui_dir: Path | None = None           # folder with UI scripts, served at /ui/x/<id>/
    ui_scripts: list[str] = []           # file names in ui_dir, loaded in this order
    folder: str = ""

    def setup(self, svc) -> None:
        """Once, after the Service exists. Attach state; svc.engine.add_tap(Tap(...)) for live frames."""

    def routes(self, router, svc) -> None:
        """FastAPI routes on `router`, mounted at /api/x/<id>/. Use plain def for anything slow."""

    def mcp_tools(self, mcp, svc, run) -> None:
        """@mcp.tool() functions. Read-only tools: get_/list_/... names. Writes: any other verb."""

    def shutdown(self, svc) -> None:
        """When the app stops: stop your threads."""

    def info(self) -> dict:
        return {"id": self.id, "title": self.title or self.id, "version": self.version,
                "description": self.description, "kind": self.kind, "folder": self.folder,
                "ui_scripts": [f"/ui/x/{self.id}/{s}" for s in self.ui_scripts]}


def plugin_dirs() -> list[Path]:
    extra = [Path(p) for p in os.environ.get("CANOPENER_PLUGIN_DIRS", "").split(os.pathsep) if p]
    out, seen = [], set()
    for d in extra + [app_dir() / "plugins", bundle_dir() / "plugins"]:
        try:
            r = d.resolve()
        except OSError:
            continue
        if r not in seen:
            seen.add(r)
            out.append(d)
    return out


def _trace(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)[-3:]
    return " <- ".join(f"{Path(f.filename).name}:{f.lineno} in {f.name}" for f in reversed(frames))


def _inside(folder: Path, p: Path) -> bool:
    f, r = folder.resolve(), p.resolve()
    return r == f or f in r.parents


def load_plugin(folder: Path) -> Extension:
    man = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    pid = str(man["id"]).strip()
    if not pid or not all(c.isalnum() or c in "-_" for c in pid):
        raise ValueError(f"bad plugin id {pid!r}: use letters, digits, - and _")
    path = folder / man.get("module", "plugin.py")
    if not _inside(folder, path):
        raise ValueError(f"module {man.get('module')!r} is outside the plugin folder")
    name = f"canopener_plugin_{pid.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    ext = getattr(mod, "EXTENSION", None) or mod.make_extension()
    if not isinstance(ext, Extension):
        raise TypeError("EXTENSION must be a canopener.ext.Extension")
    ext.id, ext.kind, ext.folder = pid, "plugin", str(folder)
    ext.title = ext.title or man.get("title", pid)
    ext.version = man.get("version", ext.version)
    ext.description = ext.description or man.get("description", "")
    if ext.ui_dir is None and (folder / "ui").is_dir():
        ext.ui_dir = folder / "ui"
    if ext.ui_dir is not None and not _inside(folder, Path(ext.ui_dir)):
        raise ValueError("ui_dir is outside the plugin folder")
    if ext.ui_dir is not None and not ext.ui_scripts:
        ext.ui_scripts = sorted(p.name for p in Path(ext.ui_dir).glob("*.js"))
    return ext


def load_all(builtins: list[Extension] | None = None) -> tuple[list[Extension], list[dict]]:
    """(extensions, errors). Builtins first; then plugins from plugin_dirs()."""
    exts: list[Extension] = list(builtins or [])
    errors: list[dict] = []
    ids = {e.id for e in exts}
    for d in plugin_dirs():
        if not d.is_dir():
            continue
        for folder in sorted(p for p in d.iterdir() if (p / "manifest.json").is_file()):
            try:
                ext = load_plugin(folder)
            except Exception as exc:
                log.exception("plugin %s failed to load", folder)
                errors.append({"plugin": str(folder), "error": f"{type(exc).__name__}: {exc}", "trace": _trace(exc)})
                continue
            if ext.id in ids:
                log.info("plugin %s from %s skipped: id already loaded", ext.id, folder)
                continue
            ids.add(ext.id)
            exts.append(ext)
    return exts, errors
