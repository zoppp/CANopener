"""The app layer: REST, the WebSocket feed, plugins and MCP, against the simulator on a virtual bus."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from canopener.server import create_app
from canopener.service import Service

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8775"
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.fixture
def client(monkeypatch, channel):
    monkeypatch.setenv("CANOPENER_PLUGIN_DIRS", str(ROOT / "plugins"))
    svc = Service()
    with TestClient(create_app(svc), base_url=BASE) as c:
        c.svc = svc
        c.post("/api/sim/start", json={"channel": channel})
        r = c.post("/api/connect", json={"interface": "virtual", "channel": channel, "bitrate": 250000})
        assert r.status_code == 200, r.text
        yield c


def _mcp(c, method, params=None, session=None, id_=1):
    h = dict(MCP_HEADERS)
    if session:
        h["mcp-session-id"] = session
    r = c.post("/mcp/", json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}, headers=h)
    text = r.text
    if text.startswith("event:") or "data:" in text:
        text = next(l[5:] for l in text.splitlines() if l.startswith("data:"))
    return r, json.loads(text) if text.strip() else None


def test_feed_sends_rows_trace_and_stats(client):
    with client.websocket_connect("/ws") as ws:
        got_rows = got_trace = False
        for _ in range(20):
            d = json.loads(ws.receive_text())
            got_rows |= any(e["rows"] for e in d["rows"].values())
            got_trace |= bool(d["trace"])
            if got_rows and got_trace:
                break
        assert got_rows and got_trace
        assert d["channels"][0]["state"] == "open"
        ws.send_text(json.dumps({"type": "trace", "filter": {"ids": "7FF"}}))
        for _ in range(3):
            d = json.loads(ws.receive_text())
        assert all(r[2] == 0x7FF for r in d["trace"])


def test_transmit_list_and_validation(client):
    r = client.post("/api/tx", json={"can_id": "123", "data": "11 22", "period_ms": 0})
    assert r.status_code == 200
    eid = r.json()["id"]
    assert client.post(f"/api/tx/{eid}/send").json()["count"] == 1
    assert client.post("/api/send", json={"id": "800", "data": "", "ext": False}).status_code == 400
    assert client.post("/api/tx", json={"can_id": "123", "data": "zz"}).status_code == 400


def test_record_and_list(client):
    client.post("/api/record/start", json={"name": "t1", "format": "trc"})
    time.sleep(0.5)
    st = client.post("/api/record/stop").json()
    assert st["written"] > 0
    assert any(f["name"] == "t1.trc" for f in client.get("/api/recordings").json()["files"])
    assert client.post("/api/record/start", json={"name": "../evil", "format": "trc"}).status_code == 400


def test_example_plugin_loads_and_sees_heartbeats(client):
    pl = client.get("/api/plugins").json()
    assert sorted(e["id"] for e in pl["extensions"]) == ["eds-editor", "heartbeat-monitor", "sniffer"] and not pl["errors"]
    time.sleep(1.2)
    nodes = client.get("/api/x/heartbeat-monitor/nodes").json()["nodes"]
    assert {n["node"] for n in nodes} >= {5, 10, 20}


def test_heartbeat_monitor_boot_ups_are_not_heartbeats(client, channel):
    """Node 11 only boots (like some motor drives): no period, never lost, flagged boot_only.
    Node 12 heartbeats every 100 ms, reboots and takes 1 s: the gap across the reset is not a period."""
    import can
    peer = can.Bus(interface="virtual", channel=channel, bitrate=250_000)
    send = lambda cid, b: peer.send(can.Message(arbitration_id=cid, data=[b], is_extended_id=False))  # noqa: E731
    try:
        for _ in range(4):
            send(0x70B, 0x00)
            send(0x70C, 0x05)
            time.sleep(0.1)
        send(0x70C, 0x00)
        time.sleep(1.0)
        send(0x70C, 0x05)
        time.sleep(0.6)                                       # > 3 x the 100 ms period of node 12 before it
        nodes = {n["node"]: n for n in client.get("/api/x/heartbeat-monitor/nodes").json()["nodes"]}
    finally:
        peer.shutdown()
    n11, n12 = nodes[11], nodes[12]
    assert (n11["boots"], n11["heartbeats"], n11["period_ms"], n11["lost"], n11["boot_only"]) == (4, 0, 0, False, True)
    assert n11["state"] == "boot-up" and n11["age_ms"] >= 1000
    assert (n12["boots"], n12["heartbeats"], n12["boot_only"]) == (1, 5, False)
    assert 50 < n12["period_ms"] < 200 and n12["lost"]        # with the 1 s gap it would be ~280


def test_mcp_lists_read_and_write_tools(client):
    r, init = _mcp(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                          "clientInfo": {"name": "test", "version": "1"}})
    assert r.status_code == 200, r.text
    session = r.headers.get("mcp-session-id")
    _mcp(client, "notifications/initialized", session=session)
    r, tools = _mcp(client, "tools/list", session=session, id_=2)
    names = {t["name"] for t in tools["result"]["tools"]}
    assert {"get_status", "get_trace", "get_set_node_id_preview", "run_node_plan", "send_frame",
            "get_heartbeat_nodes"} <= names
    r, res = _mcp(client, "tools/call", {"name": "get_bus_stats", "arguments": {}}, session=session, id_=3)
    assert "channels" in json.dumps(res)


def test_local_only(client):
    assert client.get("/api/about", headers={"host": "evil.example"}).status_code == 403
    assert client.post("/api/tx", json={"can_id": "1"}, headers={"origin": "http://evil.example"}).status_code == 403


def test_feed_refreshes_table_after_reconnect_on_same_index(client, channel):
    with client.websocket_connect("/ws") as ws:
        for _ in range(5):
            json.loads(ws.receive_text())
        client.post("/api/connect", json={"interface": "virtual", "channel": channel, "bitrate": 500000, "index": 0})
        full = False
        for _ in range(10):
            d = json.loads(ws.receive_text())
            full |= bool(d["rows"].get("0", {}).get("full"))
        assert full                                   # the new channel object's table is sent whole


def test_files_outside_recordings_are_refused(client):
    assert client.get("/api/recordings/C:%5CWindows%5Cwin.ini").status_code == 400
    assert client.post("/api/file/open", json={"path": "C:\\Windows\\win.ini"}).status_code == 400
    client.post("/api/record/start", json={"name": "same", "format": "trc"})
    client.post("/api/record/stop")
    client.post("/api/record/start", json={"name": "same", "format": "trc"})
    st = client.post("/api/record/stop").json()
    assert st["path"].endswith("same-1.trc")          # never overwrites


def test_run_requires_the_previewed_title(client):
    p = client.post("/api/nodes/plan/custom", json={"channel": 0, "steps": [{"op": "raw", "id": "0x123", "data": "01"}],
                                                     "title": "Poke 123"}).json()
    r = client.post("/api/nodes/run", json={"token": p["token"], "title": "something else"})
    assert r.status_code == 400 and "Poke 123" in r.json()["error"]
    assert client.post("/api/nodes/run", json={"token": p["token"], "title": "Poke 123"}).status_code == 200


def test_remote_mode_still_refuses_other_sites(client):
    client.app.state.allow_remote = True
    try:
        h = {"host": "10.0.0.5:8775"}
        assert client.get("/api/about", headers=h).status_code == 200
        assert client.post("/api/tx/stop_all", headers={**h, "origin": "http://evil.example"}).status_code == 403
        assert client.post("/api/tx/stop_all", headers={**h, "origin": "http://10.0.0.5:8775"}).status_code == 200
    finally:
        client.app.state.allow_remote = False


def test_text_booleans_are_parsed_strictly(client):
    """bool("false") is True: "enabled": "false" used to start cyclic sending and "ext": "false" sent 29-bit frames."""
    r = client.post("/api/tx", json={"can_id": "100", "period_ms": 1000, "enabled": "false", "ext": "false"})
    assert r.status_code == 200, r.text
    e = r.json()
    assert e["enabled"] is False and e["ext"] is False
    r = client.patch(f"/api/tx/{e['id']}", json={"enabled": "true"})
    assert r.json()["enabled"] is True
    client.post("/api/tx/stop_all")
    for bad in ({"enabled": "nope"}, {"ext": "maybe"}, {"rtr": 2}):
        r = client.post("/api/tx", json={"can_id": "100", **bad})
        assert r.status_code == 400 and "is not true or false" in r.json()["error"], (bad, r.text)
    tap_before = client.svc.engine.channels[0].tx_frames
    r = client.post("/api/send", json={"id": "100", "data": "01", "ext": "false", "rtr": "false"})
    assert r.status_code == 200
    r = client.post("/api/send", json={"id": "100", "ext": "yes please"})
    assert r.status_code == 400
    assert client.svc.engine.channels[0].tx_frames == tap_before + 1
    from canopener.core.channel import F_EXT, F_RTR, F_TX
    sent = [r_ for r_ in client.svc.engine.channels[0].rows.values() if r_.flags & F_TX and r_.can_id == 0x100]
    assert sent and all(not r_.flags & (F_EXT | F_RTR) for r_ in sent)       # an 11-bit data frame


def test_cycle_time_below_1_ms_is_refused(client):
    """period_ms 0.001 ran back to back (a bus flood); 0 stays "send on click"."""
    for bad in (0.001, 0.5, -1, "nan", "inf"):
        r = client.post("/api/tx", json={"can_id": "100", "period_ms": bad, "enabled": True})
        assert r.status_code == 400 and "cycle time" in r.json()["error"], (bad, r.text)
    for ok in (0, 1, 2.5):
        assert client.post("/api/tx", json={"can_id": "100", "period_ms": ok}).status_code == 200
    e = client.post("/api/tx", json={"can_id": "100", "period_ms": 10}).json()
    assert client.patch(f"/api/tx/{e['id']}", json={"period_ms": 0.01}).status_code == 400
    assert not any(x.enabled for x in client.svc.engine.tx.entries)


def test_file_names_never_open_a_path_outside_the_recordings_folder(client, monkeypatch):
    r"""A \\host\share name reached Path.resolve(), which on Windows opens an SMB connection (and hands the login
    hash to that host); any web page could ask with an <img> GET. Outside paths are refused by their text."""
    import ntpath
    opened = []
    real = getattr(ntpath, "_getfinalpathname", None)

    def spy(path):
        opened.append(str(path))
        if real is None:
            raise OSError("no _getfinalpathname")
        return real(path)
    monkeypatch.setattr(ntpath, "_getfinalpathname", spy, raising=False)
    evil = "http://evil.example"
    for name in (r"\\attacker.example\share\x.trc", "//attacker.example/share/x.trc", r"C:\Windows\win.ini",
                 r"..\..\secret.trc", r"\\?\UNC\attacker.example\share\x.trc"):
        r = client.get(f"/api/recordings/{name.replace(chr(92), '%5C')}", headers={"origin": evil})
        assert r.status_code in (400, 404), (name, r.status_code)
        r = client.post("/api/file/open", json={"path": name})
        assert r.status_code == 400, (name, r.text)
    assert not [p for p in opened if "attacker" in p or "Windows" in p], opened
    from canopener.paths import recordings_dir
    (recordings_dir() / "ok.trc").write_text("")
    r = client.post("/api/file/open", json={"path": str(recordings_dir() / "ok.trc")})
    assert "recordings folder" not in r.text                              # a path inside the folder still works
