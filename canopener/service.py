"""The app service: one Engine, the node workspace, the plugins, settings and projects.

The server, the MCP tools and plugins all receive this object (`svc`).
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Optional

from . import __version__
from .core import adapters
from .core.adapters import ChannelConfig
from .core.engine import Engine, EngineError
from .core.transmit import TxEntry
from .decode.manager import DecodeManager
from .decode.signals import SignalStore
from .ext import Extension, load_all
from .nodes.service import NodeService
from .paths import FROZEN, app_dir, bundle_dir, recordings_dir

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[\w .()-]{1,80}$")


class ServiceError(Exception):
    """A request refused with a message for the user (HTTP 400, MCP {"error": ...})."""


def safe_name(name: str, what: str = "name") -> str:
    name = (name or "").strip()
    if not _NAME_RE.match(name) or name.startswith(".") or ".." in name:
        raise ServiceError(f"invalid {what} {name!r}: use letters, digits, space, . _ - ( )")
    return name


def build_info() -> dict:
    info = {"app": "canopener", "version": __version__, "frozen": FROZEN}
    p = bundle_dir() / "build_info.json"
    if p.is_file():
        try:
            info.update(json.loads(p.read_text(encoding="utf-8-sig")))   # build.ps1 writes a BOM
        except (OSError, ValueError):
            pass
    return info


class Service:
    def __init__(self, engine: Optional[Engine] = None, *, load_plugins: bool = True,
                 builtins: Optional[list[Extension]] = None):
        self.engine = engine or Engine()
        self.nodes = NodeService(self.engine)
        self.signals = SignalStore()               # decoded values (decoders publish, dashboards/MCP read)
        self.settings_path = app_dir() / "settings.json"
        self.projects_dir = app_dir() / "projects"
        self.settings: dict = self._load_settings()
        from .dash import dashboards as dash_mod, raw_signals as raw_mod   # (they import this module)
        for key, clean in (("dashboards", dash_mod.clean_list), ("raw_signals", raw_mod.clean_list)):
            if key in self.settings:               # a hand-edited or damaged settings.json must not stop the app
                self.settings[key] = clean(self.settings[key])
        self._save_timer: Optional[threading.Timer] = None
        self.sim = None                            # canopener.sim.SimBus while simulated devices run
        self.save_on_exit = True                   # False for an instance whose server never started
        self._shut = False
        self.decode = DecodeManager(self)          # decoders; before the transmit list (DBC entries encode)
        from .dash.raw_signals import RawDecoder   # here: that module imports ServiceError from this one
        self.raw = RawDecoder(self)                # quick signals on raw frames (dashboards)
        self.raw.start()
        self._restore_tx()
        if load_plugins:
            self.extensions, self.extension_errors = load_all(builtins)
        else:
            self.extensions, self.extension_errors = list(builtins or []), []
        for ext in list(self.extensions):
            try:
                ext.setup(self)
            except Exception as exc:
                log.exception("extension %s setup failed", ext.id)
                self.extension_errors.append({"plugin": ext.id, "error": f"setup: {type(exc).__name__}: {exc}"})
                self.extensions.remove(ext)

    # ------------------------------------------------------------------ about / status
    def about(self) -> dict:
        return {**build_info(), "app_dir": str(app_dir()),
                "extensions": [e.info() for e in self.extensions], "extension_errors": self.extension_errors}

    def status(self) -> dict:
        st = self.engine.status()
        st["node_job"] = self.nodes.job.as_dict() if self.nodes.job else None
        return st

    # ------------------------------------------------------------------ settings
    def _load_settings(self) -> dict:
        try:
            return json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save_settings(self, delay: float = 1.0) -> None:
        """Persist settings soon (debounced): last connections, transmit list, UI preferences."""
        if self._save_timer is not None:
            self._save_timer.cancel()
        self._save_timer = threading.Timer(delay, self._write_settings)
        self._save_timer.daemon = True
        self._save_timer.start()

    def _write_settings(self) -> None:
        self.settings["tx"] = [self._tx_dict(e) for e in self.engine.tx.entries]
        try:
            self.settings_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.settings_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.settings, indent=2), encoding="utf-8")
            tmp.replace(self.settings_path)
        except OSError:
            log.exception("cannot save settings")

    @staticmethod
    def _tx_dict(e: TxEntry) -> dict:
        d = e.as_dict()
        for k in ("id", "count", "error"):
            d.pop(k, None)
        d["enabled"] = False                       # never start sending by itself after a restart
        return d

    def _restore_tx(self) -> None:
        entries = []
        for d in self.settings.get("tx", []):
            try:
                e = TxEntry.from_dict(d)
                e.enabled = False
                e.message()
                entries.append(e)
            except Exception:
                log.warning("dropped a saved transmit entry: %r", d)
        self.engine.tx.replace_all(entries)

    def set_ui_settings(self, ui: dict) -> dict:
        self.settings["ui"] = {**self.settings.get("ui", {}), **(ui or {})}
        self.save_settings()
        return self.settings["ui"]

    # ------------------------------------------------------------------ channels
    def connect(self, body: dict) -> dict:
        cfg = ChannelConfig.from_dict(body)
        if cfg.interface not in adapters.BACKENDS:
            raise ServiceError(f"unknown interface {cfg.interface!r}")
        index = body.get("index")
        try:
            ch = self.engine.connect(cfg, None if index in (None, "") else int(index), name=body.get("name", ""))
        except EngineError as exc:
            raise ServiceError(str(exc)) from exc
        last = [c for c in self.settings.get("connections", []) if c.get("index") != ch.index]
        self.settings["connections"] = sorted(last + [{**cfg.as_dict(), "index": ch.index}],
                                              key=lambda c: c["index"])
        self.save_settings()
        return ch.stats()

    def disconnect(self, index: int) -> None:
        self.engine.disconnect(int(index))

    # ------------------------------------------------------------------ simulator
    def start_sim(self, channel: str = "canopener") -> dict:
        from .sim import SimBus
        if self.sim is not None:
            if self.sim.channel == channel:
                return self.sim_status()
            self.stop_sim()
        self.sim = SimBus(channel)
        self.engine.event(f"simulated devices started on virtual bus '{channel}' "
                          f"(nodes {', '.join(str(n.node_id) for n in self.sim.nodes)})")
        return self.sim_status()

    def stop_sim(self) -> None:
        if self.sim is not None:
            self.sim.stop()
            self.sim = None
            self.engine.event("simulated devices stopped")

    def sim_status(self) -> dict:
        if self.sim is None:
            return {"running": False}
        return {"running": True, "channel": self.sim.channel,
                "nodes": [{"node_id": n.node_id, "name": n.name, "vendor_id": f"0x{n.vendor:X}"} for n in self.sim.nodes]}

    # ------------------------------------------------------------------ transmit
    def tx_add(self, body: dict) -> dict:
        try:
            e = self.engine.tx.add(TxEntry.from_dict(body))
        except (ValueError, KeyError) as exc:
            raise ServiceError(str(exc)) from exc
        self.save_settings()
        return e.as_dict()

    def tx_update(self, entry_id: int, body: dict) -> dict:
        try:
            e = self.engine.tx.update(int(entry_id), body)
        except (ValueError, KeyError) as exc:
            raise ServiceError(str(exc)) from exc
        self.save_settings()
        return e.as_dict()

    def tx_remove(self, entry_id: int) -> None:
        self.engine.tx.remove(int(entry_id))
        self.save_settings()

    def tx_send(self, entry_id: int) -> dict:
        try:
            self.engine.tx.send_once(int(entry_id))
        except KeyError as exc:
            raise ServiceError(str(exc)) from exc
        except Exception as exc:
            raise ServiceError(f"send failed: {exc}") from exc
        return self.engine.tx.get(int(entry_id)).as_dict()

    # ------------------------------------------------------------------ files
    def resolve_file(self, name_or_path: str, must_exist: bool = True) -> Path:
        """A file in the recordings folder: a bare name, or a path that resolves inside that folder.

        Nothing outside it is served (the server can be reachable from the LAN with serve --host, and MCP read
        tools are free)."""
        if not name_or_path:
            raise ServiceError("no file given")
        folder = recordings_dir().resolve()
        p = Path(name_or_path)
        if p.is_absolute() or len(p.parts) > 1:
            p = p.resolve()
            if p.parent != folder:
                raise ServiceError("only files in the recordings folder can be used here; import the file first")
        else:
            p = folder / safe_name(p.name, "file name")
        if must_exist and not p.is_file():
            raise ServiceError(f"{p.name} not found in the recordings folder")
        return p

    @staticmethod
    def unique_path(p: Path) -> Path:
        """p, or p with -1, -2, ... appended so an existing file is never overwritten."""
        if not p.exists():
            return p
        n = 1
        while (q := p.with_name(f"{p.stem}-{n}{p.suffix}")).exists():
            n += 1
        return q

    def new_recording_path(self, name: str = "", fmt: str = "trc") -> Path:
        fmt = fmt.lower().lstrip(".")
        if f".{fmt}" not in (".trc", ".asc", ".blf", ".log", ".csv", ".canbin"):
            raise ServiceError(f"unknown format {fmt!r}")
        stem = safe_name(name, "file name") if name else time.strftime("rec-%Y%m%d-%H%M%S")
        if stem.lower().endswith(f".{fmt}"):
            stem = stem[: -len(fmt) - 1]
        return self.unique_path(recordings_dir() / f"{stem}.{fmt}")

    def list_recordings(self) -> list[dict]:
        out = []
        for p in sorted(recordings_dir().iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if p.is_file() and p.suffix.lower() in (".trc", ".asc", ".blf", ".log", ".csv", ".canbin"):
                st = p.stat()
                out.append({"name": p.name, "path": str(p), "bytes": st.st_size, "modified": st.st_mtime})
        return out

    # ------------------------------------------------------------------ projects
    def list_projects(self) -> list[dict]:
        if not self.projects_dir.is_dir():
            return []
        return [{"name": p.stem, "modified": p.stat().st_mtime}
                for p in sorted(self.projects_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)]

    def save_project(self, name: str, ui: Optional[dict] = None) -> dict:
        name = safe_name(name, "project name")
        data = {"app": "canopener", "version": __version__, "saved": time.time(),
                "connections": [{**c.cfg.as_dict(), "index": c.index} for c in self.engine.channels.values()
                                if c.state != "file"],
                "tx": [self._tx_dict(e) for e in self.engine.tx.entries],
                "decode": self.decode.config(portable=True),
                "ui": ui or self.settings.get("ui", {}),
                "dashboards": self.settings.get("dashboards", []), "raw_signals": self.settings.get("raw_signals", [])}
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        (self.projects_dir / f"{name}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.settings["project"] = name
        self.save_settings()
        return {"name": name, "path": str(self.projects_dir / f"{name}.json")}

    def load_project(self, name: str) -> dict:
        name = safe_name(name, "project name")
        p = self.projects_dir / f"{name}.json"
        if not p.is_file():
            raise ServiceError(f"no project {name!r}")
        data = json.loads(p.read_text(encoding="utf-8"))
        self.engine.tx.stop_all()
        entries = []
        for d in data.get("tx", []):
            try:
                e = TxEntry.from_dict(d)
                e.enabled = False
                entries.append(e)
            except Exception:
                pass
        self.engine.tx.replace_all(entries)
        if "decode" in data:
            self.decode.apply_config(data["decode"])
        if data.get("ui"):
            self.settings["ui"] = data["ui"]
        from .dash import dashboards as dash_mod, raw_signals as raw_mod
        cleaners = {"dashboards": dash_mod.clean_list, "raw_signals": raw_mod.clean_list}
        for key in ("dashboards", "raw_signals"):      # the quick-signal decoder picks up the new list
            if key in data:
                self.settings[key] = cleaners[key](data[key])
        self.settings["project"] = name
        self.save_settings()
        return {"name": name, "connections": data.get("connections", []), "ui": data.get("ui", {}),
                "tx": len(entries)}

    # ------------------------------------------------------------------ lifecycle
    def shutdown(self) -> None:
        if self._shut:                             # the lifespan, serve() and the window may all call it
            return
        self._shut = True
        for ext in self.extensions:
            try:
                ext.shutdown(self)
            except Exception:
                log.exception("extension %s shutdown failed", ext.id)
        if self._save_timer is not None:
            self._save_timer.cancel()
        if self.save_on_exit:
            self._write_settings()
        self.decode.stop()
        self.raw.stop()
        self.engine.shutdown()
        self.stop_sim()
