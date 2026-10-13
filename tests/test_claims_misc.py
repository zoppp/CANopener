"""Claims of docs/user-guide.md and docs/PLUGINS.md (trace, record/replay, statistics, dashboards, plugins, start-up,
frame timestamps), each backed by a test. A claim found false is a strict xfail naming what really happens."""

import json
import logging
import os
import sys
import textwrap
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import can
import pytest
from fastapi.testclient import TestClient

import canopener.core.channel as channel_mod
from canopener import desktop, ext
from canopener.core.adapters import ChannelConfig
from canopener.core.channel import DEFAULT_RING, F_TX, Channel, Tap
from canopener.core.engine import Engine
from canopener.core.recorder import Recorder, read_frames
from canopener.core.replay import Player
from canopener.dash import dashboards
from canopener.decode.signals import SignalStore
from canopener.paths import recordings_dir
from canopener.server import TICK_S, TRACE_PER_TICK, Feed, _id_row, create_app
from canopener.service import Service

BASE = "http://127.0.0.1:8775"
_real_monotonic = time.monotonic


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def _offline_channel(index=0, **kw) -> Channel:
    """A channel that is never opened: frames go in through _ingest(), as the receive thread does it."""
    return Channel(index, ChannelConfig("virtual", f"claims-{uuid.uuid4()}"), **kw)


def _feed(engine) -> Feed:
    return Feed(SimpleNamespace(engine=engine, nodes=SimpleNamespace(job=None), signals=SignalStore()))


# ------------------------------------------------------------------------------------------- trace filter

def test_trace_filter_runs_in_server_and_finds_a_rare_id(engine):
    """user-guide 278-285: server-side filter (ids, exclude, data contains, Rx/Tx) finds a rare ID among thousands."""
    ch = engine.channels[0] = _offline_channel()
    specs = {
        "none": None,
        "ids": {"ids": "7E5"},
        "exclude": {"ids": "181, 601", "exclude": True},
        "data": {"data": "2F 00 21"},
        "tx": {"dir": "tx"},
        "rx": {"dir": "rx", "ids": "7E5"},
    }
    feeds = {}
    for name, spec in specs.items():
        f = feeds[name] = _feed(engine)
        if spec:
            f.handle({"type": "trace", "filter": spec})
        f.tick()                                     # positions start at the ring's current end
    t = 1000.0
    for i in range(20_000):
        t += 0.0001
        ch._ingest(t, 0x181, 0, 8, b"\x11" * 8)
        if i == 10_000:
            ch._ingest(t, 0x7E5, 0, 4, bytes.fromhex("2F002100"))       # the rare one
        if i % 4000 == 0:
            ch._ingest(t, 0x601, F_TX, 4, bytes.fromhex("40001000"))
    out = {name: f.tick() for name, f in feeds.items()}

    unfiltered = out["none"]["trace"]
    assert len(unfiltered) == TRACE_PER_TICK and all(r[2] != 0x7E5 for r in unfiltered)   # lost without a filter
    for name in ("ids", "exclude", "data", "rx"):
        rows = out[name]["trace"]
        assert [(r[2], r[5]) for r in rows] == [(0x7E5, "2F002100")], name
    tx = out["tx"]["trace"]
    assert len(tx) == 5 and all(r[2] == 0x601 and r[3] & F_TX for r in tx)


# ------------------------------------------------------------------------------------------- trace buffer / save

def test_trace_buffer_keeps_about_260000_frames_per_channel(engine):
    """user-guide 288-290, 825-826: the trace buffer (what Save writes) holds about the last 260 000 frames."""
    assert DEFAULT_RING == 262_144
    live = engine.connect(ChannelConfig("virtual", f"claims-{uuid.uuid4()}"))
    assert live._ring_size == DEFAULT_RING                         # the app's Engine() uses the default ring
    ch = _offline_channel()
    for i in range(270_000):
        ch._ingest(float(i), 0x100, 0, 1, b"\x00")
    kept = ch.trace_tail(10_000_000)                               # exactly what /api/trace/save asks for
    assert 255_000 <= len(kept) <= 265_000
    assert kept[-1][0] == 269_999.0 and kept[0][0] == 270_000 - len(kept)   # the newest ones, in order


@pytest.mark.parametrize("fmt", ["trc", "asc", "blf", "log", "csv"])
def test_trace_save_writes_all_channels_in_five_formats(fmt):
    """user-guide 288-290: Save... writes every channel's buffer to the recordings folder (.trc .asc .blf .log .csv)."""
    svc = Service(Engine(ring_size=4096), load_plugins=False)
    with TestClient(create_app(svc), base_url=BASE) as c:
        for idx in (0, 1):
            ch = svc.engine.channels[idx] = _offline_channel(idx, ring_size=4096)
            for i in range(5000):
                ch._ingest(100.0 + i * 0.001 + idx * 0.0005, 0x180 + idx, 0, 2, bytes([i & 0xFF, idx]))
        r = c.post("/api/trace/save", json={"format": fmt})
        assert r.status_code == 200, r.text
        d = r.json()
    per_channel = 4096 - 1024
    assert d["frames"] == 2 * per_channel
    p = Path(d["path"])
    assert p.parent == recordings_dir() and p.suffix == f".{fmt}" and p.name.startswith("trace-")
    got = [fr for _, fr in read_frames(p)]
    assert len(got) == 2 * per_channel
    assert {fr[1] for fr in got} == {0x180, 0x181}


# ------------------------------------------------------------------------------------------- live view cap

def test_live_view_capped_at_25000_per_second_rest_in_buffer_and_recording(engine, tmp_path):
    """user-guide 292-294, 1171-1172: live view up to 25 000 frames/s; the rest are in the buffer and the recording."""
    assert TRACE_PER_TICK / TICK_S == 25_000
    ch = engine.channels[0] = _offline_channel()
    rec = Recorder(tmp_path / "all.trc", {0: 1})
    ch.add_recorder(rec)
    feed = _feed(engine)
    feed.tick()
    n = 30_000                                                     # more than one 100 ms tick may show
    for i in range(n):
        ch._ingest(1000.0 + i * 1e-5, 0x200 + (i % 50), 0, 2, i.to_bytes(4, "little")[:2])
    d = feed.tick()
    assert len(d["trace"]) == TRACE_PER_TICK                       # shown live
    assert d["skipped"] == n - TRACE_PER_TICK                      # "not shown live" count
    assert d["trace"][-1][1] == round(1000.0 + (n - 1) * 1e-5, 6)  # the newest ones are shown
    assert len(ch.trace_tail(n)) == n                              # all still in the buffer (what Save writes)
    ch.remove_recorder(rec)
    rec.stop()
    assert rec.written == n and len(list(read_frames(rec.path))) == n   # and all in the recording


# ------------------------------------------------------------------------------------------- import file

@pytest.mark.parametrize("suffix", [".trc", ".asc", ".blf", ".log", ".csv", ".canbin"])
def test_import_file_copies_into_recordings_folder(suffix):
    """user-guide 329-330: Import file... copies a .trc/.asc/.blf/.log/.csv/.canbin file into the recordings folder."""
    svc = Service(load_plugins=False)
    payload = b"imported " + suffix.encode()
    with TestClient(create_app(svc), base_url=BASE) as c:
        r = c.post("/api/recordings/upload", files={"file": (f"from-elsewhere{suffix}", payload)})
        assert r.status_code == 200, r.text
        p = Path(r.json()["path"])
        assert p.parent == recordings_dir() and p.read_bytes() == payload
        assert any(f["name"] == p.name for f in c.get("/api/recordings").json()["files"])
        bad = c.post("/api/recordings/upload", files={"file": ("notes.txt", b"x")})
        assert bad.status_code == 400 and not (recordings_dir() / "notes.txt").exists()


# ------------------------------------------------------------------------------------------- replay options

def _replay_file(tmp_path, n=5, step=0.1, name="r.trc"):
    """n frames `step` s apart; even ones recorded as Rx (0x100+i), odd ones as Tx (0x200+i)."""
    path = tmp_path / name
    rec = Recorder(path, {0: 1})
    for i in range(n):
        tx = i % 2 == 1
        rec.put(0, (50.0 + i * step, (0x200 if tx else 0x100) + i, F_TX if tx else 0, 1, bytes([i])))
    rec.stop()
    return path


class _Sink:
    def __init__(self):
        self.got = []

    def __call__(self, ch, msg):
        self.got.append((time.perf_counter(), ch, msg))


def test_replay_speeds_half_double_tenfold(tmp_path):
    """user-guide 352-354: replay at 0.5x, 2x and 10x scales the recorded timing (file spans 0.4 s)."""
    path = _replay_file(tmp_path, n=5, step=0.1)
    sinks = {s: _Sink() for s in (0.5, 2.0, 10.0)}
    players = {s: Player(path, sinks[s], {1: 0}, speed=s) for s in sinks}   # run side by side
    assert _wait(lambda: all(p.state == "done" for p in players.values()), 5.0)
    span = {}
    for s, sink in sinks.items():
        assert len(sink.got) == 5
        span[s] = sink.got[-1][0] - sink.got[0][0]
    assert 0.75 <= span[0.5] < 1.0
    assert 0.19 <= span[2.0] < 0.30
    assert 0.035 <= span[10.0] < 0.10
    assert span[10.0] < span[2.0] < span[0.5]


@pytest.mark.parametrize("only,ids", [("rx", [0x100, 0x102, 0x104]), ("tx", [0x201, 0x203])])
def test_replay_rx_only_tx_only(tmp_path, only, ids):
    """user-guide 352-354: Frames: recorded Rx only / recorded Tx only."""
    path = _replay_file(tmp_path, n=5, step=0.01)
    sink = _Sink()
    p = Player(path, sink, {1: 0}, speed=100.0, only=only)
    assert _wait(lambda: p.state == "done")
    assert [m.arbitration_id for _, _, m in sink.got] == ids
    assert p.skipped == 5 - len(ids)


def test_replay_loop_starts_over(tmp_path):
    """user-guide 354: Loop starts over at the end."""
    path = _replay_file(tmp_path, n=5, step=0.01)
    sink = _Sink()
    p = Player(path, sink, {1: 0}, speed=100.0, loop=True)
    try:
        assert _wait(lambda: p.loops >= 2)
    finally:
        p.stop()
    ids = [m.arbitration_id for _, _, m in sink.got]
    assert ids[:10] == [0x100, 0x201, 0x102, 0x203, 0x104] * 2 and p.state == "stopped"


def test_replay_pause_and_resume(tmp_path):
    """user-guide 356-357: Pause holds the replay, Resume continues with the remaining frames."""
    path = _replay_file(tmp_path, n=6, step=0.05)
    sink = _Sink()
    p = Player(path, sink, {1: 0})
    assert _wait(lambda: len(sink.got) >= 1)
    p.pause(True)
    assert _wait(lambda: p.state == "paused")
    held = len(sink.got)
    time.sleep(0.3)
    assert len(sink.got) == held and held < 6
    p.pause(False)
    assert _wait(lambda: p.state == "done")
    assert len(sink.got) == 6


def test_replay_options_reach_the_player_through_the_engine(engine, channel, tmp_path):
    """user-guide 349-357: the Replay dialog's speed, frames and loop are passed to the player."""
    path = _replay_file(tmp_path)
    engine.connect(ChannelConfig("virtual", channel))
    p = engine.start_replay(path, speed=10.0, loop=True, only="tx")
    try:
        st = p.status()
        assert (st["speed"], st["loop"], st["only"]) == (10.0, True, "tx")
    finally:
        engine.stop_replay()


# ------------------------------------------------------------------------------------------- statistics

def test_busiest_ids_payload_has_count_and_average_cycle():
    """user-guide 374-376: the receive rows give the Statistics tab each ID's count and average cycle."""
    ch = _offline_channel()
    for i in range(10):
        ch._ingest(10.0 + i * 0.02, 0x181, 0, 1, b"\x00")
    _, rows = ch.rows_snapshot()
    r = _id_row(0, rows[0])
    assert r[7] == 10 and abs(r[12] - 20.0) < 1e-6           # count, average cycle (ms)


@pytest.mark.skip(reason="UNTESTABLE from Python: the 'fewer than 5 times' cut is browser JS, "
                         "canopener/ui/js/stats.js:73 (filter r[7] >= 5)")
def test_busiest_ids_leave_out_ids_seen_fewer_than_5_times():
    """user-guide 375-376: IDs seen fewer than 5 times are left out of Busiest IDs."""


# ------------------------------------------------------------------------------------------- dashboards

def test_dashboard_widgets_snap_to_12_column_grid():
    """user-guide 730-731: widgets are placed on a 12-column grid (server clamps saved positions)."""
    assert dashboards.COLS == 12
    d = dashboards.normalize({"name": "grid", "widgets": [
        {"type": "number", "x": 20, "w": 6, "y": 0, "h": 2},
        {"type": "gauge", "x": 3, "w": 50, "y": 5, "h": 2},
        {"type": "lamp", "x": "2.5", "w": 0, "y": -3, "h": 0},
    ]})
    ws = d["widgets"]
    assert (ws[0]["x"], ws[0]["w"]) == (11, 1)
    assert (ws[1]["x"], ws[1]["w"]) == (3, 9)
    assert all(0 <= w["x"] and w["x"] + w["w"] <= 12 and w["w"] >= 1 and w["y"] >= 0 and w["h"] >= 1 for w in ws)


@pytest.mark.skip(reason="UNTESTABLE from Python: overlap prevention is browser JS only, "
                         "canopener/ui/js/dash.js:320-333 (drag(): free(r) refuses a cell range taken by another "
                         "widget); the server's dashboards.normalize() does not check overlaps")
def test_dashboard_widgets_never_overlap():
    """user-guide 731-732: widgets never overlap."""


# ------------------------------------------------------------------------------------------- plugins

GOOD = """
from canopener.ext import Extension

class P(Extension):
    def routes(self, router, svc):
        @router.get("/ping")
        def ping():
            return {"ok": True}

EXTENSION = P()
"""


def _plugin(root: Path, folder: str, pid: str, code: str = GOOD, ui: dict | None = None) -> Path:
    d = root / folder
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"id": pid, "title": pid, "version": "1", "module": "plugin.py"}),
                                     encoding="utf-8")
    (d / "plugin.py").write_text(textwrap.dedent(code), encoding="utf-8")
    for name, text in (ui or {}).items():
        (d / "ui").mkdir(exist_ok=True)
        (d / "ui" / name).write_text(text, encoding="utf-8")
    return d


def _raising(method: str) -> str:
    sig = {"setup": "self, svc", "routes": "self, router, svc", "mcp_tools": "self, mcp, svc, run"}[method]
    return f"""
from canopener.ext import Extension

class P(Extension):
    def {method}({sig}):
        raise RuntimeError("boom in {method}")

EXTENSION = P()
"""


def test_broken_plugins_never_stop_the_app(tmp_path, monkeypatch):
    """user-guide 1058-1059, PLUGINS.md 24: import error, no EXTENSION, failing setup/routes/mcp_tools: app runs."""
    root = tmp_path / "plugins"
    _plugin(root, "a-import", "claims-import", "import canopener_no_such_module_xyz\n")
    _plugin(root, "b-noext", "claims-noext", "X = 1\n")
    _plugin(root, "c-syntax", "claims-syntax", "def broken(:\n")
    _plugin(root, "d-setup", "claims-setup", _raising("setup"))
    _plugin(root, "e-routes", "claims-routes", _raising("routes"))
    _plugin(root, "f-mcp", "claims-mcp", _raising("mcp_tools"))
    _plugin(root, "g-good", "claims-good")
    monkeypatch.setenv("CANOPENER_PLUGIN_DIRS", str(root))
    svc = Service()
    with TestClient(create_app(svc), base_url=BASE) as c:
        assert c.get("/api/about").status_code == 200
        assert c.get("/api/x/claims-good/ping").json() == {"ok": True}
        pl = c.get("/api/plugins").json()
    loaded = {e["id"] for e in pl["extensions"]}
    assert "claims-good" in loaded and "claims-setup" not in loaded
    errs = " | ".join(f"{e['plugin']}: {e['error']}" for e in pl["errors"])
    assert "a-import" in errs and "ModuleNotFoundError" in errs
    assert "b-noext" in errs and "make_extension" in errs
    assert "c-syntax" in errs and "SyntaxError" in errs
    assert "claims-setup: setup: RuntimeError: boom in setup" in errs
    assert "claims-routes: routes: RuntimeError: boom in routes" in errs
    assert "claims-mcp: mcp_tools: RuntimeError: boom in mcp_tools" in errs


def test_plugin_load_order_first_id_wins(tmp_path, monkeypatch):
    """PLUGINS.md 17-21: CANOPENER_PLUGIN_DIRS, then plugins\\ next to the app, then shipped; first id wins."""
    a, b = tmp_path / "env-a", tmp_path / "env-b"
    home_plugins = Path(os.environ["CANOPENER_HOME"]) / "plugins"         # plugins\ next to canopener.exe
    _plugin(a, "zz", "claims-order-x")
    _plugin(b, "aa", "claims-order-x")
    _plugin(b, "y", "claims-order-y")
    _plugin(home_plugins, "y", "claims-order-y")
    _plugin(home_plugins, "hb", "heartbeat-monitor")                      # also shipped in the repo's plugins/
    monkeypatch.setenv("CANOPENER_PLUGIN_DIRS", os.pathsep.join([str(a), str(b)]))
    exts, errors = ext.load_all()
    by_id = {}
    for e in exts:
        assert e.id not in by_id                                          # one plugin per id
        by_id[e.id] = Path(e.folder)
    assert by_id["claims-order-x"] == a / "zz"
    assert by_id["claims-order-y"] == b / "y"
    assert by_id["heartbeat-monitor"] == home_plugins / "hb"
    assert "eds-editor" in by_id                                          # shipped ones still load
    if sys.platform == "win32":
        assert os.pathsep == ";"


def test_plugin_id_with_bad_characters_is_refused(tmp_path):
    """PLUGINS.md 36: an id with characters other than letters, digits and dashes is refused."""
    for i, pid in enumerate(["bad id", "../up", "a/b", ""]):
        folder = _plugin(tmp_path, f"p{i}", pid)
        with pytest.raises(ValueError, match="bad plugin id"):
            ext.load_plugin(folder)


@pytest.mark.xfail(strict=True, reason="CLAIM FALSE: ids with uppercase letters or underscores load fine; "
                                       "canopener/ext.py:85 accepts any str.isalnum() character plus - and _")
def test_plugin_id_must_be_lowercase_letters_digits_dashes(tmp_path):
    """PLUGINS.md 36: `id` is lowercase letters, digits and dashes (uppercase and _ are refused)."""
    for i, pid in enumerate(["ClaimsUpper", "claims_under"]):
        with pytest.raises(ValueError):
            ext.load_plugin(_plugin(tmp_path, f"p{i}", pid))


def test_plugin_ui_scripts_loaded_alphabetically(tmp_path):
    """PLUGINS.md 32: every .js file in ui/ is loaded, alphabetically, served at /ui/x/<id>/."""
    folder = _plugin(tmp_path, "ui-plugin", "claims-ui", ui={"b.js": "", "c.js": "", "a.js": "", "notes.txt": ""})
    e = ext.load_plugin(folder)
    assert e.ui_scripts == ["a.js", "b.js", "c.js"]
    assert e.info()["ui_scripts"] == [f"/ui/x/claims-ui/{n}" for n in ("a.js", "b.js", "c.js")]


def test_dropped_plugin_frames_in_statistics(engine):
    """PLUGINS.md 80-81: a full tap drops the oldest frame, counts it, and the Statistics payload shows it."""
    ch = engine.channels[0] = _offline_channel()
    tap = engine.add_tap(Tap(maxlen=10))
    for i in range(25):
        ch._ingest(1.0 + i, 0x100, 0, 1, bytes([i]))
    assert tap.dropped == 15
    assert [fr[4][0] for _, fr in tap.drain()] == list(range(15, 25))       # the oldest ones went
    assert ch.stats()["taps_dropped"] == 15
    assert _feed(engine).tick()["channels"][0]["taps_dropped"] == 15        # what the Statistics tab reads


# ------------------------------------------------------------------------------------------- start-up

def _app_args(port=8775):
    return SimpleNamespace(port=port)


@pytest.fixture
def quiet_desktop(monkeypatch):
    """desktop.run() without log files, real windows or message boxes."""
    calls = {"message": [], "window": []}
    monkeypatch.setattr(desktop, "setup_logging", lambda: None)
    monkeypatch.setattr(desktop, "message", lambda text, error=False: calls["message"].append((text, error)))
    monkeypatch.setattr(desktop, "open_window",
                        lambda title, url, on_closing=None: calls["window"].append((title, url)) or True)

    def no_service():
        raise AssertionError("a second CANopener must not start its own service")
    monkeypatch.setattr(desktop, "make_service", no_service)
    return calls


def test_port_in_use_by_another_program_message(quiet_desktop, monkeypatch):
    """user-guide 1142-1143: 'Port 8775 is in use by another program' when the port is taken."""
    monkeypatch.setattr(desktop, "claim_instance", lambda port: True)
    monkeypatch.setattr(desktop, "probe", lambda port, host=desktop.HOST: ("other", None))
    assert desktop.run(_app_args()) == 2
    (text, error), = quiet_desktop["message"]
    assert error and text.startswith("Port 8775 is in use by another program.")
    assert "app --port 8776" in text and not quiet_desktop["window"]


def test_second_start_opens_a_window_on_the_running_one(quiet_desktop, monkeypatch):
    """user-guide 48-50, 1199-1200: a second start only opens a window onto the running CANopener."""
    monkeypatch.setattr(desktop, "claim_instance", lambda port: True)
    monkeypatch.setattr(desktop, "probe", lambda port, host=desktop.HOST: ("canopener", "1.2.3"))
    assert desktop.run(_app_args(8775)) == 0
    assert quiet_desktop["window"] == [("CANopener 1.2.3", "http://127.0.0.1:8775/ui/")]


def test_second_start_during_first_start_waits_for_its_server(quiet_desktop, monkeypatch):
    """user-guide 48-50: a double-click while the first is still starting waits for it instead of starting a second."""
    answers = iter([("free", None), ("canopener", "1.2.3")])
    monkeypatch.setattr(desktop, "claim_instance", lambda port: False)        # the mutex is held by the first
    monkeypatch.setattr(desktop, "probe", lambda port, host=desktop.HOST: next(answers))
    assert desktop.run(_app_args(8775)) == 0
    assert quiet_desktop["window"] == [("CANopener 1.2.3", "http://127.0.0.1:8775/ui/")]


@pytest.mark.skipif(sys.platform != "win32", reason="named mutex: Windows only")
def test_one_instance_per_port_mutex():
    """user-guide 50: only one CANopener per PC and port (named mutex per port)."""
    port = 40_000 + uuid.uuid4().int % 20_000
    assert desktop.claim_instance(port) is True
    assert desktop.claim_instance(port) is False                              # same port: already claimed
    assert desktop.claim_instance(port + 1) is True                           # another port: its own instance


def _http_server(about: dict):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(about).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_probe_tells_canopener_other_program_and_free_port():
    """user-guide 48-50, 1142: the start-up probe tells a running CANopener from another program on the port."""
    mine, other = _http_server({"app": "canopener", "version": "9.9"}), _http_server({"app": "something"})
    try:
        assert desktop.probe(mine.server_address[1]) == ("canopener", "9.9")
        assert desktop.probe(other.server_address[1]) == ("other", None)
    finally:
        for s in (mine, other):
            s.shutdown()
            s.server_close()
    free = other.server_address[1]                                            # closed now
    assert desktop.probe(free) == ("free", None)


def test_app_log_drops_the_peer_reset_on_window_close_only(caplog):
    """user-guide 1296 points at logs/canopener.log for problems: closing the window must not put an asyncio
    ERROR traceback there (WinError 10054 in _call_connection_lost, harmless), but other asyncio errors stay."""
    import asyncio
    log = logging.getLogger("asyncio")
    f = desktop.PeerResetFilter()
    log.addFilter(f)
    loop = asyncio.new_event_loop()
    try:
        with caplog.at_level(logging.ERROR, logger="asyncio"):
            for message, exc in (
                ("Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)",
                 ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")),
                ("Exception in callback something_else()", ConnectionResetError(10054, "reset")),
                ("Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)", OSError("other")),
            ):
                loop.call_exception_handler({"message": message, "exception": exc})    # asyncio's default handler
    finally:
        loop.close()
        log.removeFilter(f)
    assert [r.getMessage() for r in caplog.records] == [
        "Exception in callback something_else()",
        "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)"]
    assert isinstance(caplog.records[1].exc_info[1], OSError)


# ------------------------------------------------------------------------------------------- frame timestamps

class _FakeClock:
    """Stands in for the time module inside canopener.core.channel: the host clock is set by the fake bus."""
    monotonic = staticmethod(_real_monotonic)

    def __init__(self, now: float):
        self.now = now

    def time(self) -> float:
        return self.now


class _ScriptedBus:
    """recv() hands out frames whose host arrival time and device timestamp the test chose."""

    def __init__(self, clock: _FakeClock, script: list):
        self.clock, self.script, self.ch = clock, list(script), None

    def recv(self, timeout=None):
        if not self.script:
            self.ch._stop.set()
            return None
        host, dev = self.script.pop(0)
        self.clock.now = host
        return can.Message(timestamp=dev, arbitration_id=0x181, is_extended_id=False, data=b"\x01")


def _run_script(monkeypatch, interface: str, script: list) -> list:
    """Run the receive loop over `script` [(host time, device timestamp)]; returns the ingested ts values."""
    clock = _FakeClock(script[0][0])
    monkeypatch.setattr(channel_mod, "time", clock)
    bus = _ScriptedBus(clock, script)
    ch = Channel(0, ChannelConfig(interface, None), bus=bus)
    bus.ch = ch
    ch._run()                                                                 # synchronously, no thread
    frames = ch.trace_tail(len(script))
    assert len(frames) == len(script)
    return [fr[0] for fr in frames]


def test_hw_timestamps_are_wall_clock_and_keep_cycle_times(monkeypatch):
    """PLUGINS.md 73: ts is wall-clock (time.time()); device timestamps + offset keep exact cycles despite jitter."""
    h0, dev0, period = time.time(), 1234.5, 0.010              # device clock: seconds since the adapter powered up
    lat = [0.002 + 0.006 * (((i + 5) * 37) % 11) / 10 for i in range(300)]   # 2..8 ms, lowest first at i = 6
    script = [(h0 + i * period + lat[i], dev0 + i * period) for i in range(300)]
    ts = _run_script(monkeypatch, "pcan", script)
    host = [h for h, _ in script]
    assert all(t <= h + 1e-9 and h - t <= 0.0061 for t, h in zip(ts, host))  # on the host's wall clock
    assert abs(ts[-1] - time.time()) < 60                                     # time.time() base, not device uptime
    host_cycles = [b - a for a, b in zip(host, host[1:])]
    assert max(host_cycles) - min(host_cycles) > 0.005                        # the host saw jitter ...
    cycles = [b - a for a, b in zip(ts[7:], ts[8:])]                          # ... once the lowest latency was seen
    assert all(abs(c - period) < 1e-6 for c in cycles)                        # ... the adapter's cycle survives


def test_hw_timestamps_resync_after_device_counter_jump(monkeypatch):
    """PLUGINS.md 73, channel.py:270-273: a device counter wrap or reset resyncs; ts stays on the wall clock."""
    h0 = time.time()
    script = [(h0 + i * 0.01 + 0.002, 5000.0 + i * 0.01) for i in range(100)]
    script += [(h0 + (100 + i) * 0.01 + 0.002, 0.0 + i * 0.01) for i in range(100)]    # adapter reset: back to 0
    ts = _run_script(monkeypatch, "kvaser", script)
    assert all(abs(h - t) < 1e-6 for t, (h, _) in zip(ts, script))
    assert all(abs((b - a) - 0.01) < 1e-6 for a, b in zip(ts[101:], ts[102:]))


def test_hw_timestamps_follow_clock_drift(monkeypatch):
    """PLUGINS.md 73, channel.py:276-280: the offset rises again with drift, so ts does not fall behind time.time()."""
    h0, period, n = time.time(), 0.010, 6000                     # 60 s
    drift = 500e-6                                               # the device clock runs 500 ppm slow
    script = [(h0 + i * period + 0.002, 77.0 + i * period * (1 - drift)) for i in range(n)]
    ts = _run_script(monkeypatch, "pcan", script)
    lag = [h - t for t, (h, _) in zip(ts, script)]
    assert min(lag) >= -1e-9
    assert lag[-1] < 0.012                                       # a fixed first offset would lag 30 ms by now
    cycles = [b - a for a, b in zip(ts, ts[1:])]
    steps = sum(abs(c - period * (1 - drift)) > 1e-6 for c in cycles)   # ts ~1.7e9: ulp ~2e-7
    assert steps <= 6                                            # the device's cycle except at the few re-syncs


@pytest.mark.parametrize("interface", ["gs_usb", "slcan"])
def test_host_clock_adapters_use_time_time(monkeypatch, interface):
    """PLUGINS.md 73, channel.py:241, 264-265: gs_usb and slcan frames are stamped with the host clock."""
    h0 = time.time()
    script = [(h0 + i * 0.013, (i * 0.01) % 0.05) for i in range(50)]       # a wrapping device counter
    ts = _run_script(monkeypatch, interface, script)
    assert ts == [h for h, _ in script]


def test_frames_without_device_timestamp_use_time_time(monkeypatch):
    """PLUGINS.md 73, channel.py:264: a frame with no device timestamp (0) gets time.time()."""
    h0 = time.time()
    script = [(h0 + i * 0.01, 0.0) for i in range(10)]
    assert _run_script(monkeypatch, "pcan", script) == [h for h, _ in script]
