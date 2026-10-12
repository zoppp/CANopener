"""The EDS editor plugin: reading, checking and writing EDS/DCF, DCF -> Nodes steps, and the read-only device work
(compare, create from device) against a real canopen SDO server on a virtual bus."""

import io
import json
import time
from pathlib import Path

import can
import canopen
import pytest
from fastapi.testclient import TestClient

from canopener.ext import load_plugin

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = load_plugin(ROOT / "plugins" / "eds-editor")
import canopener_plugin_eds_editor.device as D  # noqa: E402  (the plugin's own modules, loaded above)
import canopener_plugin_eds_editor.edsdoc as E  # noqa: E402


def od_of(doc, node_id):
    s = io.StringIO(E.write(doc, "eds", "t.eds"))
    s.name = "t.eds"
    return canopen.import_od(s, node_id)


def errors(doc):
    return [p for p in E.check(doc) if p["level"] == "error"]


# ------------------------------------------------------------------------------------------- values

def test_parse_int():
    assert E.parse_int("0x180") == 0x180
    assert E.parse_int("$NODEID+0x180", 5) == 0x185
    assert E.parse_int("0x200 + $NODEID", 3) == 0x203
    assert E.parse_int("010") == 8                       # CiA 306: leading 0 is octal
    assert E.parse_int("-12") == -12
    with pytest.raises(ValueError):
        E.parse_int("abc")
    assert E.value_error("256", 0x0005) and not E.value_error("255", 0x0005)
    assert E.value_error("$NODEID+0x81", 0x0005)          # node 127 overflows an UNSIGNED8
    assert E.format_value(b"\x80\x01\x00\x00", 0x0007) == "0x00000180"
    assert E.same_value("$NODEID+0x180", "0x00000185", 0x0007, 5)


# ------------------------------------------------------------------------------------------- read / write

@pytest.mark.parametrize("profile", ["301", "401", "402"])
def test_templates_are_clean_and_round_trip(profile):
    doc = E.template(profile, "ACME", "0x00000ABC", "Widget", "0x00000002")
    assert errors(doc) == []
    od = od_of(doc, 5)                                   # canopen reads what we write
    assert od[0x1800][1].default == 0x185 and od[0x1018][1].default == 0xABC
    again = E.read(E.write(doc, "eds", "t.eds").encode(), "t.eds")
    strip = lambda d: [{k: v for k, v in o.items() if k != "guess"} for o in d["objects"]]  # noqa: E731
    assert strip(again) == strip(doc) and again["notes"] == []


VENDOR_EDS = """\
[FileInfo]
FileName=vendor.eds
CreatedBy=Vendor tool 1.2

[DeviceInfo]
VendorName=Vendor
VendorNumber=0x123
ProductNumber=0x5
BaudRate_250=1

[Comments]
Lines=1
Line1=keep me

[MandatoryObjects]
SupportedObjects=2
1=0x1000
2=0x1018

[1000]
ParameterName=Device type
DataType=0x0007
AccessType=ro
DefaultValue=0
PDOMapping=0
ObjFlags=1

[1018]
ParameterName=Identity
ObjectType=0x9
SubNumber=2

[1018sub0]
ParameterName=Count
DataType=0x0005
AccessType=ro
DefaultValue=1

[1018sub1]
ParameterName=Vendor
DataType=0x0007
AccessType=ro
DefaultValue=0x123

[OptionalObjects]
SupportedObjects=1
1=0x1001

[1001]
ParameterName=Error register
DataType=0x0005
AccessType=ro
PDOMapping=1

[ManufacturerObjects]
SupportedObjects=1
1=0x2000

[2000]
ParameterName=Outputs
ObjectType=0x8
DataType=0x0006
AccessType=rw
PDOMapping=1
DefaultValue=7
CompactSubObj=3

[2000Name]
NrOfEntries=1
2=Second output

[Tools]
Editor=Something
"""


def test_read_keeps_unknown_keys_and_expands_compact():
    doc = E.read(VENDOR_EDS.encode(), "vendor.eds")
    assert E.find(doc, 0x1000)["extra"] == {"ObjFlags": "1"}
    arr = E.find(doc, 0x2000)
    assert [s["sub"] for s in arr["subs"]] == [0, 1, 2, 3]
    assert arr["subs"][2]["name"] == "Second output" and arr["subs"][1]["default"] == "7"
    assert errors(doc) == []
    text = E.write(doc, "eds", "vendor.eds")
    assert "ObjFlags=1" in text and "[Tools]\r\nEditor=Something" in text and "Line1=keep me" in text
    assert "CreatedBy=Vendor tool 1.2" in text and "[2000sub2]\r\nParameterName=Second output" in text
    assert "ParameterValue" not in text and "[DeviceComissioning]" not in text
    od_of(doc, 1)


def test_check_finds_problems():
    doc = E.template("402")
    E.find_var(doc, 0x1A00, 0)["default"] = "3"
    E.find_var(doc, 0x1A00, 2)["default"] = "0x60640020"            # 16 + 32 + 32 bits ...
    E.find_var(doc, 0x1A00, 3)["default"] = "0x605A0020"            # ... and 0x605A (not PDO-mappable, 16 bits)
    E.find_var(doc, 0x1A01, 1)["default"] = "0x60410020"            # statusword mapped with 32 bits
    E.find_var(doc, 0x1601, 1)["default"] = "0x60410010"            # RPDO maps a read-only object
    E.find_var(doc, 0x1801, 1)["default"] = "$NODEID+0x180"          # same COB-ID as TPDO 1
    E.find(doc, 0x6060)["default"] = "200"                            # INTEGER8
    E.find_var(doc, 0x1018, 0)["default"] = "7"
    doc["objects"] = [o for o in doc["objects"] if o["index"] != 0x1001]
    texts = " | ".join(p["text"] for p in errors(doc))
    for want in ("0x605A:00 (Quick stop option code), which is not PDO-mappable", "more than the 64",
                 "with 32 bits, but it is UNSIGNED16", "an RPDO needs a writable object",
                 "TPDO 2 uses the same COB-ID as TPDO 1", "200 is outside INTEGER8",
                 "sub-index 0 says 7 but the highest sub-index is 4", "mandatory object 0x1001 is missing"):
        assert want in texts, want


def test_dcf_write_order_for_pdos():
    doc = E.template("402")
    doc["format"] = "dcf"
    E.find_var(doc, 0x1800, 5)["value"] = "100"                      # TPDO 1 event timer
    E.find_var(doc, 0x1A00, 0)["value"] = "2"                        # TPDO 1: statusword + position
    E.find_var(doc, 0x1A00, 2)["value"] = "0x60640020"
    E.find(doc, 0x6081)["value"] = "5000"                            # profile velocity
    E.find(doc, 0x6041)["value"] = "1"                               # read-only: skipped
    E.find(doc, 0x6083)["value"] = "0"                               # same as the default: not written
    r = D.dcf_steps(doc, 7)
    w = [(s["index"], s["sub"], s["value"]) for s in r["steps"] if s["op"] == "sdo_write"]
    assert r["steps"][0]["op"] == "nmt" and r["steps"][-1]["op"] == "store"
    assert w == [("0x1800", 1, 0x80000187), ("0x1A00", 0, 0), ("0x1A00", 1, 0x60410010), ("0x1A00", 2, 0x60640020),
                 ("0x1A00", 0, 2), ("0x1800", 5, 100), ("0x1800", 1, 0x187), ("0x6081", 0, 5000)]
    assert any("0x6041" in s for s in r["skipped"]) and r["vars"] == {"OLD_ID": 7}
    assert "ParameterValue=5000" in E.write(doc, "dcf", "x.dcf")


def test_pdo_decode_encode_and_add():
    doc = E.template("402")
    assert E.encode_entry(0x6041, 0, 16) == 0x60410010 and E.decode_entry(0x607A0020) == (0x607A, 0, 32)
    c = E.decode_cob("$NODEID+0x80000180", 5)
    assert c["valid"] is False and c["node_relative"] and c["id"] == 0x180 and c["id_for_node"] == 0x185
    assert E.decode_cob("0x20000000", 1)["extended"] and E.decode_cob("0x40000181")["rtr_allowed"] is False
    assert E.transmission_text(10) == "synchronous, every 10 SYNC" and E.transmission_text(254).startswith("event")
    tp3 = next(p for p in E.pdos(doc, 5) if p["kind"] == "tpdo" and p["number"] == 3)
    assert tp3["cob"]["id_for_node"] == 0x385 and tp3["bits"] == 48
    assert [(e["name"], e["start_bit"], e["bits"]) for e in tp3["entries"]] == [("Statusword", 0, 16),
                                                                                ("Position actual value", 16, 32)]
    assert E.add_pdo(doc, "tpdo") == 5 and E.add_pdo(doc, "rpdo") == 5
    tp5 = next(p for p in E.pdos(doc) if p["kind"] == "tpdo" and p["number"] == 5)
    assert tp5["cob"]["valid"] is False and tp5["entries"] == []         # no predefined COB-ID past PDO 4
    assert errors(doc) == []
    doc["format"] = "dcf"                                                 # a DCF value wins over the default
    E.find_var(doc, 0x1A02, 0)["value"] = "1"
    assert next(p for p in E.pdos(doc) if p["kind"] == "tpdo" and p["number"] == 3)["bits"] == 16


# ------------------------------------------------------------------------------------------- device (read only)

@pytest.fixture
def device(channel):
    """A canopen SDO server serving the 401 template plus two manufacturer objects, node 7."""
    doc = E.template("401", "ACME", "0x00000ABC", "IO box", "0x00000009")
    doc["objects"].append(E.new_object(0x2345, "Speed", data_type=0x0006, default="1234"))
    rec = E._rec(0x2400, "Setup", [("Mode", 0x0005, "rw", "3", False), ("Limit", 0x0007, "rw", "0x10000", False)])
    doc["objects"].append(rec)
    doc["objects"].sort(key=lambda o: o["index"])
    slave = canopen.Network()
    slave.connect(interface="virtual", channel=channel, receive_own_messages=False)
    slave.create_node(7, od_of(doc, 7))
    tool = canopen.Network(bus=can.Bus(interface="virtual", channel=channel))
    tool.connect()
    remote = tool.add_node(7)
    remote.sdo.RESPONSE_TIMEOUT = 0.25
    from canopen.sdo import SdoAbortedError, SdoCommunicationError

    def upload(i, s):
        try:
            return remote.sdo.upload(i, s)
        except SdoAbortedError as exc:
            raise D.Abort(exc.code)
        except SdoCommunicationError as exc:
            raise D.NoAnswer(str(exc))
    yield doc, upload
    tool.disconnect()
    slave.disconnect()


def test_compare_with_device(device):
    doc, upload = device
    E.find(doc, 0x2345)["default"] = "99"
    doc["objects"].append(E.new_object(0x2ABC, "Not on the device", data_type=0x0005, default="1"))
    r = D.compare(doc, upload, 7)
    rows = r["rows"]
    assert r["stopped"] == ""
    by = {(r["index"], r["sub"]): r for r in rows}
    assert by[(0x2345, 0)]["status"] == "differs" and by[(0x2345, 0)]["device"] == "0x04D2"
    assert by[(0x2ABC, 0)]["status"] == "missing"
    assert by[(0x1800, 1)]["status"] == "same"                       # $NODEID+0x180 == 0x187 on node 7
    assert by[(0x1018, 1)]["status"] == "same"


def test_create_from_device(device):
    _, upload = device
    ranges = ((0x1000, 0x1020), (0x1200, 0x1200), (0x1400, 0x1401), (0x1600, 0x1600), (0x1800, 0x1800),
              (0x1A00, 0x1A00), (0x2340, 0x2410), (0x6000, 0x6000), (0x6200, 0x6200))
    doc = D.probe(upload, ranges, node_id=7)
    idx = [o["index"] for o in doc["objects"]]
    assert {0x1000, 0x1001, 0x1018, 0x1200, 0x1400, 0x1401, 0x1600, 0x1800, 0x1A00, 0x2345, 0x2400, 0x6000} <= set(idx)
    assert E.find(doc, 0x1018)["name"] == "Identity object" and not E.find(doc, 0x1018).get("guess")
    speed = E.find(doc, 0x2345)
    assert speed["data_type"] == 0x0006 and speed["default"] == "0x04D2" and "type" in speed["guess"]
    setup = E.find(doc, 0x2400)
    assert setup["object_type"] == E.RECORD and [s["sub"] for s in setup["subs"]] == [0, 1, 2]
    assert E.find_var(doc, 0x1800, 1)["default"] == "$NODEID+0x180"
    assert E.section(doc, "DeviceInfo")["VendorNumber"] == "0x00000ABC"
    assert errors(doc) == []
    assert any("guessed" in p["text"] for p in E.check(doc))


def test_probe_keeps_what_it_read_when_the_node_is_gone():
    def upload(i, s):
        if i <= 0x1001:
            return b"\x00"
        raise D.NoAnswer("timeout")
    doc = D.probe(upload, ((0x1000, 0x1010),))
    assert [o["index"] for o in doc["objects"]] == [0x1000, 0x1001]
    assert any("stopped answering" in n["text"] for n in doc["notes"])


def test_probe_ignores_a_node_that_aborts_everything():
    calls = []

    def general_error(i, s):                       # 0x08000000 for every index: nothing is there
        calls.append(i)
        raise D.Abort(0x08000000)
    assert D.probe(general_error, ((0x2000, 0x20FF),))["objects"] == []

    def busy(i, s):                                # "not in this device state" for every index
        raise D.Abort(0x08000022)
    doc = D.probe(busy, ((0x2000, 0x23FF),))
    assert doc["objects"] == [] and any("almost every index" in n["text"] for n in doc["notes"])


def test_unsized_replies_are_cut_to_the_type():
    assert D.fit(D.Reply(b"\x05\x00\x00\x00", False), 0x0005) == b"\x05"
    assert D.fit(b"\x05\x00\x00\x00", 0x0006) == b"\x05\x00"           # zero padding
    assert D.fit(D.Reply(b"\x05\x00\x00\x01", True), 0x0005) == b"\x05\x00\x00\x01"   # really too long


def test_signed_hex_and_strings():
    assert E.value_error("0xFFFF", 0x0003) is None and E.int_value("0xFFFF", 0x0003) == -1
    assert E.same_value("0xFFFF", "-1", 0x0003)
    v = E.new_var("Min", 0x0004, low="0x80000000", high="0x7FFFFFFF", default="-5")
    out = []
    E._check_var(v, 0x2000, None, "eds", set(), out)
    assert out == []
    doc = E.template("301")
    doc["format"] = "dcf"
    E.find(doc, 0x1008)["access"] = "rw"
    E.find(doc, 0x1008)["value"] = "0012"
    E.find(doc, 0x1014)["value"] = "0xA0"                            # EMCY: switched off first
    st = [s for s in D.dcf_steps(doc, 5)["steps"] if s["op"] == "sdo_write"]
    assert st[0] == {"op": "sdo_write", "node": "$OLD_ID", "index": "0x1008", "sub": 0, "type": "bytes", "value": "30 30 31 32"}
    assert [(s["index"], s["value"]) for s in st[1:]] == [("0x1014", 0x800000A0), ("0x1014", 0xA0)]


def test_compact_sub_obj_is_bounded():
    text = b"[2000]\nParameterName=X\nObjectType=0x8\nDataType=0x0005\nAccessType=rw\nCompactSubObj=0x10000000\n"
    doc = E.read(text, "x.eds")
    assert E.find(doc, 0x2000)["subs"] == [] and any("not expanded" in n["text"] for n in doc["notes"])


# ------------------------------------------------------------------------------------------- app

@pytest.fixture
def client(monkeypatch, channel):
    from canopener.server import create_app
    from canopener.service import Service
    monkeypatch.setenv("CANOPENER_PLUGIN_DIRS", str(ROOT / "plugins"))
    svc = Service()
    with TestClient(create_app(svc), base_url="http://127.0.0.1:8775") as c:
        c.post("/api/sim/start", json={"channel": channel})
        assert c.post("/api/connect", json={"interface": "virtual", "channel": channel, "bitrate": 250000}).status_code == 200
        yield c


def _wait_job(c, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get("/api/nodes/job").json()
        if j.get("state") != "running":
            return j
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_api_new_save_open_and_steps(client):
    c = client
    assert "eds-editor" in [e["id"] for e in c.get("/api/about").json()["extensions"]]
    assert c.get("/ui/x/eds-editor/eds-editor.js").status_code == 200
    d = c.post("/api/x/eds-editor/new", json={"profile": "402", "product_name": "Drive", "file_name": "drive"}).json()
    assert d["doc"]["file_name"] == "drive.eds" and not [p for p in d["problems"] if p["level"] == "error"]
    r = c.post("/api/x/eds-editor/save", json={"doc": d["doc"], "name": "drive", "format": "eds"})
    assert r.status_code == 200 and r.json()["name"] == "drive.eds"
    assert c.post("/api/x/eds-editor/save", json={"doc": d["doc"], "name": "drive", "format": "eds"}).status_code == 409
    assert c.post("/api/x/eds-editor/save", json={"doc": d["doc"], "name": "../x", "format": "eds"}).json()["name"] == "x.eds"
    files = c.get("/api/x/eds-editor/files").json()["files"]
    path = next(f["path"] for f in files if f["name"] == "drive.eds")
    o = c.post("/api/x/eds-editor/open", json={"path": path}).json()
    assert len(o["doc"]["objects"]) == len(d["doc"]["objects"])
    assert c.post("/api/x/eds-editor/open", json={"path": str(ROOT / "README.md")}).status_code == 400
    up = c.post("/api/x/eds-editor/upload", files={"file": ("node.xdd", (ROOT / "tests/data/test_node.xdd").read_bytes())})
    assert up.status_code == 200 and up.json()["doc"]["file_name"] == "node.eds"
    doc = o["doc"]
    E.find(doc, 0x6081)["value"] = "100"
    a = c.post("/api/x/eds-editor/pdo/add", json={"doc": doc, "kind": "tpdo"}).json()
    assert a["number"] == 5 and [o["index"] for o in a["objects"]] == [0x1804, 0x1A04]
    assert c.post("/api/x/eds-editor/pdo/add", json={"doc": doc, "kind": "xpdo"}).status_code == 400
    s = c.post("/api/x/eds-editor/dcf-steps", json={"doc": doc, "node": 5}).json()
    assert s["count"] == 1 and s["steps"][1]["index"] == "0x6081"
    t = c.post("/api/x/eds-editor/text", json={"doc": doc, "format": "dcf"}).json()
    assert t["name"] == "drive.dcf" and "ParameterValue=100" in t["text"]


def test_api_device_jobs_against_the_simulator(client):
    c = client
    j = c.post("/api/x/eds-editor/device/probe", json={"channel": 0, "node": 5, "ranges": "1000-1020, 2100-2110"}).json()
    done = _wait_job(c)
    assert done["state"] == "done", done
    r = c.get(f"/api/x/eds-editor/device/result?job={j['id']}").json()
    idx = {o["index"] for o in r["doc"]["objects"]}
    assert {0x1000, 0x1008, 0x1018, 0x2100, 0x2101, 0x2110} <= idx
    assert E.find(r["doc"], 0x1008)["default"] == "Demo device A (SDO)"
    j = c.post("/api/x/eds-editor/device/compare", json={"channel": 0, "node": 5, "doc": r["doc"]}).json()
    assert _wait_job(c)["state"] == "done"
    rows = c.get(f"/api/x/eds-editor/device/result?job={j['id']}").json()["rows"]
    # the simulator's records have no sub-index 0 (real devices do)
    assert rows and all(x["status"] in ("same", "read", "error") or (x["status"] == "missing" and x["sub"] == 0)
                        for x in rows), rows
    bad = c.post("/api/x/eds-editor/device/probe", json={"channel": 0, "node": 0})
    assert bad.status_code == 400


def test_mcp_tools_listed(client):
    h = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    r = client.post("/mcp/", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    sid = r.headers.get("mcp-session-id")
    h2 = dict(h, **({"mcp-session-id": sid} if sid else {}))
    client.post("/mcp/", headers=h2, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    r = client.post("/mcp/", headers=h2, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    text = r.text
    if "data:" in text:
        text = next(l[5:] for l in text.splitlines() if l.startswith("data:"))
    names = {t["name"] for t in json.loads(text)["result"]["tools"]}
    assert {"get_eds_problems", "list_eds_objects", "get_eds_pdos", "read_node_vs_eds", "create_eds_from_node"} <= names
