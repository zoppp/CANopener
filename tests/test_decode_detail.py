"""Decode tab detail views: the SDO sniffer (expedited, segmented, aborts, block, source), sniffed values in the
object dictionary, the node detail (PDO objects from the receive table, nodes without an EDS), DBC message and J1939
source details, and their REST/MCP surface against the simulator."""

import struct
import time
from pathlib import Path

import can
import pytest

from canopener.core.channel import F_EXT, F_TX
from canopener.service import Service

DATA = Path(__file__).resolve().parent / "data"
EDS = str(DATA / "test_node.eds")
DBC = str(DATA / "test.dbc")


@pytest.fixture
def svc():
    s = Service(load_plugins=False)
    yield s
    s.shutdown()


@pytest.fixture
def sim_svc(svc, channel):
    svc.start_sim(channel)
    svc.connect({"interface": "virtual", "channel": channel, "bitrate": 250000})
    return svc


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


_t = [1000.0]


def fr(cid, hexdata, tx=False):
    """One (channel 0, frame) item; timestamps 10 ms apart."""
    _t[0] += 0.01
    data = bytes.fromhex(hexdata)
    return 0, (_t[0], cid, (F_EXT if cid > 0x7FF else 0) | (F_TX if tx else 0), len(data), data)


def sdo_log(svc, node=5):
    return svc.decode.canopen_node(node)["sdo"]


# ------------------------------------------------------------------------------------------- SDO sniffer

def test_expedited_read_by_another_master_decoded_with_the_eds(svc):
    svc.decode.assign_eds(5, EDS)
    svc.decode.process([fr(0x605, "4018100100000000"), fr(0x585, "43181001A00C0000")])
    e = sdo_log(svc)[0]
    assert e["source"] == "bus" and e["op"] == "read" and e["result"] == "ok"
    assert (e["index"], e["sub"], e["value"], e["size"]) == (0x1018, 1, 0x0CA0, 4) and e["name"] == "Identity.Vendor id"
    assert e["ms"] == 10.0 and e["raw"] == "A0 0C 00 00"
    v = svc.decode.canopen_node(5)["values"][f"{0x1018}:1"]
    assert v["value"] == 0x0CA0 and v["source"] == "bus" and v["op"] == "read"


def test_expedited_write_by_this_tool_is_marked_canopener(svc):
    svc.decode.assign_eds(5, EDS)
    svc.decode.process([fr(0x605, "2F02220007000000", tx=True), fr(0x585, "6002220000000000")])
    e = sdo_log(svc)[0]
    assert e["source"] == "CANopener" and e["op"] == "write" and e["value"] == 7 and e["name"] == "Command"
    assert "SDO write node 5 0x2202:00 done" in svc.decode.text_map()[f"0:{0x585}"]


def test_segmented_upload_reassembled_and_toggle_checked(svc):
    svc.decode.process([fr(0x705, "05")])                         # heartbeat: node 5 known, no EDS
    name = b"Demo device A (SDO)"                                 # 19 bytes: 7 + 7 + 5
    svc.decode.process([fr(0x605, "4008100000000000"), fr(0x585, "41081000" + struct.pack("<I", 19).hex()),
                        fr(0x605, "6000000000000000"), fr(0x585, "00" + name[:7].hex()),
                        fr(0x605, "7000000000000000"), fr(0x585, "10" + name[7:14].hex()),
                        fr(0x605, "6000000000000000"), fr(0x585, f"{(2 << 1) | 1:02X}" + name[14:].hex() + "0000")])
    e = sdo_log(svc)[0]
    assert e["result"] == "ok" and e["segmented"] and e["size"] == 19
    assert bytes.fromhex(e["raw"]) == name and e["name"] == "Manufacturer device name"
    # the same again with the second segment's toggle bit wrong
    svc.decode.process([fr(0x605, "4008100000000000"), fr(0x585, "41081000" + struct.pack("<I", 19).hex()),
                        fr(0x605, "6000000000000000"), fr(0x585, "00" + name[:7].hex()),
                        fr(0x605, "7000000000000000"), fr(0x585, "00" + name[7:14].hex())])
    assert sdo_log(svc)[0]["result"] == "toggle bit error"


def test_segmented_download(svc):
    svc.decode.assign_eds(5, EDS)
    data = b"0123456789"                                          # 10 bytes: 7 + 3
    svc.decode.process([fr(0x605, "21081000" + struct.pack("<I", 10).hex()), fr(0x585, "6008100000000000"),
                        fr(0x605, "00" + data[:7].hex()), fr(0x585, "2000000000000000"),
                        fr(0x605, f"{0x10 | (4 << 1) | 1:02X}" + data[7:].hex() + "00000000"),
                        fr(0x585, "3000000000000000")])
    e = sdo_log(svc)[0]
    assert e["op"] == "write" and e["result"] == "ok" and e["size"] == 10 and e["value"] == "0123456789"


def test_abort_with_text_and_no_response(svc):
    svc.decode.assign_eds(5, EDS)
    svc.decode.process([fr(0x605, "4000300000000000"), fr(0x585, "8000300000000206")])
    e = sdo_log(svc)[0]
    assert e["result"] == "abort" and e["abort_code"] == "0x06020000" and e["abort_by"] == "server"
    assert "does not exist" in e["abort_text"] and e["value"] is None
    svc.decode.process([fr(0x605, "4000100000000000"), fr(0x605, "4001100000000000"), fr(0x585, "4F01100005000000")])
    log = sdo_log(svc)
    assert log[1]["index"] == 0x1000 and log[1]["result"] == "no response"
    assert log[0]["index"] == 0x1001 and log[0]["value"] == 5
    assert f"{0x3000}:0" not in svc.decode.canopen_node(5)["values"]       # an abort sets no value


def test_block_transfer_is_logged_not_decoded(svc):
    svc.decode.assign_eds(5, EDS)
    svc.decode.process([fr(0x605, "A408100000000000"), fr(0x585, "C408100013000000"),
                        fr(0x605, "A300000000000000"), fr(0x585, "0141424344454647"), fr(0x585, "8248494A4B4C4D4E")])
    log = sdo_log(svc)
    assert len(log) == 1 and log[0]["result"] == "block transfer, not decoded" and log[0]["op"] == "read"
    svc.decode.process([fr(0x605, "4018100100000000"), fr(0x585, "A1")])  # 0xA1: still inside the block
    _t[0] += 2                                                           # silence ends it
    svc.decode.process([fr(0x605, "4018100100000000"), fr(0x585, "43181001A00C0000")])
    assert sdo_log(svc)[0]["value"] == 0x0CA0


def test_sdo_ids_of_unknown_nodes_are_not_sdo(svc):
    svc.decode.process([fr(0x605, "4018100100000000"), fr(0x585, "43181001A00C0000")])
    assert sdo_log(svc) == [] and f"0:{0x585}" not in svc.decode.text_map()
    svc.decode.process([fr(0x605, "4018100100000000", tx=True), fr(0x585, "43181001A00C0000")])   # this tool asked
    e = sdo_log(svc)[0]
    assert e["source"] == "CANopener" and e["value"] == "0x00000CA0" and e["name"] == "Identity object.Vendor-ID"


def test_var_object_name_only_on_sub_0(svc):
    # 0x2202 is a VAR in the EDS (like a battery's 0x4900 seen live): a read of sub 0xD2 is not "Command"
    svc.decode.assign_eds(5, EDS)
    svc.decode.process([fr(0x605, "4002220000000000"), fr(0x585, "4F02220007000000"),
                        fr(0x605, "400222D200000000"), fr(0x585, "4F0222D207000000")])
    sub0, subd2 = sdo_log(svc)[::-1]
    assert (sub0["sub"], sub0["name"], sub0["value"], sub0["type"]) == (0, "Command", 7, "u8")
    assert (subd2["sub"], subd2["name"], subd2["value"], subd2["type"]) == (0xD2, "", "0x07", "")
    assert sub0["result"] == subd2["result"] == "ok"


def test_extra_sdo_server_logged_under_its_node(svc):
    # a second SDO server of node 7 on 0x627/0x5A7: without the pair it is nobody's ("node 39" has no EDS/heartbeat)
    svc.decode.assign_eds(7, EDS)
    svc.decode.process([fr(0x627, "4002220000000000"), fr(0x5A7, "4F02220007000000")])
    assert sdo_log(svc, 7) == [] and sdo_log(svc, 39) == []
    svc.decode.add_sdo_server(7, "627", "5A7")
    svc.decode.process([fr(0x627, "4002220000000000"), fr(0x607, "4018100100000000"),       # both servers at once
                        fr(0x5A7, "4F02220007000000"), fr(0x587, "43181001A00C0000")])
    srv, dflt = sdo_log(svc, 7)[::-1]
    assert (srv["server"], srv["name"], srv["value"], srv["ms"]) == ("0x627/0x5A7", "Command", 7, 20.0)
    assert srv["result"] == "ok"
    assert (dflt["server"], dflt["name"], dflt["value"], dflt["result"]) == ("", "Identity.Vendor id", 0x0CA0, "ok")
    assert sdo_log(svc, 39) == [] and "SDO read node 7 0x2202:00 done" in svc.decode.text_map()[f"0:{0x5A7}"]
    svc.decode.remove_sdo_server(7, "627")
    svc.decode.process([fr(0x627, "4002220000000000"), fr(0x5A7, "4F02220007000000")])
    assert len(sdo_log(svc, 7)) == 2


def test_extra_sdo_server_replies_alone(svc):
    """On a Kvaser channel shared with the program that asks, only the replies are seen."""
    svc.decode.add_sdo_server(7, "627", "5A7")                    # no EDS: CiA 301 names and the bytes
    svc.decode.process([fr(0x5A7, "4B17100064000000"), fr(0x5A7, "6017100000000000")])
    read, write = sdo_log(svc, 7)[::-1]
    assert (read["op"], read["name"], read["value"]) == ("read", "Producer heartbeat time", "0x0064")
    assert read["result"] == "ok"
    assert (write["op"], write["index"], write["value"], write["result"]) == ("write", 0x1017, None, "request not seen")
    assert svc.decode.canopen_node(7)["values"][f"{0x1017}:0"]["value"] == "0x0064"    # the ack carries no value


def test_extra_sdo_server_checks(svc):
    d = svc.decode
    d.add_sdo_server(7, "627", "5A7", channel=0)
    for args, msg in [((7, "607", "587"), "default SDO server"), ((8, "628", "5A7"), "overlaps"),
                      ((8, "800", "5A8"), "11-bit"), ((8, "628", "628"), "differ"), ((0, "628", "5A8"), "1..127")]:
        with pytest.raises(ValueError, match=msg):
            d.add_sdo_server(*args)
    d.add_sdo_server(8, "628", "5A7", channel=1)                  # the same id on another channel is another bus
    assert [s["node"] for s in d.canopen_info()["sdo_servers"]] == [7, 8]


def test_clear_sdo_log_only(svc):
    svc.decode.assign_eds(5, EDS)
    svc.decode.process([fr(0x605, "4018100100000000"), fr(0x585, "43181001A00C0000"),
                        fr(0x085, "1032010000000000")])
    svc.decode.clear_logs("sdo")
    assert sdo_log(svc) == [] and svc.decode.canopen_info()["emcy"]
    assert svc.decode.canopen_node(5)["values"]                          # the values stay


# ------------------------------------------------------------------------------------------- detail views (simulator)

def test_node_detail_pdo_objects_and_tool_reads(sim_svc):
    dec = sim_svc.decode
    dec.assign_eds(5, EDS)
    assert wait_for(lambda: dec.canopen_node(5)["pdos"][0]["count"] >= 3)
    d = dec.canopen_node(5)
    tpdo = next(p for p in d["pdos"] if p["name"] == "TPDO1")
    assert tpdo["cob_id"] == 0x185 and tpdo["kind"] == "TPDO" and tpdo["period_ms"] > 0 and tpdo["age"] < 1
    pos = next(f for f in tpdo["fields"] if f["name"] == "Position")
    assert (pos["index"], pos["sub"], pos["offset"], pos["bits"]) == (0x2200, 0, 0, 16)
    assert -500 <= pos["value"] <= 500 and pos["raw"].startswith("0x") and pos["min"] is None
    rpdo = next(p for p in d["pdos"] if p["name"] == "RPDO1")              # never sent: no frame, no values
    assert rpdo["count"] == 0 and rpdo["fields"][0]["value"] is None
    dec.sdo_read(0, 5, 0x1018, 1)
    assert wait_for(lambda: any(e["source"] == "CANopener" and e["index"] == 0x1018 for e in dec.canopen_node(5)["sdo"]))
    assert dec.canopen_node(5)["values"][f"{0x1018}:1"]["value"] == 0x0CA0


def test_node_without_eds_shows_predefined_pdo_ids(sim_svc):
    dec = sim_svc.decode
    assert wait_for(lambda: any(p["cob_id"] == 0x185 for p in dec.canopen_node(5)["pdos"]))
    d = dec.canopen_node(5)
    assert d["eds"] == "" and all(p["count"] > 0 and p["fields"] == [] for p in d["pdos"])
    assert next(p for p in d["pdos"] if p["cob_id"] == 0x185)["name"] == "TPDO1"


def test_dbc_live_and_message_detail(sim_svc):
    dec = sim_svc.decode
    dec.load_dbc(DBC)
    sim_svc.engine.channel(0).send(can.Message(arbitration_id=0x100, is_extended_id=False,
                                               data=bytes.fromhex("A00F0F7800000000")))
    assert wait_for(lambda: "Engine" in dec.dbc_live()["messages"])
    live = dec.dbc_live()["messages"]["Engine"]
    assert live["data"] == "A0 0F 0F 78 00 00 00 00" and live["tx"]
    m = dec.dbc_message("Engine")
    sig = {s["name"]: s for s in m["signals"]}
    assert sig["EngineSpeed"]["value"] == 1000.0 and sig["EngineSpeed"]["raw"] == 4000
    assert sig["Gear"]["text"] == "Reverse"


def test_j1939_sources_and_source_detail(sim_svc):
    dec = sim_svc.decode
    dec.set_j1939(0, True)
    assert wait_for(lambda: any(s["sa"] == 0 for s in dec.j1939_info()["sources"]))
    assert wait_for(lambda: {p["pgn"] for p in dec.j1939_source(0)["pgns"]} >= {65265, 61444})
    ccvs = next(p for p in dec.j1939_source(0, 0)["pgns"] if p["pgn"] == 65265)
    assert ccvs["name"] == "CCVS" and ccvs["da"] == 0xFF and ccvs["count"] >= 1
    assert dec.j1939_source(0x55)["pgns"] == []


def test_detail_endpoints_and_mcp(channel):
    from fastapi.testclient import TestClient
    from canopener.server import create_app
    from test_decode_api import _mcp
    svc = Service(load_plugins=False)
    with TestClient(create_app(svc), base_url="http://127.0.0.1:8775") as c:
        c.post("/api/sim/start", json={"channel": channel})
        assert c.post("/api/connect", json={"interface": "virtual", "channel": channel, "bitrate": 250000}).status_code == 200
        c.post("/api/decode/canopen/eds", json={"node": 5, "path": EDS})
        assert wait_for(lambda: c.get("/api/decode/canopen/node", params={"node": 5}).json()["pdos"][0]["count"] > 0)
        assert c.get("/api/decode/canopen/node", params={"node": 5, "channel": "0"}).json()["channel"] == 0
        c.post("/api/decode/canopen/sdo_read", json={"node": 5, "index": "0x1018", "sub": 1})
        assert wait_for(lambda: c.get("/api/decode/canopen/node", params={"node": 5}).json()["sdo"])
        assert c.post("/api/decode/clear", json={"what": "sdo"}).status_code == 200
        assert c.get("/api/decode/canopen/node", params={"node": 5}).json()["sdo"] == []
        assert c.post("/api/decode/clear").status_code == 200                  # no body: as before
        c.post("/api/decode/dbc/load", json={"path": DBC})
        assert "messages" in c.get("/api/decode/dbc/live").json()
        assert c.get("/api/decode/dbc/message", params={"name": "Engine"}).json()["name"] == "Engine"
        assert c.get("/api/decode/dbc/message", params={"name": "Nope"}).status_code == 400
        c.post("/api/decode/j1939", json={"channel": 0, "enabled": True})
        assert wait_for(lambda: c.get("/api/decode/j1939/source", params={"sa": 0}).json()["pgns"])
        r, _ = _mcp(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                      "clientInfo": {"name": "test", "version": "1"}})
        session = r.headers.get("mcp-session-id")
        _mcp(c, "notifications/initialized", session=session)
        _, tools = _mcp(c, "tools/list", session=session, id_=2)
        names = {t["name"] for t in tools["result"]["tools"]}
        assert {"get_pdo_values", "get_sdo_log", "get_dbc_message_values", "get_j1939_source"} <= names
        _, res = _mcp(c, "tools/call", {"name": "get_pdo_values", "arguments": {"node": 5}}, session=session, id_=3)
        assert "TPDO1" in res["result"]["content"][0]["text"]


# ------------------------------------------------------------------------------------------- review fixes
def test_block_download_last_segment_33_does_not_end_it(svc):
    """Review: a last data segment with sequence number 33 is 0xA1 too; only the server's 0xA1 ends a download."""
    svc.decode.assign_eds(5, EDS)
    svc.decode.process([fr(0x605, "C608100000010000"), fr(0x585, "A408100021000000")]       # init, 33/block
                       + [fr(0x605, f"{i:02X}00000000000000") for i in range(1, 33)]
                       + [fr(0x605, "A100000000000000"),                                     # last segment, seq 33
                          fr(0x585, "A221210000000000"),                                     # block ack
                          fr(0x605, "C534120000000000"),                                     # end request + CRC
                          fr(0x585, "A100000000000000")])                                    # end response
    log = sdo_log(svc)
    assert len(log) == 1 and log[0]["result"] == "block transfer, not decoded" and log[0]["op"] == "write"


def test_the_request_after_a_silent_block_end_is_not_lost(svc):
    """Review: the first frame after a block transfer ended in silence was dropped, so its reply was credited to
    "bus" with 0 ms even when CANopener sent the request."""
    svc.decode.assign_eds(5, EDS)
    svc.decode.process([fr(0x605, "A408100000000000"), fr(0x585, "C408100013000000")])
    _t[0] += 2
    svc.decode.process([fr(0x605, "4018100100000000", tx=True), fr(0x585, "43181001A00C0000")])
    e = sdo_log(svc)[0]
    assert e["source"] == "CANopener" and e["ms"] > 0 and e["value"] == 0x0CA0


def test_a_channel_bound_dbc_message_ignores_newer_copies_on_other_channels(svc, monkeypatch):
    """Review: with the same frames on two adapters, the newest copy on the other channel hid the message."""
    dec = svc.decode
    dec.load_dbc(DBC, channel=0)
    now = time.time()
    rows = [(0, 0x100, bytes.fromhex("A00F0F7800000000"), now - 0.01, 0.01, 5),
            (1, 0x100, bytes.fromhex("A00F0F7800000000"), now, 0.01, 5)]
    monkeypatch.setattr(dec, "_rows", lambda channels, keep: rows)
    live = dec.dbc_live()["messages"]["Engine"]
    assert live["channel"] == 0
