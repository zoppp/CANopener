"""FastAPI app: REST API, the 10 Hz WebSocket feed, the static UI, plugin routes and the MCP endpoint (/mcp/).

The UI never receives single frames as events: every 100 ms the feed sends what changed (receive-table rows
since the last tick, new trace frames after the client's filter, capped per tick), the channel statistics,
new engine events and the node job's new log lines.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .core import adapters
from .core.channel import F_RTR, F_TX
from .core.engine import EngineError
from .core.transmit import parse_bool, parse_data, parse_id
from .decode import routes as decode_routes
from .dash import routes as dash_routes
from .ext import plugin_dirs
from .mcp_server import build_mcp
from .nodes.service import NodeError
from .paths import bundle_dir, package_dir, recordings_dir
from .service import Service, ServiceError

log = logging.getLogger(__name__)

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
TICK_S = 0.1
TRACE_PER_TICK = 2500          # 25k frames/s to the UI: a full 1 Mbit/s bus (~8k/s) fits with jitter to spare


def _local_origin(origin: str) -> bool:
    from urllib.parse import urlparse
    return (urlparse(origin).hostname or "") in ALLOWED_HOSTS


def _same_origin(origin: str, host_header: str, remote: bool) -> bool:
    """A local page, or (serve --host) a page served by this server itself; never another web site."""
    from urllib.parse import urlparse
    if _local_origin(origin):
        return True
    return remote and urlparse(origin).netloc.lower() == host_header.lower()


# ------------------------------------------------------------------------------------------- trace filter

def parse_id_filter(text: str) -> list[tuple[int, int]]:
    """'100-1FF, 7E5, 18FEF100' -> [(0x100, 0x1FF), (0x7E5, 0x7E5), ...]. Hex."""
    out = []
    for part in (text or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = parse_id(a), parse_id(b)
        else:
            lo = hi = parse_id(part)
        out.append((min(lo, hi), max(lo, hi)))
    return out


class TraceFilter:
    def __init__(self, spec: Optional[dict] = None):
        spec = spec or {}
        self.ranges = parse_id_filter(spec.get("ids", ""))
        self.exclude = bool(spec.get("exclude"))
        self.direction = spec.get("dir", "all")
        chans = spec.get("channels")
        self.channels = set(int(c) for c in chans) if chans else None
        data = (spec.get("data") or "").replace(" ", "").upper()
        self.data = data or None

    @property
    def passthrough(self) -> bool:
        return not self.ranges and self.direction == "all" and self.channels is None and self.data is None

    def accept(self, ch: int, frame) -> bool:
        if self.channels is not None and ch not in self.channels:
            return False
        flags = frame[2]
        if self.direction == "rx" and flags & F_TX:
            return False
        if self.direction == "tx" and not flags & F_TX:
            return False
        if self.ranges:
            cid = frame[1]
            hit = any(lo <= cid <= hi for lo, hi in self.ranges)
            if hit == self.exclude:
                return False
        if self.data is not None and self.data not in frame[4].hex().upper():
            return False
        return True


def _frame_row(ch: int, fr) -> list:
    ts, cid, flags, dlc, data = fr
    return [ch, round(ts, 6), cid, flags, dlc, "" if flags & F_RTR else data.hex().upper()]


def _id_row(ch: int, r) -> list:
    key, cid, flags, dlc, data, prev, count, t_first, t_last, cycle, cmin, cmax = r
    avg = (t_last - t_first) / (count - 1) * 1000 if count > 1 else 0.0
    return [f"{ch}:{key}", ch, cid, flags, dlc, data.hex().upper(), prev.hex().upper(), count,
            round(cycle * 1000, 3), round(cmin * 1000, 3), round(cmax * 1000, 3), round(t_last, 6), round(avg, 3)]


class Feed:
    """Per-WebSocket state: positions in each channel's ring and table, and the client's trace filter."""

    def __init__(self, svc: Service):
        self.svc = svc
        self.trace_pos: dict[int, int] = {}
        self.row_seq: dict[int, int] = {}
        self.chan_ids: dict[int, int] = {}       # channel index -> Channel.serial
        self.chan_gen: dict[int, int] = {}
        self.event_seq = svc.engine.events[-1][0] if svc.engine.events else 0
        self.job_id = None
        self.job_lines = 0
        self.job_state = None
        self.sig_seq = 0
        self.sig_gen = -1
        self.paused = False
        self.filter = TraceFilter()
        self.filter_gen = None         # the client's number of its filter: trace rows say which filter made them
        self.tick_n = 0

    def handle(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "trace":
            if "paused" in msg:
                self.paused = bool(msg["paused"])
            if "filter" in msg:
                try:
                    self.filter = TraceFilter(msg["filter"])
                except ValueError:
                    self.filter = TraceFilter()
            if "gen" in msg:
                self.filter_gen = msg["gen"]
        elif t == "resync":
            self.row_seq.clear()

    def tick(self) -> dict:
        eng = self.svc.engine
        self.tick_n += 1
        out: dict = {"t": time.time(), "rows": {}, "trace": [], "skipped": 0}
        chans = sorted(eng.channels.values(), key=lambda c: c.index)
        out["channels"] = [c.stats() for c in chans]
        live = set()
        for c in chans:
            live.add(c.index)
            if self.chan_ids.get(c.index) != c.serial:            # another channel object: start fresh
                self.chan_ids[c.index] = c.serial
                self.trace_pos[c.index] = c.written               # a file view's frames: GET /api/trace
                self.chan_gen[c.index] = -1
            if self.chan_gen.get(c.index) != c.generation:       # new channel or cleared table: send it whole
                self.chan_gen[c.index] = c.generation
                self.row_seq[c.index] = 0
                out["rows"][str(c.index)] = {"full": True, "rows": []}
            seq, rows = c.rows_snapshot(self.row_seq.get(c.index, 0))
            if rows or self.row_seq.get(c.index, 0) == 0:
                entry = out["rows"].setdefault(str(c.index), {"full": self.row_seq.get(c.index, 0) == 0, "rows": []})
                entry["rows"] = [_id_row(c.index, r) for r in rows]
            self.row_seq[c.index] = seq
        for gone in set(self.chan_ids) - live:
            self.chan_ids.pop(gone, None)
            self.trace_pos.pop(gone, None)
            self.row_seq.pop(gone, None)
        # trace: merge new frames of every channel, newest TRACE_PER_TICK after filtering
        merged = []
        skipped = 0
        for c in chans:
            pos = self.trace_pos.get(c.index, c.written)
            if self.paused:
                self.trace_pos[c.index] = c.written
                continue
            pos, frames, sk = c.trace_since(pos, limit=200_000)
            self.trace_pos[c.index] = pos
            skipped += sk
            f = self.filter
            if f.passthrough:
                merged.extend((c.index, fr) for fr in frames)
            else:
                merged.extend((c.index, fr) for fr in frames if f.accept(c.index, fr))
        if len(merged) > TRACE_PER_TICK:
            merged.sort(key=lambda x: x[1][0])
            skipped += len(merged) - TRACE_PER_TICK
            merged = merged[-TRACE_PER_TICK:]
        elif len(chans) > 1:
            merged.sort(key=lambda x: x[1][0])
        out["trace"] = [_frame_row(ch, fr) for ch, fr in merged]
        out["trace_gen"] = self.filter_gen
        out["skipped"] = skipped
        out["paused"] = self.paused
        # events
        evs = [e for e in list(eng.events) if e[0] > self.event_seq]
        if evs:
            self.event_seq = evs[-1][0]
            out["events"] = [list(e) for e in evs]
        # node job lines
        job = self.svc.nodes.job
        if job is not None:
            if job.id != self.job_id:
                self.job_id, self.job_lines, self.job_state = job.id, 0, None
            d = job.as_dict(self.job_lines)
            if d["lines"] or (job.state == "running" and self.tick_n % 10 == 0) or self.job_state != job.state:
                self.job_lines = d["line_count"]
                self.job_state = job.state
                out["job"] = d
        # decoded signals: the latest value of each that changed (dashboards keep their own history)
        store = self.svc.signals
        if store.generation != self.sig_gen:
            self.sig_gen, self.sig_seq = store.generation, 0
            out["signals_reset"] = True
        self.sig_seq, sigs = store.changed(self.sig_seq)
        if sigs:
            out["signals"] = sigs
        if self.tick_n % 3 == 0:
            out["recording"] = eng.recorder.status() if eng.recorder else None
            out["replay"] = eng.player.status() if eng.player else None
            out["tx"] = [{"id": e.id, "count": e.count, "error": e.error, "enabled": e.enabled}
                         for e in eng.tx.entries]
        return out


# ------------------------------------------------------------------------------------------- app

def create_app(svc: Service) -> FastAPI:
    mcp = build_mcp(svc)
    mcp_app = mcp.streamable_http_app(streamable_http_path="/")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            async with mcp.session_manager.run():
                yield
        finally:                                   # stop transmits, finish a recording, close adapters
            await run_in_threadpool(svc.shutdown)

    app = FastAPI(title="CANopener", lifespan=lifespan)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        """Refuse other host names (DNS rebinding) and cross-site POSTs from other web pages."""
        remote = getattr(app.state, "allow_remote", False)
        host_hdr = request.headers.get("host") or ""
        host = host_hdr.rsplit(":", 1)[0].strip("[]")
        if host not in ALLOWED_HOSTS and not remote:
            return JSONResponse({"error": "host not allowed"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin and not _same_origin(origin, host_hdr, remote):
            return JSONResponse({"error": "cross-site request refused"}, status_code=403)
        resp = await call_next(request)
        if request.url.path.startswith("/ui"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    def err(exc: Exception, status: int = 400):
        return JSONResponse({"error": str(exc)}, status_code=status)

    for exc_type in (ServiceError, EngineError, NodeError, ValueError, KeyError):
        app.add_exception_handler(exc_type, lambda request, exc: err(exc))

    async def call(fn, *a, **kw):
        return await run_in_threadpool(fn, *a, **kw)

    eng = svc.engine

    # ---------------------------------------------------------------- general
    @app.get("/api/about")
    async def about():
        return svc.about()

    @app.get("/api/status")
    async def status():
        return svc.status()

    @app.get("/api/backends")
    async def backends():
        return {"backends": adapters.backends_info(), "bitrates": adapters.BITRATES,
                "data_bitrates": adapters.DATA_BITRATES}

    @app.get("/api/devices")
    async def devices():
        return {"devices": [d.as_dict() for d in await call(adapters.detect_devices)]}

    @app.get("/api/settings")
    async def get_settings():
        return svc.settings

    @app.post("/api/settings/ui")
    async def set_ui(body: dict = Body(...)):
        return svc.set_ui_settings(body)

    @app.get("/api/events")
    async def events():
        return {"events": [list(e) for e in eng.events]}

    @app.post("/api/sim/start")
    async def sim_start(body: dict = Body(default={})):
        return await call(svc.start_sim, str(body.get("channel") or "canopener"))

    @app.post("/api/sim/stop")
    async def sim_stop():
        await call(svc.stop_sim)
        return {"running": False}

    @app.get("/api/sim")
    async def sim_status():
        return svc.sim_status()

    # ---------------------------------------------------------------- channels
    @app.post("/api/connect")
    async def connect(body: dict = Body(...)):
        return await call(svc.connect, body)

    @app.post("/api/disconnect")
    async def disconnect(body: dict = Body(...)):
        await call(svc.disconnect, int(body.get("index", 0)))
        return svc.status()

    @app.post("/api/channel/clear")
    async def clear(body: dict = Body(...)):
        idx = body.get("index")
        for c in eng.channels.values():
            if idx is None or c.index == int(idx):
                c.clear()
        return {"ok": True}

    @app.get("/api/trace")
    async def trace(channel: int = 0, count: int = 5000):
        ch = eng.channel(channel)
        return {"frames": [_frame_row(channel, fr) for fr in ch.trace_tail(min(count, 200_000))]}

    @app.post("/api/trace/save")
    async def trace_save(body: dict = Body(default={})):
        """Write what the trace ring holds (all channels) to a file in the recordings folder."""
        from .core.recorder import _open_writer
        fmt = body.get("format", "trc")
        path = svc.new_recording_path(body.get("name", "") or time.strftime("trace-%Y%m%d-%H%M%S"), fmt)

        def work():
            frames = []
            nums = {}
            for n, c in enumerate(sorted(eng.channels.values(), key=lambda c: c.index), 1):
                nums[c.index] = n
                frames.extend((c.index, fr) for fr in c.trace_tail(10_000_000))
            frames.sort(key=lambda x: x[1][0])
            w = _open_writer(path, 0)
            try:
                for ch, fr in frames:
                    w.write(nums[ch], fr)
            finally:
                w.close()
            return {"path": str(path), "name": path.name, "frames": len(frames)}
        return await call(work)

    @app.post("/api/send")
    async def send(body: dict = Body(...)):
        data = parse_data(body.get("data", ""))
        ext = None if body.get("ext") is None else parse_bool(body["ext"], "ext")
        ts = await call(eng.send, int(body.get("channel", 0)), parse_id(body["id"]), data,
                        ext=ext, fd=parse_bool(body.get("fd"), "fd"), brs=parse_bool(body.get("brs"), "brs"),
                        rtr=parse_bool(body.get("rtr"), "rtr"), dlc=body.get("dlc"))
        return {"ok": True, "ts": ts}

    # ---------------------------------------------------------------- transmit list
    @app.get("/api/tx")
    async def tx_list():
        return {"entries": [e.as_dict() for e in eng.tx.entries]}

    @app.post("/api/tx")
    async def tx_add(body: dict = Body(...)):
        return await call(svc.tx_add, body)

    @app.patch("/api/tx/{entry_id}")
    async def tx_update(entry_id: int, body: dict = Body(...)):
        return await call(svc.tx_update, entry_id, body)     # waits for a send under way: off the event loop

    @app.delete("/api/tx/{entry_id}")
    async def tx_delete(entry_id: int):
        await call(svc.tx_remove, entry_id)
        return {"ok": True}

    @app.post("/api/tx/{entry_id}/send")
    async def tx_send(entry_id: int):
        return await call(svc.tx_send, entry_id)

    @app.post("/api/tx/stop_all")
    async def tx_stop_all():
        await call(eng.tx.stop_all)
        return {"ok": True}

    # ---------------------------------------------------------------- record / replay / files
    @app.post("/api/record/start")
    async def record_start(body: dict = Body(default={})):
        path = svc.new_recording_path(body.get("name", ""), body.get("format", "trc"))
        chans = body.get("channels") or None
        rec = await call(eng.start_recording, path, [int(c) for c in chans] if chans else None)
        return rec.status()

    @app.post("/api/record/stop")
    async def record_stop():
        return await call(eng.stop_recording) or {}

    @app.get("/api/recordings")
    async def recordings():
        return {"folder": str(recordings_dir()), "files": svc.list_recordings()}

    @app.get("/api/recordings/{name}")
    async def recording_file(name: str):
        p = svc.resolve_file(name)
        return FileResponse(p, filename=p.name)

    @app.post("/api/recordings/upload")
    async def recording_upload(file: UploadFile = File(...)):
        p = svc.unique_path(svc.resolve_file(Path(file.filename or "upload.trc").name, must_exist=False))
        if p.suffix.lower() not in (".trc", ".asc", ".blf", ".log", ".csv", ".canbin"):
            raise ServiceError("not a recording file (.trc .asc .blf .log .csv .canbin)")
        p.write_bytes(await file.read())
        return {"name": p.name, "path": str(p)}

    @app.post("/api/file/open")
    async def file_open(body: dict = Body(...)):
        p = svc.resolve_file(body.get("name") or body.get("path", ""))
        chans = await call(eng.open_file_view, p)
        return {"channels": [c.index for c in chans]}

    @app.post("/api/replay/start")
    async def replay_start(body: dict = Body(...)):
        p = svc.resolve_file(body.get("name") or body.get("path", ""))
        bus_map = {int(k): int(v) for k, v in (body.get("bus_map") or {}).items()} or None
        player = await call(eng.start_replay, p, bus_map, speed=float(body.get("speed", 1.0)),
                            loop=parse_bool(body.get("loop"), "loop"), only=body.get("only", "all"))
        return player.status()

    @app.post("/api/replay/stop")
    async def replay_stop():
        return await call(eng.stop_replay) or {}

    @app.post("/api/replay/pause")
    async def replay_pause(body: dict = Body(...)):
        if eng.player is None:
            raise ServiceError("no replay running")
        eng.player.pause(parse_bool(body.get("paused", True), "paused"))
        return eng.player.status()

    # ---------------------------------------------------------------- nodes
    nodes = svc.nodes

    @app.post("/api/nodes/scan")
    async def nodes_scan(body: dict = Body(default={})):
        job = nodes.scan_job(int(body.get("channel", 0)), float(body.get("seconds", 2.5)),
                             float(body.get("sdo_timeout", 0.03)))
        return job.as_dict()

    @app.get("/api/nodes/scan")
    async def nodes_scan_result(channel: int = 0):
        return nodes.scan_dict(channel) or {}

    @app.get("/api/nodes/job")
    async def nodes_job(since: int = 0):
        return nodes.job.as_dict(since) if nodes.job else {}

    @app.post("/api/nodes/stop")
    async def nodes_stop():
        nodes.stop_job()
        return {"ok": True}

    @app.post("/api/nodes/plan/set_id")
    async def plan_set_id(body: dict = Body(...)):
        p = nodes.plan_set_id(int(body.get("channel", 0)), int(body["old"]), int(body["new"]),
                              body.get("method") or None, float(body.get("verify_timeout", 5.0)))
        return p.as_dict()

    @app.post("/api/nodes/plan/baud")
    async def plan_baud(body: dict = Body(...)):
        node = body.get("node")
        p = nodes.plan_baud(int(body.get("channel", 0)), int(body["kbit"]),
                            None if node in (None, "") else int(node), int(body.get("switch_delay_ms", 500)),
                            float(body.get("verify_timeout", 5.0)))
        return p.as_dict()

    @app.post("/api/nodes/plan/custom")
    async def plan_custom(body: dict = Body(...)):
        p = nodes.plan_custom(int(body.get("channel", 0)), body.get("steps") or [], body.get("vars") or {},
                              body.get("title", ""))
        return p.as_dict()

    @app.post("/api/nodes/run")
    async def nodes_run(body: dict = Body(...)):
        return nodes.run_job(str(body["token"]), actor="user", expected_title=body.get("title")).as_dict()

    @app.get("/api/nodes/recipes")
    async def nodes_recipes():
        from .nodes import recipes as rm
        return {"recipes": rm.recipe_list(nodes.recipes()), "user_file": str(rm.user_recipes_path())}

    @app.post("/api/nodes/recipes/save")
    async def nodes_recipe_save(body: dict = Body(...)):
        vid = parse_id(str(body["vendor_id"]))
        path = nodes.save_recipe(vid, body.get("name", ""), body.get("kind", "set_id"), body.get("steps") or [],
                                 body.get("effect", "immediate"), body.get("node_id_range"), body.get("note", ""))
        return {"ok": True, "path": path}

    @app.get("/api/nodes/log")
    async def nodes_log(lines: int = 300):
        p = nodes.log_path
        if not p.is_file():
            return {"lines": []}
        text = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"lines": text[-lines:], "path": str(p)}

    # ---------------------------------------------------------------- decoded signals
    @app.get("/api/signals")
    async def signals(prefix: str = ""):
        return {"signals": svc.signals.list(prefix)}

    @app.get("/api/signals/history")
    async def signal_history(key: str, seconds: float = 60.0, max_points: int = 5000):
        return {"key": key, "points": svc.signals.history(key, seconds, max_points)}

    decode_routes.register(app, svc)
    dash_routes.register(app, svc)                 # dashboards and quick signals: canopener/dash/routes.py

    # ---------------------------------------------------------------- projects
    @app.get("/api/projects")
    async def projects():
        return {"projects": svc.list_projects(), "current": svc.settings.get("project")}

    @app.post("/api/projects/save")
    async def project_save(body: dict = Body(...)):
        return svc.save_project(body.get("name", ""), body.get("ui"))

    @app.post("/api/projects/load")
    async def project_load(body: dict = Body(...)):
        return svc.load_project(body.get("name", ""))

    # ---------------------------------------------------------------- plugins
    @app.get("/api/plugins")
    async def plugins():
        return {"extensions": [e.info() for e in svc.extensions], "errors": svc.extension_errors,
                "folders": [str(d) for d in plugin_dirs()]}

    for ext in svc.extensions:
        router = APIRouter()
        try:
            ext.routes(router, svc)
        except Exception as exc:
            log.exception("extension %s routes failed", ext.id)
            svc.extension_errors.append({"plugin": ext.id, "error": f"routes: {type(exc).__name__}: {exc}"})
            continue
        app.include_router(router, prefix=f"/api/x/{ext.id}")
        if ext.ui_dir is not None and Path(ext.ui_dir).is_dir():
            app.mount(f"/ui/x/{ext.id}", StaticFiles(directory=str(ext.ui_dir)), name=f"ui-x-{ext.id}")

    # ---------------------------------------------------------------- websocket feed
    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        if origin and not _same_origin(origin, websocket.headers.get("host") or "",
                                       getattr(app.state, "allow_remote", False)):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        feed = Feed(svc)

        async def reader():
            while True:
                text = await websocket.receive_text()
                try:
                    feed.handle(json.loads(text))
                except (ValueError, TypeError):
                    pass

        rtask = asyncio.create_task(reader())
        loop = asyncio.get_running_loop()
        try:
            due = loop.time()
            while not rtask.done():
                data = await run_in_threadpool(feed.tick)
                await websocket.send_text(json.dumps(data, separators=(",", ":")))
                due = max(due + TICK_S, loop.time())          # fixed 100 ms deadlines, no drift
                await asyncio.sleep(due - loop.time())
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception:
            log.exception("websocket feed failed")
        finally:
            rtask.cancel()

    # ---------------------------------------------------------------- static
    ui_dir = package_dir() / "ui"
    app.mount("/ui", StaticFiles(directory=str(ui_dir), html=True), name="ui")
    guide = bundle_dir() / "docs"
    if guide.is_dir():
        app.mount("/guide", StaticFiles(directory=str(guide), html=True), name="guide")
    app.mount("/mcp", mcp_app)

    @app.get("/")
    async def root():
        return RedirectResponse("/ui/")

    return app
