"""Tests that back the user guide's claims about node work: scans, plans, runs, LSS, the simulator, the MCP read
tools, the command line and the EDS editor's device work. Each test names the doc line it backs
(docs/user-guide.md unless noted).

What CANopener sends is captured with an engine tap on its transmit side (every frame any channel sends) and,
where it matters, with a second bus on the virtual channel (everything on the wire)."""

import json
import re
import threading
import time
from pathlib import Path

import can
import pytest
from fakeslave import FakeSlave
from fastapi.testclient import TestClient
from test_server import _mcp

from canopener import cli
from canopener.core.adapters import ChannelConfig
from canopener.core.channel import F_TX, Tap
from canopener.nodes.service import NodeService
from canopener.server import create_app
from canopener.service import Service
from canopener.sim import SimBus

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8775"

# client -> server command bytes of an SDO upload: initiate upload, upload segment (toggle 0 / 1), abort (the
# canopen client aborts an upload that timed out; an abort changes nothing on a node)
SDO_UPLOAD_CS = {0x40, 0x60, 0x70, 0x80}


class Sniffer(can.Listener):
    def __init__(self):
        self.frames = []

    def on_message_received(self, msg):
        self.frames.append((msg.arbitration_id, bytes(msg.data)))


@pytest.fixture
def sniffer(channel):
    """Every frame on the virtual wire that a bus other than this one sent."""
    bus = can.Bus(interface="virtual", channel=channel)
    s = Sniffer()
    n = can.Notifier(bus, [s], 0.5)
    yield s
    n.stop()
    bus.shutdown()


def tx_tap(engine) -> Tap:
    """Every frame CANopener transmits on any channel."""
    return engine.add_tap(Tap(maxlen=None, include_tx=True, accept=lambda cid, fl: bool(fl & F_TX)))


def sent(tap: Tap) -> list:
    return [(fr[1], bytes(fr[4])) for _, fr in tap.drain()]


def not_sdo_uploads(frames) -> list:
    return [(f"0x{i:X}", d.hex(" ")) for i, d in frames if not (0x601 <= i <= 0x67F and d and d[0] in SDO_UPLOAD_CS)]


@pytest.fixture
def svc(engine, channel):
    engine.connect(ChannelConfig("virtual", channel))
    return NodeService(engine)


# ------------------------------------------------------------------------------------------- scan

def test_scan_sends_only_sdo_uploads(svc, engine, channel, sniffer):
    """user-guide.md:401 and README.md:27: the scan only reads (SDO uploads only; no download, NMT or LSS)."""
    a = FakeSlave(channel, node_id=5, heartbeat_ms=50)                 # found by heartbeat
    b = FakeSlave(channel, node_id=9, heartbeat_ms=0)                  # found only by the SDO ping
    tap = tx_tap(engine)
    try:
        result = svc.scan(0, passive_s=0.3, sdo_timeout=0.01)
        time.sleep(0.1)
        assert {5, 9} <= set(result.nodes)
        tx = sent(tap)
        assert tx, "an active scan sends SDO reads"
        assert not_sdo_uploads(tx) == []
        assert any(d[0] == 0x60 for _, d in tx)                        # the segmented name upload is covered
        wire = [f for f in sniffer.frames if f[0] not in (0x705, 0x585, 0x589)]   # not from the slaves
        assert not_sdo_uploads(wire) == []
        assert a.writes == [] and b.writes == []
    finally:
        a.close()
        b.close()


def test_scan_on_listen_only_channel_sends_nothing(engine, channel, sniffer):
    """user-guide.md:401: on a listen-only channel the scan only listens."""
    engine.connect(ChannelConfig("virtual", channel, listen_only=True))
    svc = NodeService(engine)
    slave = FakeSlave(channel, node_id=5, heartbeat_ms=50)
    tap = tx_tap(engine)
    try:
        job = svc.wait(svc.scan_job(0, passive_s=0.3, sdo_timeout=0.01), 30)
        assert job.state == "done", job.error
        assert [n["node_id"] for n in job.result["nodes"]] == [5]      # heard by its heartbeat
        time.sleep(0.1)
        assert sent(tap) == []
        assert engine.channel(0).tx_frames == 0
        assert {i for i, _ in sniffer.frames} == {0x705}               # only the slave's heartbeat on the wire
    finally:
        slave.close()


def test_scan_plan_and_run_are_written_to_nodes_log(svc, channel):
    """user-guide.md:380-382: every scan, plan and run is written to logs/nodes.log."""
    slave = FakeSlave(channel, node_id=5, heartbeat_ms=50)
    try:
        job = svc.wait(svc.scan_job(0, passive_s=0.3, sdo_timeout=0.01), 30)
        assert job.state == "done", job.error
        plan = svc.plan_custom(0, [{"op": "raw", "id": "0x321", "data": "AA"}], {}, "log check")
        svc.run_now(plan.token)
    finally:
        slave.close()
    assert (svc.log_path.parent.name, svc.log_path.name) == ("logs", "nodes.log")
    text = svc.log_path.read_text(encoding="utf-8")
    assert f"[job {job.id}] start: Scan CAN1" in text
    assert f"[job {job.id}] Passive listen" in text and f"[job {job.id}]   node 5: vendor=0xCA0" in text
    assert f"[job {job.id}] 1 node(s)" in text and f"[job {job.id}] done" in text
    assert f"[plan {plan.token}] log check on channel 1: " in text
    assert f"[run {plan.token}] confirmed by user: log check" in text and f"[run {plan.token}] result: ok" in text


# ------------------------------------------------------------------------------------------- plans

def test_optional_step_only_warns_when_it_fails(svc, engine):
    """user-guide.md:436-437: steps marked optional only warn when they fail; the run goes on and succeeds."""
    tap = tx_tap(engine)
    steps = [{"op": "wait_node", "node": 40, "timeout_s": 0.2, "optional": True},     # nobody there: fails
             {"op": "raw", "id": "0x123", "data": "01"}]
    plan = svc.plan_custom(0, steps, {}, "optional check")
    assert plan.preview[0]["flags"] == ["optional"]
    out = svc.run_now(plan.token)                                      # no NodeError: the run did not fail
    assert out["ok"] and out["error"] == ""
    assert [s["status"] for s in out["steps"]] == ["warn", "ok"]
    time.sleep(0.1)
    assert (0x123, b"\x01") in sent(tap)                               # the step after it still ran
    assert f"[run {plan.token}]   warning: node 40 did not answer" in svc.log_path.read_text(encoding="utf-8")


# ------------------------------------------------------------------------------------------- LSS and the simulator

def test_lss_set_id_configures_only_the_selected_device(svc, engine, channel):
    """user-guide.md:862-864: LSS Set ID selects the device by identity, so only it changes, with two LSS devices
    on the bus."""
    two_lss = ((20, "LSS one", 0x0CA2, 0x53494D03, None, False, True),
               (21, "LSS two", 0x0CA2, 0x53494D03, None, False, True))    # same vendor/product, other serial
    sim = SimBus(channel, nodes=two_lss, traffic=False)
    tap = tx_tap(engine)
    try:
        svc.scan(0, passive_s=0.6, sdo_timeout=0.01)
        plan = svc.plan_set_id(0, 20, 30, verify_timeout=2.0)
        assert plan.meta["method"] == "lss"
        sent(tap)
        res = svc.run_now(plan.token)
        time.sleep(0.1)
        assert res["ok"] and res["verified"], res
        one, two = sim.nodes
        assert one.node_id == 30
        assert two.node_id == 21 and two.pending_id is None             # never configured
        lss = [d for i, d in sent(tap) if i == 0x7E5]
        assert bytes([0x04, 0x01]) not in [d[:2] for d in lss]          # never switch global to configuration
        assert [d[0] for d in lss[:4]] == [0x40, 0x41, 0x42, 0x43]      # switch selective first ...
        assert int.from_bytes(lss[3][1:5], "little") == one.serial     # ... by the serial of the selected one
    finally:
        sim.stop()


def test_simulator_demo_devices_really_change_their_node_id(svc, channel):
    """user-guide.md:83: the simulator's demo devices really change their node id (A: store + reset; B: at the
    next NMT reset after the store; the LSS device over LSS)."""
    sim = SimBus(channel, traffic=False)
    a, b, lss = sim.nodes
    try:
        svc.scan(0, passive_s=0.6, sdo_timeout=0.01)
        plans = [svc.plan_set_id(0, 5, 6, verify_timeout=2.0), svc.plan_set_id(0, 10, 11, verify_timeout=2.0),
                 svc.plan_set_id(0, 20, 21, verify_timeout=2.0)]
        res_a, res_b, res_lss = (svc.run_now(p.token) for p in plans)
        assert res_a["verified"] and a.node_id == 6
        assert res_lss["verified"] and lss.node_id == 21
        assert res_b["power_cycle_required"] and b.node_id == 10        # stored, not applied yet
        reset = svc.plan_custom(0, [{"op": "nmt", "command": "reset_node", "node": 10},
                                    {"op": "wait_node", "node": 11, "timeout_s": 2}], {}, "reset B")
        assert svc.run_now(reset.token)["ok"]
        assert b.node_id == 11
    finally:
        sim.stop()


# ------------------------------------------------------------------------------------------- MCP

@pytest.fixture
def app(monkeypatch, channel):
    """The app with its plugins, the simulator on `channel` and channel 0 connected to it."""
    monkeypatch.setenv("CANOPENER_PLUGIN_DIRS", str(ROOT / "plugins"))
    svc = Service()
    with TestClient(create_app(svc), base_url=BASE) as c:
        c.svc = svc
        c.post("/api/sim/start", json={"channel": channel})
        r = c.post("/api/connect", json={"interface": "virtual", "channel": channel, "bitrate": 250000})
        assert r.status_code == 200, r.text
        yield c


def mcp_session(c):
    """call(name, **arguments) -> the tool's result dict."""
    r, _ = _mcp(c, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "test", "version": "1"}})
    assert r.status_code == 200, r.text
    session = r.headers.get("mcp-session-id")
    _mcp(c, "notifications/initialized", session=session)
    ids = iter(range(2, 10_000))

    def call(name, **arguments):
        _, res = _mcp(c, "tools/call", {"name": name, "arguments": arguments}, session=session, id_=next(ids))
        result = res["result"]
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        return json.loads(result["content"][0]["text"])

    def names():
        _, res = _mcp(c, "tools/list", session=session, id_=next(ids))
        return {t["name"] for t in res["result"]["tools"]}
    call.names = names
    return call


def sim_unchanged(sim) -> bool:
    return [n.node_id for n in sim.nodes] == [5, 10, 20] and all(n.writes == [] for n in sim.nodes)


DOCUMENTED_READ_TOOLS = {
    "get_about", "get_status", "list_devices", "get_bus_stats",
    "get_receive_table", "get_trace", "get_signals", "get_signal_history", "get_decoded_frames",
    "list_recordings", "read_recording",
    "scan_nodes", "get_node_scan", "get_node_job", "list_recipes",
    "get_set_node_id_preview", "get_baud_preview", "get_sequence_preview", "get_sdo_write_preview", "get_nmt_preview",
    "get_decode_config", "list_dbc_messages", "get_canopen_state", "list_object_dictionary", "read_sdo",
    "get_j1939_state",
    "list_dashboards", "get_dashboard",
    "get_heartbeat_nodes", "get_eds_problems", "list_eds_objects", "get_eds_pdos", "read_node_vs_eds",
    "get_sniffer_changes",
}


def test_mcp_read_tools_are_exactly_the_documented_list(app):
    """user-guide.md:1011-1021: the read tools (get_/list_/read_/scan_) are exactly the documented ones."""
    names = mcp_session(app).names()
    read = {n for n in names if n.startswith(("get_", "list_", "read_", "scan_"))}
    assert read == DOCUMENTED_READ_TOOLS


def test_mcp_previews_return_frames_and_send_nothing(app):
    """user-guide.md:1016-1017: the preview tools return frames and send nothing."""
    call = mcp_session(app)
    svc = app.svc
    svc.nodes.scan(0, passive_s=0.6, sdo_timeout=0.01)                  # the set-id and baud previews need one
    tap = tx_tap(svc.engine)
    previews = {
        "get_set_node_id_preview": call("get_set_node_id_preview", old_id=5, new_id=6),
        "get_baud_preview": call("get_baud_preview", kbit=500, node=5),
        "get_sequence_preview": call("get_sequence_preview", steps=[{"op": "raw", "id": "0x123", "data": "01"}],
                                     title="poke"),
        "get_sdo_write_preview": call("get_sdo_write_preview", node=5, index="0x2100", sub=0, value="7", type="u8"),
        "get_nmt_preview": call("get_nmt_preview", node=5, command="reset_node"),
    }
    for name, p in previews.items():
        assert "error" not in p, (name, p)
        assert p["token"] and any(s["frames"] for s in p["preview"]), name
    time.sleep(0.3)
    assert sent(tap) == []
    assert svc.nodes.job is None and sim_unchanged(svc.sim)


def test_mcp_scan_read_sdo_and_read_node_vs_eds_change_nothing(app):
    """user-guide.md:1008-1009, 1023-1024: scan_nodes, read_sdo and read_node_vs_eds send only SDO reads and
    change nothing on a node."""
    call = mcp_session(app)
    svc = app.svc
    tap = tx_tap(svc.engine)
    scan = call("scan_nodes", seconds=0.6)
    assert scan["state"] == "done" and {n["node_id"] for n in scan["nodes"]} == {5, 10, 20}
    rd = call("read_sdo", node=5, index="0x1018", sub=1)
    assert rd["raw"] == "A0 0C 00 00"                                  # vendor id of demo device A
    cmp = call("read_node_vs_eds", path=str(ROOT / "tests" / "data" / "test_node.eds"), node=5)
    assert "error" not in cmp and cmp["counts"]
    time.sleep(0.2)
    tx = sent(tap)
    assert tx and not_sdo_uploads(tx) == []
    assert sim_unchanged(svc.sim)


def test_mcp_run_node_plan_runs_the_preview_unchanged_with_its_title(app):
    """user-guide.md:1036-1038: run_node_plan runs the previewed plan unchanged, the title must match, and the run
    is logged as done by Claude."""
    call = mcp_session(app)
    svc = app.svc
    svc.nodes.scan(0, passive_s=0.6, sdo_timeout=0.01)
    p = call("get_set_node_id_preview", old_id=5, new_id=6)
    assert p["title"] == "Node 5 -> 6"
    previewed = [(f["id"], bytes.fromhex(f["data"])) for s in p["preview"] for f in s["frames"]
                 if s["op"] != "wait_node"]          # its poll only goes out if no heartbeat comes first
    tap = tx_tap(svc.engine)
    wrong = call("run_node_plan", token=p["token"], title="Node 5 -> 7")
    assert "Node 5 -> 6" in wrong["error"]
    time.sleep(0.2)
    assert sent(tap) == [] and sim_unchanged(svc.sim)
    job = call("run_node_plan", token=p["token"], title="Node 5 -> 6")
    assert job["state"] == "done", job
    assert [f for f in sent(tap) if f[0] in (0x000, 0x605)] == previewed
    assert svc.sim.nodes[0].node_id == 6
    log_text = svc.nodes.log_path.read_text(encoding="utf-8")
    assert f"[run {p['token']}] confirmed by claude: Node 5 -> 6" in log_text


# ------------------------------------------------------------------------------------------- command line

def test_cli_does_not_start_the_simulator(channel, capsys, monkeypatch):
    """user-guide.md:1093-1094: the command line does not start the simulator; a scan finds an empty virtual bus."""
    started = []
    monkeypatch.setattr("canopener.sim.SimBus.__init__", lambda self, *a, **k: started.append(a))
    rc = cli.main(["scan", "--channel", channel, "--seconds", "0.2", "--sdo-timeout", "0.005", "--json"])
    assert rc == 0 and started == []
    data = json.loads(capsys.readouterr().out)
    assert data["nodes"] == [] and data["raw_ids"] == [] and data["raw_ext_ids"] == []


def _tool_frames(frames):
    """What the tool side sends: SDO requests, NMT, LSS master."""
    return [f for f in frames if 0x600 <= f[0] <= 0x67F or f[0] in (0x000, 0x7E5)]


@pytest.mark.parametrize("command", [["set", "--from", "1", "--to", "2"], ["baud", "--to", "500", "--node", "1"]])
def test_cli_set_and_baud_scan_print_the_plan_and_n_sends_nothing(command, channel, capsys, monkeypatch, sniffer):
    """user-guide.md:1107-1108: set and baud --node scan first, print the exact-frame plan and ask
    `Send this? [y/N]`; answering N sends nothing."""
    slave = FakeSlave(channel, node_id=1, heartbeat_ms=50)            # demo device A: has set-id and baud recipes
    asked = []

    def answer(prompt):
        time.sleep(0.1)
        asked.append((prompt, len(_tool_frames(sniffer.frames))))
        return "n"
    monkeypatch.setattr("builtins.input", answer)
    try:
        rc = cli.main([*command, "--channel", channel, "--scan-seconds", "0.2", "--sdo-timeout", "0.005"])
        time.sleep(0.2)
        out = capsys.readouterr().out
        assert rc == 1 and "Aborted." in out
        assert [p for p, _ in asked] == ["Send this? [y/N] "]
        frames_at_question = asked[0][1]
        before = _tool_frames(sniffer.frames)[:frames_at_question]
        assert before and not_sdo_uploads(before) == []                # the scan ran first, reads only
        assert len(_tool_frames(sniffer.frames)) == frames_at_question  # nothing after the answer
        assert slave.writes == []
        assert out.index("Scanning bus") < out.index("0x601  ")       # the scan, then the exact frames
        assert re.search(r"^ +0x601  2[2-9A-F] 0[01] 21 00 ", out, re.M)  # an SDO download frame of the plan
    finally:
        slave.close()


def test_cli_set_yes_skips_the_question(channel, capsys, monkeypatch):
    """user-guide.md:1108: --yes skips the question and sends the plan."""
    slave = FakeSlave(channel, node_id=1, heartbeat_ms=50)

    def no_input(prompt):
        raise AssertionError("asked although --yes was given")
    monkeypatch.setattr("builtins.input", no_input)
    try:
        rc = cli.main(["set", "--from", "1", "--to", "2", "--yes", "--channel", channel, "--scan-seconds", "0.2",
                       "--sdo-timeout", "0.005", "--verify-timeout", "0.3"])
        assert rc == 0, capsys.readouterr().out
        assert (0x2100, 0, b"\x02") in slave.writes
    finally:
        slave.close()


# ------------------------------------------------------------------------------------------- EDS editor device work

EDS = "/api/x/eds-editor"


def _wait_job(c, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get("/api/nodes/job").json()
        if j.get("state") != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_eds_editor_device_work_only_sends_sdo_reads_and_is_logged(app):
    """user-guide.md:706-707 (and 655-656, Read PDOs from node is a compare): device work only sends SDO reads, runs
    as a node job and is logged in logs/nodes.log."""
    c = app
    doc = c.post(f"{EDS}/new", json={"profile": "301", "file_name": "t"}).json()["doc"]
    tap = tx_tap(c.svc.engine)
    j1 = c.post(f"{EDS}/device/compare", json={"channel": 0, "node": 5, "doc": doc}).json()
    assert _wait_job(c)["state"] == "done"
    j2 = c.post(f"{EDS}/device/probe", json={"channel": 0, "node": 5, "ranges": "1000-1020, 2100-2110"}).json()
    assert _wait_job(c)["state"] == "done"
    time.sleep(0.2)
    tx = sent(tap)
    assert tx and not_sdo_uploads(tx) == []
    assert sim_unchanged(c.svc.sim)
    text = c.svc.nodes.log_path.read_text(encoding="utf-8")
    assert f"[job {j1['id']}] start: Compare node 5" in text and f"[job {j2['id']}] start: Create EDS from node 5" in text


def test_eds_editor_device_work_cannot_start_while_another_node_job_runs(app):
    """user-guide.md:706-707: device work cannot start while another node job runs."""
    c = app
    nodes = c.svc.nodes
    release = threading.Event()
    other = nodes.start_job("custom", "Other job", lambda logline, job: release.wait(10))
    try:
        doc = c.post(f"{EDS}/new", json={"profile": "301", "file_name": "t"}).json()["doc"]
        tap = tx_tap(c.svc.engine)
        r1 = c.post(f"{EDS}/device/compare", json={"channel": 0, "node": 5, "doc": doc})
        r2 = c.post(f"{EDS}/device/probe", json={"channel": 0, "node": 5, "ranges": "1000-1020"})
        assert r1.status_code == 400 and "'Other job' is still running" in r1.text
        assert r2.status_code == 400 and "'Other job' is still running" in r2.text
        assert nodes.job is other
        time.sleep(0.1)
        assert sent(tap) == []
    finally:
        release.set()
        nodes.wait(other, 5)


def test_eds_editor_never_writes_to_a_node(app):
    """user-guide.md:689: the editor never writes to a node; To Nodes only returns steps for the Nodes tab."""
    c = app
    doc = c.post(f"{EDS}/new", json={"profile": "402", "file_name": "drive"}).json()["doc"]
    next(o for o in doc["objects"] if o["index"] == 0x6081)["value"] = "100"
    tap = tx_tap(c.svc.engine)
    s = c.post(f"{EDS}/dcf-steps", json={"doc": doc, "node": 5}).json()
    assert any(st["op"] == "sdo_write" for st in s["steps"])         # steps to hand over, not sent
    time.sleep(0.2)
    assert sent(tap) == [] and c.svc.nodes.plans == {} and c.svc.nodes.job is None
    assert sim_unchanged(c.svc.sim)
    # no code path in the plugin that could write: no SDO download or write stream, no raw send, no plan or run
    for src in (ROOT / "plugins" / "eds-editor").glob("*.py"):
        code = src.read_text(encoding="utf-8")
        for bad in (".download(", "send_message", ".send(", "send_raw", "run_now", "run_job", "plan_custom",
                    "plan_set_id", "plan_baud", ".lss", ".nmt"):
            assert bad not in code, (src.name, bad)
        assert all(m == '"rb"' for m in re.findall(r"sdo\.open\([^,]+,[^,]+,\s*([^,)]+)", code)), src.name
