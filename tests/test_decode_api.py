"""Decoder REST endpoints and MCP tools, against the simulator on a virtual bus."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from canopener.server import create_app
from canopener.service import Service

DATA = Path(__file__).resolve().parent / "data"
BASE = "http://127.0.0.1:8775"
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}


@pytest.fixture
def client(channel):
    svc = Service(load_plugins=False)
    with TestClient(create_app(svc), base_url=BASE) as c:
        c.svc = svc
        c.post("/api/sim/start", json={"channel": channel})
        r = c.post("/api/connect", json={"interface": "virtual", "channel": channel, "bitrate": 250000})
        assert r.status_code == 200, r.text
        yield c


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_dbc_load_upload_encode_and_transmit(client):
    r = client.post("/api/decode/dbc/load", json={"path": str(DATA / "test.dbc")})
    assert r.status_code == 200 and r.json()["messages"] == 4
    assert client.post("/api/decode/dbc/load", json={"path": "C:/nope.dbc"}).status_code == 400
    info = client.get("/api/decode/dbc").json()
    eng = next(m for m in info["messages"] if m["name"] == "Engine")
    assert eng["id"] == 0x100 and {s["name"] for s in eng["signals"]} == {"EngineSpeed", "Gear", "CoolantTemp"}
    r = client.post("/api/decode/dbc/encode", json={"message": "Engine", "signals": {"EngineSpeed": 1000}})
    assert r.json() == {"id": 0x100, "ext": False, "data": "A0 0F 00 00 00 00 00 00"}
    assert client.post("/api/decode/dbc/encode", json={"message": "Nope", "signals": {}}).status_code == 400
    # transmit by signal value; the sent frame is decoded back (Tx rows decode too)
    e = client.post("/api/tx", json={"dbc_message": "Engine", "signals": {"EngineSpeed": 750, "Gear": 1}}).json()
    assert e["can_id"] == 0x100 and e["dbc_message"] == "Engine"
    client.post(f"/api/tx/{e['id']}/send")
    assert wait_for(lambda: (client.svc.signals.get("dbc/Engine/EngineSpeed") or {}).get("value") == 750.0)
    texts = client.get("/api/decode/text").json()["text"]
    assert any("Gear=First" in t for t in texts.values())
    # upload: the file is kept in the app folder and loaded for channel 0 only
    r = client.post("/api/decode/dbc/upload", files={"file": ("up.dbc", (DATA / "test.dbc").read_bytes())},
                    data={"channel": "0"})
    assert r.status_code == 200 and r.json()["channel"] == 0
    assert client.post("/api/decode/dbc/upload", files={"file": ("x.exe", b"MZ")}).status_code == 400
    files = client.get("/api/decode").json()["dbc"]
    assert len(files) == 2
    client.post("/api/decode/dbc/remove", json={"path": r.json()["path"], "channel": 0})
    assert len(client.get("/api/decode").json()["dbc"]) == 1


def test_canopen_endpoints(client):
    eds = (DATA / "test_node.eds").read_bytes()
    r = client.post("/api/decode/canopen/eds/upload", files={"file": ("node.eds", eds)}, data={"node": "5"})
    assert r.status_code == 200 and r.json()["pdos"][0]["cob_id"] == 0x185
    assert wait_for(lambda: client.svc.signals.get("canopen/5/TPDO1.Counter") is not None)
    od = client.get("/api/decode/canopen/od", params={"node": 5}).json()["objects"]
    assert any(o["name"] == "Position" and o["type"] == "i16" for o in od)
    r = client.post("/api/decode/canopen/sdo_read", json={"node": 5, "index": "0x1018", "sub": 1})
    assert r.status_code == 200 and r.json()["value"] == 0x0CA0
    plan = client.post("/api/decode/canopen/plan/sdo_write",
                       json={"node": 5, "index": 0x2202, "sub": 0, "value": 3}).json()
    assert plan["token"] and plan["preview"][0]["frames"][0]["data"].startswith("2F 02 22 00 03")
    assert client.post("/api/decode/canopen/plan/sdo_write",
                       json={"node": 5, "index": 0x1000, "sub": 0, "value": 3}).status_code == 400
    plan = client.post("/api/decode/canopen/plan/nmt", json={"node": 5, "command": "preop"}).json()
    assert plan["preview"][0]["frames"][0]["data"] == "80 05"
    assert client.post("/api/nodes/run", json={"token": plan["token"]}).status_code == 200
    assert client.get("/api/decode/canopen").json()["nodes"][0]["node"] == 5
    assert wait_for(lambda: any(n["node"] == 10 for n in client.get("/api/decode/canopen").json()["nmt"]))
    client.post("/api/decode/canopen/eds/remove", json={"node": 5})
    assert client.get("/api/decode/canopen").json()["nodes"] == []


def test_j1939_endpoints(client):
    assert client.post("/api/decode/j1939", json={"channel": 0, "enabled": True}).json()["channels"] == [0]
    # the simulator sends 0x18FEF100 (CCVS) and 0x18F00400 (EEC1)
    def decoded():
        return client.get("/api/decode/text").json()["text"]
    assert wait_for(lambda: any("PGN 65265 CCVS SA 0x00" in t for t in decoded().values()))
    assert client.get("/api/decode/j1939").json()["channels"] == [0]
    client.post("/api/decode/j1939", json={"channel": 0, "enabled": False})
    assert client.get("/api/decode").json()["j1939"] == []


def _mcp(c, method, params=None, session=None, id_=1):
    h = dict(MCP_HEADERS)
    if session:
        h["mcp-session-id"] = session
    r = c.post("/mcp/", json={"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}, headers=h)
    text = r.text
    if text.startswith("event:") or "data:" in text:
        text = next(l[5:] for l in text.splitlines() if l.startswith("data:"))
    return r, json.loads(text) if text.strip() else None


def test_mcp_decode_tools(client):
    r, _ = _mcp(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                       "clientInfo": {"name": "test", "version": "1"}})
    session = r.headers.get("mcp-session-id")
    _mcp(client, "notifications/initialized", session=session)
    r, tools = _mcp(client, "tools/list", session=session, id_=2)
    names = {t["name"] for t in tools["result"]["tools"]}
    assert {"list_dbc_messages", "get_j1939_state", "get_canopen_state", "read_sdo", "get_sdo_write_preview",
            "get_nmt_preview", "load_dbc_file", "assign_eds_file"} <= names
    r, res = _mcp(client, "tools/call", {"name": "load_dbc_file", "arguments": {"path": str(DATA / "test.dbc")}},
                  session=session, id_=3)
    assert json.loads(res["result"]["content"][0]["text"])["messages"] == 4
    r, res = _mcp(client, "tools/call", {"name": "get_nmt_preview", "arguments": {"node": 5, "command": "start"}},
                  session=session, id_=4)
    assert "token" in json.dumps(res) and "01 05" in json.dumps(res)
