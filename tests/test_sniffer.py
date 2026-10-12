"""The sniffer plugin: bit-change tracking, notch, byte guesses (counter, checksum, constant), candidate signals,
and the REST/MCP surface against the simulator. It only reads frames."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from canopener.core.channel import F_EXT
from canopener.ext import load_plugin

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = load_plugin(ROOT / "plugins" / "sniffer")
import canopener_plugin_sniffer.analysis as A  # noqa: E402  (the plugin's own module, loaded above)


def frame(data: bytes, can_id: int = 0x123, ch: int = 0, ext: bool = False, ts: float = 0.0):
    return ch, (ts, can_id, F_EXT if ext else 0, len(data), data)


def machine(pedal_from: int = 1000, frames: int = 60, pedal=lambda i: 0):
    """Frames of a made-up ECU: byte 0 a counter (step 3), bytes 1-2 a 16-bit pedal (little endian), byte 3 constant,
    byte 4 a 4-bit counter in the low nibble with a fixed high nibble, byte 7 the XOR of bytes 0-6."""
    out = []
    for i in range(frames):
        p = pedal(i) if i >= pedal_from else 0
        d = bytearray([(i * 3) & 0xFF, p & 0xFF, p >> 8, 0x5A, 0xA0 | (i & 0xF), 0, 0, 0])
        x = 0
        for b in d[:7]:
            x ^= b
        d[7] = x
        out.append(frame(bytes(d), ts=i * 0.01))
    return out


def test_guesses_counter_checksum_constant():
    s = A.Sniffer()
    s.feed(machine())
    st = s.ids[(0, 0x123, False)]
    tags = st.update_tags(time.time(), force=True)
    assert tags[0] == "cnt" and tags[3] == "const" and tags[4] == "cnt4" and tags[7] == "chk"
    assert tags[1] == tags[2] == "const"                           # the pedal was never pressed
    assert st.period == pytest.approx(0.01, rel=0.05)


def test_notch_isolates_the_action():
    s = A.Sniffer()
    s.feed(machine(frames=30))
    assert s.notch() > 0                                          # counters and the checksum get hidden
    # a pedal pressed slowly: a new value every third frame (a ramp that changes every frame looks like a counter)
    pressed = machine(pedal_from=30, frames=90, pedal=lambda i: (i - 30) // 3 * 120)[30:]
    s.feed(pressed)
    st = s.ids[(0, 0x123, False)]
    st.update_tags(time.time(), force=True)
    new = set(A.bits_of(st.new_bits()))
    assert new and all(8 <= b < 24 for b in new)                  # only the pedal bytes are new
    cands = st.candidates()
    pedal = next(c for c in cands if c["start_bit"] == 8 and c["length"] == 16 and c["byte_order"] == "little")
    assert pedal["value"] == 59 // 3 * 120 and pedal["max"] == 59 // 3 * 120
    rows = s.snapshot(only_new=True)
    assert [r["id"] for r in rows] == [0x123] and rows[0]["new"] != "0"
    s.clear_notch()
    assert s.snapshot(only_new=True)[0]["new"] == "0"               # no notch: "only new" is ignored


def test_heat_extract_and_snapshot_filters():
    assert A.heat(None) == 0 and A.heat(0.05) == 9 and A.heat(4) == 2 and A.heat(60) == 0
    assert A.extract(bytes([0x34, 0x12]), 0, 16, "little") == 0x1234
    assert A.extract(bytes([0x12, 0x34]), 7, 16, "big") == 0x1234      # DBC big endian: start = MSB (bit 7)
    assert A.extract(bytes([0x12]), 0, 16, "little") is None
    s = A.Sniffer()
    s.feed([frame(b"\x01", 0x100, ts=0), frame(b"\x02", 0x100, ts=0.1), frame(b"\x07", 0x18FEF100, ext=True, ts=0.1),
            frame(b"\x07", 0x18FEF100, ext=True, ts=0.2), frame(b"\x01", 0x200, ch=1, ts=0.2)])
    assert [r["id"] for r in s.snapshot()] == [0x100, 0x18FEF100, 0x200]
    assert [r["id"] for r in s.snapshot(channel=1)] == [0x200]
    assert [r["id"] for r in s.snapshot(hide_static=True)] == [0x100]
    assert [r["id"] for r in s.snapshot(ids=[(0x18FEF100, 0x18FEF100)])] == [0x18FEF100]
    row = s.snapshot(ids=[(0x100, 0x100)], now=0.15)[0]
    assert row["heat"][:2] == "99" and row["heat"][2:] == "000000"    # bits 0 and 1 just changed
    assert s.detail(row["key"])["bit_changes"][:2] == [1, 1]


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


def test_api_against_the_simulator(client):
    c = client
    assert "sniffer" in [e["id"] for e in c.get("/api/about").json()["extensions"]]
    assert c.get("/ui/x/sniffer/sniffer.js").status_code == 200
    c.get("/api/x/sniffer/state")                                    # starts the analysis
    assert c.post("/api/x/sniffer/notch").status_code == 409           # too early: everything would look new
    time.sleep(3.5)
    st = c.get("/api/x/sniffer/state").json()
    ids = {r["id"] for r in st["ids"]}
    assert {0x0CF, 0x185, 0x705} <= ids and st["frames"] > 50
    row = next(r for r in st["ids"] if r["id"] == 0x0CF)              # the simulator's sine + 16-bit counter
    assert row["tags"][2] == "cnt"                                      # the counter's low byte
    d = c.get("/api/x/sniffer/detail", params={"key": row["key"]}).json()
    assert len(d["bytes"]) == 4 and [b["tag"] for b in d["bytes"]] == row["tags"]
    sine = next(x for x in d["candidates"] if x["start_bit"] == 0 and x["length"] == 16 and x["byte_order"] == "little")
    assert 200 <= sine["min"] < sine["max"] <= 1800
    assert c.get("/api/x/sniffer/detail", params={"key": "nonsense"}).status_code == 400
    assert c.post("/api/x/sniffer/notch").json()["hidden_bits"] > 0
    only = c.get("/api/x/sniffer/state", params={"only_new": True, "ids": "700-77F"}).json()
    assert all(0x700 <= r["id"] <= 0x77F for r in only["ids"])
    assert c.get("/api/x/sniffer/state", params={"ids": "zz"}).status_code == 400
    assert c.post("/api/x/sniffer/reset").json()["ok"]


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
    assert {"get_sniffer_changes", "set_sniffer_notch"} <= names


def test_ids_after_the_notch_are_new_and_nothing_is_new_without_one():
    s = A.Sniffer()
    s.feed([frame(b"\x01", 0x100, ts=0), frame(b"\x02", 0x100, ts=0.1)])
    assert s.snapshot()[0]["new"] == "0"                            # no notch: no "new" outline
    s.notch()
    s.feed([frame(b"\x05", 0x300, ts=0.2)])                         # appears only after the action
    rows = s.snapshot(only_new=True)
    assert [(r["id"], r["new_id"]) for r in rows] == [(0x300, True)]


def _feed(s, rows, start=0.0):
    s.feed([frame(bytes(d), ts=start + i * 0.01) for i, d in enumerate(rows)])


def test_motorola_signal_gets_a_big_endian_candidate():
    s = A.Sniffer()
    rows = []
    for i in range(300):                                           # a 10-bit big-endian value in bytes 1-2
        v = (i // 3) ** 2 * 37 % 1001 if i < 297 else 1000          # irregular, not a counter
        rows.append([0, (v >> 8) & 0x03, v & 0xFF, 0])
    _feed(s, rows)
    st = s.ids[(0, 0x123, False)]
    st.update_tags(time.time(), force=True)
    big = [c for c in st.candidates() if c["byte_order"] == "big"]
    exact = next(c for c in big if c["start_bit"] == 9 and c["length"] == 10)   # DBC 9|10@0
    assert exact["max"] == 1000 and exact["min"] == 0


def test_ranges_seen_outlast_the_counter_traffic():
    s = A.Sniffer()
    rows = []
    for i in range(1500):                                         # pedal pressed early, then 1300 counter frames
        p = min(i, 90) * 10 if i < 200 else 0
        rows.append([i & 0xFF, p & 0xFF, p >> 8, 0])
    for chunk in range(0, 1500, 100):                             # guesses are refreshed as the plugin does
        _feed(s, rows[chunk:chunk + 100], start=chunk * 0.01)
        s.ids[(0, 0x123, False)].update_tags(time.time(), force=True)
    st = s.ids[(0, 0x123, False)]
    pedal = next(c for c in st.candidates() if c["start_bit"] == 8 and c["length"] == 16 and c["byte_order"] == "little")
    assert pedal["max"] == 900 and pedal["value"] == 0


def test_counter_in_the_low_nibble_does_not_hide_the_high_nibble():
    s = A.Sniffer()
    _feed(s, [[0x10 | (i & 0xF)] for i in range(40)])             # gear 1 in the high nibble, counter below
    st = s.ids[(0, 0x123, False)]
    assert st.update_tags(time.time(), force=True) == ["cnt4"]
    s.notch()
    _feed(s, [[0x30 | (i & 0xF)] for i in range(40, 60)], start=1)   # gear 3
    st.update_tags(time.time(), force=True)
    assert st.update_tags(time.time()) == ["cnt4"] and set(A.bits_of(st.new_bits())) == {5}


def test_checksum_in_byte_0_and_copies_are_not_checksums():
    s = A.Sniffer()
    rows = []
    for i in range(60):
        d = [0, i & 0xFF, 0x55, (i // 7) * 3, (i // 7) * 3 + 2, 0, 0, 0]   # bytes 3 and 4: one value and its copy
        x = 0
        for b in d[1:]:
            x ^= b
        d[0] = x
        rows.append(d)
    _feed(s, rows)
    tags = s.ids[(0, 0x123, False)].update_tags(time.time(), force=True)
    assert tags[0] == "chk" and tags[1] == "cnt" and tags[3] == "" and tags[4] == ""


def test_long_fd_runs_are_split_into_64_bit_candidates():
    s = A.Sniffer()
    _feed(s, [bytes([(i * 37 + k * 11) & 0xFF for k in range(16)]) + bytes(48) for i in range(30)])
    cands = s.ids[(0, 0x123, False)].candidates()
    assert cands and all(c["length"] <= 64 for c in cands)


def _mcp_call(c, name, args):
    h = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
    r = c.post("/mcp/", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
    sid = r.headers.get("mcp-session-id")
    h2 = dict(h, **({"mcp-session-id": sid} if sid else {}))
    c.post("/mcp/", headers=h2, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    r = c.post("/mcp/", headers=h2, json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                          "params": {"name": name, "arguments": args}})
    text = r.text
    if "data:" in text:
        text = next(l[5:] for l in text.splitlines() if l.startswith("data:"))
    res = json.loads(text)["result"]
    return res.get("structuredContent") or json.loads(res["content"][0]["text"])


def test_mcp_cold_notch_and_changes(client):
    out = _mcp_call(client, "set_sniffer_notch", {})
    assert "error" in out and "wait" in out["error"]                 # a notch on a cold sniffer is refused
    time.sleep(3.5)
    assert _mcp_call(client, "set_sniffer_notch", {})["hidden_bits"] > 0
    ch = _mcp_call(client, "get_sniffer_changes", {"only_new_since_notch": False, "max_ids": 5})
    assert ch["notched"] and 0 < len(ch["ids"]) <= 5
    assert all({"id", "changed_bits", "byte_tags", "candidates"} <= set(r) for r in ch["ids"])
