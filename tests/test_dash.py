"""Quick signals (raw frame signals) and dashboards: extraction, decoder throughput, REST, projects, simulator."""

import json
import struct
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from canopener.core.channel import F_EXT, F_RTR, Tap
from canopener.dash.raw_signals import RawDecoder, RawSignal, normalize
from canopener.decode.signals import SignalStore
from canopener.server import create_app
from canopener.service import Service, ServiceError

BASE = "http://127.0.0.1:8775"


def sig(**kw):
    return RawSignal(normalize({"name": "s", "can_id": "100", **kw}))


def test_intel_extraction():
    assert sig(start_bit=0, length=16).extract(bytes([0x34, 0x12])) == 0x1234
    assert sig(start_bit=4, length=8).extract(bytes([0xA0, 0x0B])) == 0xBA
    assert sig(start_bit=8, length=1).extract(bytes([0, 1])) == 1
    assert sig(start_bit=56, length=8).extract(bytes(8)[:7]) is None          # frame too short


def test_motorola_extraction():
    assert sig(start_bit=7, length=16, byte_order="big").extract(bytes([0x12, 0x34])) == 0x1234
    # 12 bits, MSB at bit 3 of byte 0: low nibble of byte 0 + byte 1
    assert sig(start_bit=3, length=12, byte_order="big").extract(bytes([0xFA, 0xBC])) == 0xABC
    assert sig(start_bit=23, length=8, byte_order="big").extract(bytes([0, 0, 0x5A, 0])) == 0x5A
    assert sig(start_bit=15, length=16, byte_order="big").extract(bytes([0, 0x12])) is None


def test_signed_factor_offset():
    assert sig(length=8, signed=True).extract(b"\xff") == -1
    assert sig(length=16, signed=True).extract(struct.pack("<h", -500)) == -500
    assert sig(start_bit=7, length=16, byte_order="big", signed=True).extract(struct.pack(">h", -2)) == -2
    assert sig(length=8, factor=0.5, offset=-10).extract(bytes([100])) == 40.0
    assert sig(length=8, signed=True, factor=2).extract(b"\x80") == -256.0


def test_definition_checks():
    for bad in ({"name": "has space"}, {"byte_order": "middle"}, {"can_id": "800", "extended": False},
                {"length": 0}, {"start_bit": 511, "length": 16, "byte_order": "big"}, {"can_id": "zz"}):
        with pytest.raises(ServiceError):
            normalize({"name": "s", "can_id": "100", **bad})
    assert normalize({"name": "x", "can_id": "18FEF100"})["extended"] is True
    assert normalize({"name": "x", "can_id": 0x123, "channel": "any"})["channel"] is None


def fake_svc(defs):
    return SimpleNamespace(settings={"raw_signals": defs}, signals=SignalStore())


def test_decoder_ids_channels_and_reload():
    svc = fake_svc([{"name": "ext", "can_id": "100", "extended": True, "length": 8},
                    {"name": "std", "can_id": "100", "length": 8},
                    {"name": "ch1", "can_id": "200", "length": 8, "channel": 1},
                    {"name": "any", "can_id": "200", "length": 8, "start_bit": 8}])
    dec = RawDecoder(svc)
    dec.refresh()
    t = time.time()
    dec.decode([(0, (t, 0x100, F_EXT, 1, b"\x07")), (0, (t, 0x100, 0, 1, b"\x09")),
                (0, (t, 0x200, 0, 2, b"\x01\x02")), (1, (t, 0x200, 0, 2, b"\x03\x04")),
                (0, (t, 0x300, 0, 1, b"\x01"))])
    vals = {s["key"]: (s["value"], s["count"]) for s in svc.signals.list()}
    assert vals == {"raw/ext": (7, 1), "raw/std": (9, 1), "raw/ch1": (3, 1), "raw/any": (4, 2)}
    assert dec._accept(0x100, 0) and not dec._accept(0x300, 0) and not dec._accept(0x100, F_RTR)
    svc.settings["raw_signals"] = svc.settings["raw_signals"][:1]       # remove three: their values go
    dec.refresh()
    assert [s["key"] for s in svc.signals.list()] == []
    dec.decode([(0, (t, 0x100, F_EXT, 1, b"\x08")), (0, (t, 0x200, 0, 2, b"\x01\x02"))])
    assert [s["key"] for s in svc.signals.list()] == ["raw/ext"]


def test_decoder_throughput(engine):
    """A full 1 Mbit/s bus is ~8k frames/s: the decoder thread must keep up (two signals per frame)."""
    n = 80_000
    defs = []
    for i in range(8):
        defs += [{"name": f"a{i}", "can_id": 0x100 + i, "length": 16, "factor": 0.1},
                 {"name": f"b{i}", "can_id": 0x100 + i, "start_bit": 23, "length": 12, "byte_order": "big",
                  "signed": True}]
    svc = SimpleNamespace(settings={"raw_signals": defs}, signals=SignalStore(), engine=engine)
    dec = RawDecoder(svc)
    dec.tap = Tap(maxlen=None, accept=dec._accept)
    t = time.time()
    for i in range(n):
        dec.tap.push((0, (t + i * 1e-4, 0x100 + i % 8, 0, 8, i.to_bytes(4, "little") + b"\x00" * 4)))
    t0 = time.perf_counter()
    dec.start()
    try:
        deadline = time.monotonic() + 30
        while sum(s["count"] for s in svc.signals.list()) < 2 * n and time.monotonic() < deadline:
            time.sleep(0.01)
        elapsed = time.perf_counter() - t0
    finally:
        dec.stop()
    assert sum(s["count"] for s in svc.signals.list()) == 2 * n
    rate = n / elapsed
    print(f"\nquick-signal decoder: {rate:,.0f} frames/s ({2 * rate:,.0f} values/s)")
    assert rate >= 8000


# ------------------------------------------------------------------------------------------- REST

@pytest.fixture
def client(channel):
    svc = Service(load_plugins=False)
    with TestClient(create_app(svc), base_url=BASE) as c:
        c.svc = svc
        yield c


def test_dashboards_rest_and_projects(client):
    d = {"name": "Test rig", "widgets": [
        {"type": "plot", "x": 0, "y": 0, "w": 6, "h": 5, "title": "Speed", "series": [{"key": "raw/s", "color": "#ff0000"}],
         "window": 60},
        {"type": "gauge", "x": 11, "y": 0, "w": 5, "h": 4, "key": "raw/s", "min": 0, "max": 100}]}
    r = client.post("/api/dashboards", json=d)
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["id"] and all(w["id"] for w in saved["widgets"])
    assert saved["widgets"][1]["x"] == 11 and saved["widgets"][1]["w"] == 1          # clamped into 12 columns
    assert saved["widgets"][0]["series"] == [{"key": "raw/s", "color": "#ff0000"}]
    assert client.post("/api/dashboards", json={**saved, "name": "Renamed"}).json()["name"] == "Renamed"
    assert [x["name"] for x in client.get("/api/dashboards").json()["dashboards"]] == ["Renamed"]
    assert client.get(f"/api/dashboards/{saved['id']}").json()["widgets"][0]["title"] == "Speed"
    assert client.post("/api/dashboards", json={"name": "Renamed"}).status_code == 400          # duplicate name
    assert client.post("/api/dashboards", json={"name": "x", "widgets": [{"type": "pie"}]}).status_code == 400
    assert client.post("/api/dashboards", json={"name": "../x"}).status_code == 400
    other = client.post("/api/dashboards", json={"name": "Other"}).json()

    raw = [{"name": "speed", "can_id": "18FEF100", "start_bit": 8, "length": 16, "factor": 0.125, "unit": "km/h"}]
    r = client.post("/api/raw_signals", json={"signals": raw})
    assert r.status_code == 200, r.text
    assert r.json()["signals"][0]["extended"] is True and r.json()["signals"][0]["can_id"] == 0x18FEF100
    assert client.post("/api/raw_signals", json={"signals": raw + raw}).status_code == 400      # duplicate name
    assert client.get("/api/raw_signals").json()["signals"][0]["unit"] == "km/h"

    assert client.post("/api/projects/save", json={"name": "p1"}).status_code == 200
    client.delete(f"/api/dashboards/{other['id']}")
    client.delete(f"/api/dashboards/{saved['id']}")
    client.post("/api/raw_signals", json={"signals": []})
    assert client.get("/api/dashboards").json()["dashboards"] == []
    assert client.post("/api/projects/load", json={"name": "p1"}).status_code == 200
    assert [x["name"] for x in client.get("/api/dashboards").json()["dashboards"]] == ["Renamed", "Other"]
    assert client.get("/api/raw_signals").json()["signals"][0]["name"] == "speed"
    proj = json.loads((client.svc.projects_dir / "p1.json").read_text(encoding="utf-8"))
    assert len(proj["dashboards"]) == 2 and proj["raw_signals"][0]["factor"] == 0.125


def test_dashboard_mcp_tools(client):
    hdr = {"accept": "application/json, text/event-stream", "content-type": "application/json"}

    def call(method, params, session=None, id_=1):
        h = {**hdr, **({"mcp-session-id": session} if session else {})}
        r = client.post("/mcp/", json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params}, headers=h)
        text = r.text
        if "data:" in text:
            text = next(l[5:] for l in text.splitlines() if l.startswith("data:"))
        return r, json.loads(text) if text.strip() else None

    r, _ = call("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "test", "version": "1"}})
    session = r.headers.get("mcp-session-id")
    call("notifications/initialized", {}, session)
    _, tools = call("tools/list", {}, session, 2)
    names = {t["name"] for t in tools["result"]["tools"]}
    assert {"list_dashboards", "get_dashboard", "save_dashboard", "delete_dashboard"} <= names
    _, res = call("tools/call", {"name": "save_dashboard", "arguments": {"dashboard": {
        "name": "From Claude", "widgets": [{"type": "number", "key": "raw/x"}]}}}, session, 3)
    assert "From Claude" in json.dumps(res)
    _, res = call("tools/call", {"name": "list_dashboards", "arguments": {}}, session, 4)
    assert "From Claude" in json.dumps(res)


def test_raw_signals_decode_the_simulator(client, channel):
    c = client
    c.post("/api/sim/start", json={"channel": channel})
    assert c.post("/api/connect", json={"interface": "virtual", "channel": channel, "bitrate": 250000}).status_code == 200
    r = c.post("/api/raw_signals", json={"signals": [
        {"name": "sine", "can_id": "0CF", "start_bit": 0, "length": 16, "unit": "rpm"},
        {"name": "cosine", "can_id": "185", "start_bit": 0, "length": 16, "signed": True, "factor": 0.01},
        {"name": "sine_ch5", "can_id": "0CF", "start_bit": 0, "length": 16, "channel": 5}]})
    assert r.status_code == 200, r.text
    time.sleep(1.5)
    sigs = {s["key"]: s for s in c.get("/api/signals", params={"prefix": "raw/"}).json()["signals"]}
    assert set(sigs) == {"raw/sine", "raw/cosine"}                # nothing on channel 5
    assert 200 <= sigs["raw/sine"]["value"] <= 1800 and sigs["raw/sine"]["unit"] == "rpm"
    assert -5.0 <= sigs["raw/cosine"]["value"] <= 5.0
    pts = c.get("/api/signals/history", params={"key": "raw/sine", "seconds": 5}).json()["points"]
    assert len(pts) > 50 and min(p[1] for p in pts) < max(p[1] for p in pts)        # 100 Hz, moving
    prev = c.post("/api/raw_signals/preview", json={"signals": [
        {"name": "p", "can_id": "0CF", "length": 16}, {"name": "q", "can_id": "7AB"}]}).json()["results"]
    assert 200 <= prev[0]["value"] <= 1800 and prev[1]["value"] is None and prev[1]["note"]


def test_review_bad_values_are_refused_or_dropped(tmp_path, monkeypatch):
    """NaN/inf, bools, malformed project values must never reach settings or the feed."""
    from fastapi.testclient import TestClient
    from canopener.server import create_app
    from canopener.service import Service
    svc = Service(load_plugins=False)
    with TestClient(create_app(svc), base_url="http://127.0.0.1:8775") as c:
        sig = {"name": "s", "can_id": "100", "start_bit": 0, "length": 8}
        for bad in ({"factor": "nan"}, {"max": "inf"}, {"channel": True}, {"start_bit": 2.7}):
            r = c.post("/api/raw_signals", json={"signals": [{**sig, **bad}]})
            assert r.status_code == 400, bad
        assert c.get("/api/raw_signals").status_code == 200
        r = c.post("/api/dashboards", content='{"name": "d", "widgets": [{"type": "plot", "x": 1e400, "window": 1e400}]}',
                   headers={"content-type": "application/json"})
        assert r.status_code == 400 and c.get("/api/dashboards").status_code == 200
        assert c.post("/api/dashboards", json={"id": "{bad id}", "name": "e", "widgets": []}).status_code == 400
        svc.settings["raw_signals"] = True            # what a broken project would have written before
        time.sleep(0.5)
        assert svc.raw._thread.is_alive()
    svc.signals.publish("x", time.time(), float("nan"))
    assert svc.signals.get("x")["value"] is None
