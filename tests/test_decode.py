"""Decoders: DBC (decode, encode, multiplexing, J1939 PGN match), J1939 (id split, TP/BAM, address claim, DM1),
CANopen (EMCY, heartbeat, PDO decode against the simulator, PDO mapping read over SDO, plans) and throughput."""

import json
import shutil
import struct
import time
from pathlib import Path

import canopen
import pytest

from canopener.core.channel import F_EXT, F_TX
from canopener.decode.canopen_od import decode_emcy
from canopener.decode.dbc import DbcError, DbcSet
from canopener.decode.j1939 import J1939Decoder, decode_dm1, decode_name, split_id
from canopener.decode.signals import SignalStore
from canopener.service import Service

DATA = Path(__file__).resolve().parent / "data"
DBC = str(DATA / "test.dbc")
EDS = str(DATA / "test_node.eds")
XDD = str(DATA / "test_node.xdd")


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


def frame(cid, data, ext=None, ts=None):
    ext = cid > 0x7FF if ext is None else ext
    return (ts or time.time(), cid, F_EXT if ext else 0, len(data), bytes(data))


# ------------------------------------------------------------------------------------------- DBC

def test_dbc_decode_choices_and_publish(svc):
    svc.decode.load_dbc(DBC)
    svc.decode.process([(0, frame(0x100, bytes.fromhex("A00F0F7800000000")))])
    sig = svc.signals.get("dbc/Engine/EngineSpeed")
    assert sig["value"] == 1000.0 and sig["unit"] == "rpm" and sig["max"] == 16383.75
    gear = svc.signals.get("dbc/Engine/Gear")
    assert gear["value"] == 15 and gear["choices"]["15"] == "Reverse"
    assert svc.signals.get("dbc/Engine/CoolantTemp")["value"] == 80
    text = svc.decode.text_map()[f"0:{0x100}"]
    assert "Gear=Reverse" in text and "EngineSpeed=1000.0 rpm" in text


def test_dbc_multiplexed_decode_and_encode():
    d = DbcSet()
    d.load(DBC)
    assert d.encode("MuxMsg", {"Selector": 1, "ValueB": -12.5}) == (0x200, False, bytes.fromhex("01E7FF0000000000"))
    m = d.find(0, 0x200, False)
    assert m.decode(bytes.fromhex("01E7FF0000000000")) == {"Selector": 1, "ValueB": -12.5}
    assert m.decode(bytes.fromhex("0034120000000000")) == {"Selector": 0, "ValueA": 0x1234}
    assert d.encode("Engine", {"EngineSpeed": 1000, "Gear": "Reverse", "CoolantTemp": 80})[2] == \
        bytes.fromhex("A00F0F7800000000")
    with pytest.raises(DbcError):
        d.encode("Engine", {"EngineSpeed": 99999})          # out of range
    with pytest.raises(DbcError):
        d.encode("Engine", {"Nope": 1})


def test_dbc_per_channel_and_j1939_pgn_match():
    d = DbcSet()
    d.load(DBC, channel=1)
    assert d.find(0, 0x100, False) is None and d.find(1, 0x100, False).name == "Engine"
    # EEC1 is 0x0CF00400 in the DBC; another source address and priority still match by PGN 61444
    assert d.find(1, 0x18F00417, True).name == "EEC1"
    assert d.find(1, 0x18F00517, True) is None


def test_dbc_transmit_entry_encoded_from_signals(svc):
    svc.decode.load_dbc(DBC)
    e = svc.tx_add({"dbc_message": "Engine", "signals": {"EngineSpeed": 1000, "Gear": "First"}, "period_ms": 0})
    assert e["can_id"] == 0x100 and e["data"] == "A0 0F 01 00 00 00 00 00"     # CoolantTemp not given: raw 0
    e = svc.tx_update(e["id"], {"signals": {"EngineSpeed": 500, "Gear": 2, "CoolantTemp": 0}})
    assert e["data"] == "D0 07 02 28 00 00 00 00"
    e = svc.tx_update(e["id"], {"dbc_message": "EEC1", "signals": {"EngineSpeed": 800}})
    assert e["can_id"] == 0x0CF00400 and e["ext"] and e["data"].split()[3:5] == ["00", "19"]


# ------------------------------------------------------------------------------------------- J1939

def test_j1939_id_split():
    assert split_id(0x0CF00400) == (3, 61444, 0xFF, 0x00)           # PDU2: EEC1 broadcast
    assert split_id(0x18EAFF00) == (6, 59904, 0xFF, 0x00)           # PDU1 request to global
    assert split_id(0x18EF2A11) == (6, 61184, 0x2A, 0x11)           # PDU1 proprietary A: DA in PS
    assert split_id(0x19FEF100)[1] == 0x1FEF1                       # data page bit


def _name(identity, mfr, fn, ig=1, arbitrary=True):
    n = identity | (mfr << 21) | (2 << 32) | (3 << 35) | (fn << 40) | (5 << 49) | (1 << 56) | (ig << 60) | \
        (int(arbitrary) << 63)
    return n.to_bytes(8, "little")


def test_address_claim_name_decode():
    info = decode_name(_name(123456, 0x2AB, 0x81))
    assert (info["identity"], info["manufacturer"], info["function"]) == (123456, 0x2AB, 0x81)
    assert (info["ecu_instance"], info["function_instance"], info["vehicle_system"]) == (2, 3, 5)
    assert info["industry_group"] == 1 and info["industry"] == "on-highway" and info["arbitrary_address"]
    j = J1939Decoder(SignalStore(), lambda *a: None)
    text = j.handle(0, 1.0, 0x18EEFF80, _name(7, 0x123, 0x00))
    assert "PGN 60928 AC SA 0x80" in text
    assert j.ecu_list()[0]["sa"] == 0x80 and j.ecu_list()[0]["manufacturer"] == 0x123


def _tp(j, cm_id, dt_id, ctrl, pgn, payload):
    packets = (len(payload) + 6) // 7
    j.handle(0, 1.0, cm_id, bytes([ctrl, len(payload) & 0xFF, len(payload) >> 8, packets, 0xFF,
                                   pgn & 0xFF, (pgn >> 8) & 0xFF, pgn >> 16]))
    padded = payload + b"\xff" * (packets * 7 - len(payload))
    texts = [j.handle(0, 1.1, dt_id, bytes([i + 1]) + padded[i * 7:i * 7 + 7]) for i in range(packets)]
    return texts


def test_tp_rts_cts_reassembly_decoded_by_dbc(svc):
    svc.decode.load_dbc(DBC)
    svc.decode.set_j1939(0, True)
    payload = struct.pack("<H", 0x1234) + bytes(10) + struct.pack("<I", 0xDEADBEEF)   # PropLong, 16 bytes
    frames = [frame(0x1CECF901, bytes([16, 16, 0, 3, 3, 0x00, 0xFF, 0x00]))]           # RTS SA 01 -> DA F9
    frames.append(frame(0x1CEC01F9, bytes([17, 3, 1, 0xFF, 0xFF, 0x00, 0xFF, 0x00])))             # CTS F9 -> 01
    for i in range(3):
        frames.append(frame(0x1CEBF901, bytes([i + 1]) + (payload + b"\xff" * 5)[i * 7:i * 7 + 7]))
    svc.decode.process([(0, f) for f in frames])
    msgs = svc.decode.j1939_info()["messages"]
    assert len(msgs) == 1 and msgs[0]["kind"] == "RTS/CTS" and msgs[0]["pgn"] == 65280 and msgs[0]["size"] == 16
    assert msgs[0]["sa"] == 1 and msgs[0]["da"] == 0xF9 and "Value2=3735928559" in msgs[0]["decoded"]
    assert svc.signals.get("j1939/1/65280.Value2")["value"] == 0xDEADBEEF


def test_bam_dm1_multi_dtc():
    j = J1939Decoder(SignalStore(), lambda *a: None)
    dtc = lambda spn, fmi, oc: bytes([spn & 0xFF, (spn >> 8) & 0xFF, ((spn >> 16) << 5) | fmi, oc])  # noqa: E731
    payload = bytes([0x44, 0xFF]) + dtc(100, 1, 5) + dtc(520192, 31, 2) + dtc(190, 0, 1)
    texts = _tp(j, 0x18ECFF00, 0x18EBFF00, 32, 65226, payload)
    assert "complete" in texts[-1]
    m = j.messages[-1]
    assert m["kind"] == "BAM" and m["pgn"] == 65226 and m["size"] == 14
    dm1 = j.dm1_list()[0]
    assert dm1["lamps"] == {"MIL": 1, "RSL": 0, "AWL": 1, "PL": 0}
    assert [(d["spn"], d["fmi"], d["oc"]) for d in dm1["dtcs"]] == [(100, 1, 5), (520192, 31, 2), (190, 0, 1)]


def test_dm1_single_frame_and_tp_abort():
    assert decode_dm1(bytes([0, 0xFF, 0, 0, 0, 0, 0xFF, 0xFF]))["dtcs"] == []
    store = SignalStore()
    j = J1939Decoder(store, lambda *a: None)
    text = j.handle(0, 1.0, 0x18FECA00, bytes([0x40, 0xFF, 100, 0, 0x01, 0x05, 0xFF, 0xFF]))
    assert "SPN 100 FMI 1 OC 5" in text and store.get("j1939/0/DM1.ActiveDTCs")["value"] == 1
    j.handle(0, 1.0, 0x18ECF900, bytes([16, 20, 0, 3, 3, 0xCA, 0xFE, 0]))
    assert "Abort" in j.handle(0, 1.0, 0x18EC00F9, bytes([255, 3, 0xFF, 0xFF, 0xFF, 0xCA, 0xFE, 0]))
    assert not j.sessions and j.messages[-1]["decoded"] == "aborted: timeout"


# ------------------------------------------------------------------------------------------- CANopen

def test_emcy_decode():
    e = decode_emcy(bytes.fromhex("3081110102030405"))
    assert e["code"] == 0x8130 and "heartbeat" in e["text"] and e["register_bits"] == ["generic", "communication"]
    assert e["vendor"] == "01 02 03 04 05"
    assert decode_emcy(bytes.fromhex("1042040000000000"))["text"] == "device temperature"
    assert decode_emcy(bytes.fromhex("0000000000000000"))["text"] == "error reset / no error"


def test_emcy_only_for_known_nodes_and_heartbeat_state(svc):
    dec = svc.decode
    dec.process([(0, frame(0x0CF, bytes(8)))])                        # unknown node: not an EMCY
    assert dec.canopen_info()["emcy"] == []
    dec.process([(0, frame(0x74F, b"\x7f")), (0, frame(0x0CF, bytes.fromhex("1032010000000000")))])
    info = dec.canopen_info()
    assert info["nmt"][0]["node"] == 0x4F and info["nmt"][0]["state"] == "pre-operational"
    assert info["emcy"][0]["node"] == 0x4F and info["emcy"][0]["code"] == 0x3210
    assert svc.signals.get("canopen/79/NMT")["value"] == 127
    assert "EMCY node 79: 0x3210" in dec.text_map()[f"0:{0x0CF}"]


def test_pdo_decode_from_eds_against_sim(sim_svc):
    sim_svc.decode.assign_eds(5, EDS)
    assert wait_for(lambda: (sim_svc.signals.get("canopen/5/TPDO1.Counter") or {}).get("count", 0) >= 2)
    pos = sim_svc.signals.get("canopen/5/TPDO1.Position")
    assert -500 <= pos["value"] <= 500 and pos["can_id"] == 0x185
    assert "TPDO1 node 5 Position=" in sim_svc.decode.text_map()[f"0:{0x185}"]
    assert wait_for(lambda: any(n["node"] == 5 for n in sim_svc.decode.canopen_info()["nmt"]))
    assert sim_svc.signals.get("canopen/10/TPDO1.Position") is None   # node 10 has no EDS


def test_xdd_import_decodes_the_same(svc):
    svc.decode.assign_eds(7, XDD)
    svc.decode.process([(0, frame(0x187, struct.pack("<hH", -42, 9)))])
    assert svc.signals.get("canopen/7/TPDO1.Position")["value"] == -42
    assert svc.signals.get("canopen/7/TPDO1.Position")["min"] == -500


def test_read_pdo_mapping_and_sdo_read(sim_svc, channel):
    # a device whose real mapping differs from the EDS default: Counter first, then Position
    net = canopen.Network()
    net.connect(interface="virtual", channel=channel, receive_own_messages=False)
    try:
        local = net.create_node(40, EDS)
        local.data_store[0x1A00] = {1: struct.pack("<I", 0x22010010), 2: struct.pack("<I", 0x22000010)}
        dec = sim_svc.decode
        dec.assign_eds(40, EDS, channel=0)
        info = dec.read_pdo_mapping(0, 40)
        tpdo = next(p for p in info["pdos"] if p["name"] == "TPDO1")
        assert info["source"] == "device" and [e["name"] for e in tpdo["entries"]] == ["Counter", "Position"]
        dec.process([(0, frame(0x1A8, struct.pack("<Hh", 3, -7)))])
        assert sim_svc.signals.get("canopen/40/TPDO1.Position")["value"] == -7
        r = dec.sdo_read(0, 5, 0x1008, 0)                              # sim node 5 (no EDS: raw + latin text)
        assert r["raw"] and bytes.fromhex(r["raw"]).decode() == "Demo device A (SDO)"
        r = dec.sdo_read(0, 40, 0x1000, 0)
        assert r["value"] == 0x191 and r["name"] == "Device type"
    finally:
        net.disconnect()


def test_sdo_write_and_nmt_go_through_plans(sim_svc):
    dec = sim_svc.decode
    dec.assign_eds(5, EDS)
    with pytest.raises(ValueError):
        dec.plan_sdo_write(0, 5, 0x1000, 0, 1)                         # read-only in the EDS
    with pytest.raises(ValueError):
        dec.plan_sdo_write(0, 5, 0x2110, 1, 7)                         # not in the EDS and no type given
    plan = dec.plan_sdo_write(0, 5, 0x2110, 1, 7, "u8")
    assert plan["token"] and plan["preview"][0]["frames"][0]["id"] == 0x605
    assert plan["preview"][0]["frames"][0]["data"].startswith("2F 10 21 01 07")
    sim_svc.nodes.run_now(plan["token"])
    node5 = next(n for n in sim_svc.sim.nodes if n.node_id == 5)
    assert (0x2110, 1, b"\x07") in node5.writes
    nmt = dec.plan_nmt(0, 5, "start")
    assert nmt["preview"][0]["frames"] == [{"id": 0, "data": "01 05", "note": "NMT"}]
    with pytest.raises(ValueError):
        dec.plan_nmt(0, 5, "explode")


def test_config_persists_in_settings_and_projects(svc):
    svc.decode.load_dbc(DBC)
    svc.decode.assign_eds(5, EDS, channel=0)
    svc.decode.set_j1939(1, True)
    svc.decode.add_sdo_server(7, "627", "0x5A7")
    cfg = svc.settings["decode"]
    assert cfg == {"dbc": [{"path": DBC, "channel": None}], "eds": [{"node": 5, "path": EDS, "channel": 0}],
                   "sdo_servers": [{"node": 7, "request": 0x627, "response": 0x5A7, "channel": None}], "j1939": [1]}
    svc.save_project("deco")
    svc.decode.apply_config({})
    assert svc.decode.config() == {"dbc": [], "eds": [], "sdo_servers": [], "j1939": []}
    svc.load_project("deco")
    assert svc.decode.config() == cfg
    s2 = Service(load_plugins=False)                                   # a restart restores it from settings
    try:
        s2.decode.apply_config(svc.settings["decode"], save=False)
        assert s2.decode.config() == cfg
    finally:
        s2.shutdown()
    r = svc.decode.apply_config({"dbc": [{"path": "C:/nope/missing.dbc"}]})
    assert r["errors"] and r["dbc"] == []


def test_files_in_the_app_folder_are_saved_relative_and_survive_moving_it(tmp_path, monkeypatch):
    """settings.json and projects keep DBC/EDS files from the app folder relative to it: copying the folder (or
    its settings into the next release folder) must not leave the decoders pointing at the old place."""
    old = tmp_path / "2026.10.12.1352"
    monkeypatch.setenv("CANOPENER_HOME", str(old))
    (old / "decode").mkdir(parents=True)
    for f in (DBC, EDS):
        shutil.copy(f, old / "decode")
    s = Service(load_plugins=False)
    try:
        s.decode.load_dbc(str(old / "decode" / "test.dbc"))
        s.decode.assign_eds(5, str(old / "decode" / "test_node.eds"))
        s.decode.assign_eds(6, EDS)                                    # outside the app folder: stays absolute
        s.save_project("p")
        assert s.decode.config()["eds"][0]["path"] == str(old / "decode" / "test_node.eds")   # the UI sees full paths
    finally:
        s.shutdown()
    want = {"dbc": [{"path": "decode/test.dbc", "channel": None}],
            "eds": [{"node": 5, "path": "decode/test_node.eds", "channel": None},
                    {"node": 6, "path": EDS, "channel": None}], "sdo_servers": [], "j1939": []}
    assert json.loads((old / "settings.json").read_text(encoding="utf-8"))["decode"] == want
    assert json.loads((old / "projects" / "p.json").read_text(encoding="utf-8"))["decode"] == want

    new = tmp_path / "moved"
    shutil.move(str(old), str(new))
    monkeypatch.setenv("CANOPENER_HOME", str(new))
    s = Service(load_plugins=False)
    try:
        assert s.decode.errors == []
        assert s.decode.config()["dbc"][0]["path"] == str(new / "decode" / "test.dbc")
        s.load_project("p")
        assert s.decode.errors == [] and s.decode.config()["eds"][0]["path"] == str(new / "decode" / "test_node.eds")
    finally:
        s.shutdown()


# ------------------------------------------------------------------------------------------- throughput

def _mixed_frames(n):
    """A busy mixed bus: DBC messages, CANopen PDOs + heartbeats, J1939 broadcast and many unknown ids."""
    now = time.time()
    pattern = [
        (0, frame(0x100, bytes.fromhex("A00F0F7800000000"), ts=now)),
        (0, frame(0x200, bytes.fromhex("01E7FF0000000000"), ts=now)),
        (0, frame(0x185, struct.pack("<hHI", 100, 5, 0)[:4], ts=now)),
        (0, frame(0x705, b"\x05", ts=now)),
        (1, frame(0x0CF00400, bytes([0xF1, 0x7D, 0x7D, 0x40, 0x1F, 0, 0xF0, 0x7D]), ts=now)),
        (1, frame(0x18FEF100, bytes(8), ts=now)),
        (0, frame(0x3A1, bytes(8), ts=now)),
        (0, frame(0x123, bytes(8), ts=now)),
        (1, frame(0x18FF1234, bytes(8), ts=now)),
        (0, (now, 0x100, F_TX, 8, bytes(8))),
    ]
    return (pattern * (n // len(pattern) + 1))[:n]


def test_decoder_throughput(svc):
    dec = svc.decode
    dec.load_dbc(DBC)
    dec.assign_eds(5, EDS)
    dec.set_j1939(1, True)
    frames = _mixed_frames(50_000)
    t0 = time.perf_counter()
    for i in range(0, len(frames), 2000):
        dec.process(frames[i:i + 2000])
    rate = len(frames) / (time.perf_counter() - t0)
    print(f"decoder: {rate:,.0f} frames/s")
    assert rate >= 8000, f"decoder only manages {rate:.0f} frames/s"
    # through the tap and the decoder thread, as the receive threads feed it
    start = dec.processed
    for item in frames[:20_000]:
        dec.tap.push(item)
    assert wait_for(lambda: dec.processed - start >= 20_000, 2.5) and dec.tap.dropped == 0


# ------------------------------------------------------------------------------------------- review fixes

def test_tp_dt_needs_every_sequence_number_and_times_out():
    store = SignalStore()
    j = J1939Decoder(store, on_message=lambda *a: None)
    t = 100.0
    rts = bytes([16, 20, 0, 3, 0xFF, 0x00, 0xEF, 0x00])           # 20 bytes in 3 packets, PGN 0xEF00
    j.handle(0, t, 0x1CEC2100, rts)
    j.handle(0, t, 0x1CEB2100, bytes([1]) + bytes(7))
    assert "ignored" in j.handle(0, t, 0x1CEB2100, bytes([9]) + bytes(7))   # outside 1..3
    assert "complete" not in j.handle(0, t, 0x1CEB2100, bytes([2]) + bytes(7))
    assert "complete" in j.handle(0, t, 0x1CEB2100, bytes([3]) + bytes(7))
    j.handle(0, t, 0x1CEC2100, rts)
    j.handle(0, t, 0x1CEB2100, bytes([1]) + bytes(7))
    assert "timed out" in j.handle(0, t + 2.0, 0x1CEB2100, bytes([2]) + bytes(7))


def test_pgn_match_only_on_j1939_channels_and_nan_text(svc):
    svc.decode.load_dbc(DBC)
    svc.decode.process([(0, frame(0x00F00499, bytes(8), ext=True))])        # proprietary id, same PGN as EEC1
    assert not [s for s in svc.signals.list() if s["key"].startswith("j1939/")]
    from canopener.decode.dbc import DbcMessage
    m = svc.decode.dbc.message("Engine")
    assert "n/a" in m.text({"EngineSpeed": float("nan"), "Gear": float("nan"), "CoolantTemp": 1.0})


def test_encode_with_missing_signals_uses_values_that_encode(svc):
    svc.decode.load_dbc(DBC)
    cid, ext, data = svc.decode.dbc.encode("Engine", {"EngineSpeed": 1000})
    assert cid == 0x100 and data[3] == 0                          # CoolantTemp: physical -40 = raw 0
    from types import SimpleNamespace
    from canopener.decode.dbc import _default_value
    assert _default_value(SimpleNamespace(choices=None, scale=1, offset=0, minimum=10, maximum=25)) == 10
    assert _default_value(SimpleNamespace(choices={3: "a", 5: "b"}, scale=1, offset=0, minimum=None, maximum=None)) == 3


def test_decoder_sdo_read_waits_for_node_operations(sim_svc):
    nodes = sim_svc.nodes
    with nodes.bus:                                               # as if a node job were running
        import threading
        err = []
        t = threading.Thread(target=lambda: err.append(_try_read(sim_svc)))
        t.start(); t.join(5)
    assert err and "using the bus" in err[0]


def test_sdo_read_refuses_node_ids_outside_1_to_127(sim_svc):
    """canopen sends to 0x600 + node for any number: node -1019 would write node 5's RPDO1 (0x205), -0x580 SYNC."""
    from canopener.decode.manager import DecodeError
    ch = sim_svc.engine.channels[0]
    sent = ch.tx_frames
    for node in (-1019, -0x580, 0, 128, 200):
        with pytest.raises(DecodeError, match="out of range"):
            sim_svc.decode.sdo_read(0, node, 0x1000, 0)
    with pytest.raises(DecodeError, match="out of range"):
        sim_svc.decode.sdo_read(0, 5, 0x10000, 0)
    time.sleep(0.1)
    assert ch.tx_frames == sent                                   # nothing went on the bus
    assert sim_svc.decode.sdo_read(0, 5, 0x1000, 0)["raw"]        # 1..127 still reads


def _try_read(svc):
    from canopener.decode.manager import DecodeError
    try:
        svc.decode.sdo_read(0, 5, 0x1000, 0)
        return "read"
    except DecodeError as exc:
        return str(exc)
