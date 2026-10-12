"""Tests backing the safety claims of docs/user-guide.md and README.md: closing, listen-only, the transmit list
after a restart, projects, file channels, several channels and the adapter refusal messages."""

import json
import time
import uuid

import can
import pytest
from fakeslave import FakeSlave
from fastapi.testclient import TestClient

from canopener.core.adapters import ChannelConfig, backend_bitrate
from canopener.core.engine import EngineError
from canopener.core.recorder import Recorder, read_frames
from canopener.core.transmit import TxEntry
from canopener.nodes.service import NodeError, NodeService
from canopener.paths import app_dir
from canopener.server import create_app
from canopener.service import Service

BASE = "http://127.0.0.1:8775"


# ---------------------------------------------------------------------------------------------- helpers

@pytest.fixture
def sniff(channel):
    """A second handle on the virtual bus: sees every frame CANopener puts on `channel`."""
    bus = can.Bus(interface="virtual", channel=channel)
    yield bus
    bus.shutdown()


@pytest.fixture
def make_svc():
    made = []

    def make():
        svc = Service(load_plugins=False)
        made.append(svc)
        return svc
    yield make
    for svc in made:
        svc.shutdown()


def _drain(bus) -> list:
    out = []
    while (m := bus.recv(0.0)) is not None:
        out.append(m)
    return out


def _assert_quiet(bus, seconds: float = 0.3) -> None:
    """Nothing arrives on `bus` for `seconds` (call after _drain)."""
    m = bus.recv(seconds)
    assert m is None, f"frame 0x{m.arbitration_id:X} arrived"


def _wait(cond, timeout: float = 3.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def _long_replay_file(path, n: int = 500, can_id: int = 0x400):
    """A recording that takes n * 20 ms to replay (10 s for the default)."""
    rec = Recorder(path, {0: 1})
    for i in range(n):
        rec.put(0, (10.0 + i * 0.02, can_id, 0, 1, bytes([i & 0xFF])))
    rec.stop()
    return path


# ---------------------------------------------------------------------------------------------- closing

def test_close_stops_cyclic_and_replay_finishes_recording_and_closes_adapters(make_svc, channel, sniff, tmp_path):
    """user-guide.md 89-91: closing stops cyclic transmits and a replay, finishes a recording, closes adapters."""
    svc = make_svc()
    svc.connect({"interface": "virtual", "channel": channel, "bitrate": 250000})
    svc.tx_add({"can_id": "123", "data": "01 02", "period_ms": 10, "enabled": True, "channel": 0})
    eng = svc.engine
    rec = eng.start_recording(tmp_path / "rec.trc")
    player = eng.start_replay(_long_replay_file(tmp_path / "long.trc"))
    seen = set()

    def both_sending():
        seen.update(m.arbitration_id for m in _drain(sniff))
        return {0x123, 0x400} <= seen
    assert _wait(both_sending), seen
    assert player.state == "playing"

    svc.shutdown()

    assert all(not e.enabled for e in eng.tx.entries)
    assert not eng.tx._thread.is_alive()
    assert player.state == "stopped" and not player._thread.is_alive()
    assert eng.recorder is None and not rec._thread.is_alive()
    st = eng.last_recording
    assert st["stopped"] is not None and st["error"] == ""
    assert st["written"] == st["received"] > 0
    assert len(list(read_frames(rec.path))) == st["written"]        # the file is complete and readable
    assert eng.channels == {}
    _drain(sniff)
    _assert_quiet(sniff)


def test_server_lifespan_end_runs_the_same_shutdown(channel, sniff):
    """user-guide.md 89-91: the app's server shutdown (window close) stops sending and closes adapters."""
    svc = Service(load_plugins=False)
    with TestClient(create_app(svc), base_url=BASE) as c:
        r = c.post("/api/connect", json={"interface": "virtual", "channel": channel, "bitrate": 250000})
        assert r.status_code == 200, r.text
        svc.tx_add({"can_id": "124", "data": "01", "period_ms": 10, "enabled": True, "channel": 0})
        assert _wait(lambda: any(m.arbitration_id == 0x124 for m in _drain(sniff)))
    assert svc.engine.channels == {}
    assert all(not e.enabled for e in svc.engine.tx.entries)
    _drain(sniff)
    _assert_quiet(sniff)


# ---------------------------------------------------------------------------------------------- listen-only

@pytest.fixture
def lo_engine(engine, channel):
    engine.connect(ChannelConfig("virtual", channel, listen_only=True))
    return engine


def test_listen_only_refuses_sending(lo_engine, sniff):
    """user-guide.md 159-160: on a listen-only channel sending is refused."""
    with pytest.raises(EngineError, match="listen-only"):
        lo_engine.send(0, 0x123, b"\x01")
    e = lo_engine.tx.add(TxEntry(can_id=0x10, data="AA"))
    with pytest.raises(can.CanError, match="listen-only"):
        lo_engine.tx.send_once(e.id)
    assert lo_engine.channel(0).tx_frames == 0
    _assert_quiet(sniff)


def test_listen_only_stops_cyclic_entries(lo_engine, sniff):
    """user-guide.md 159-160: on a listen-only channel cyclic entries stop."""
    e = lo_engine.tx.add(TxEntry(can_id=0x321, data="01", period_ms=10, enabled=True))
    assert _wait(lambda: not e.enabled, 2.0)
    assert "listen-only" in e.error and e.count == 0
    assert lo_engine.channel(0).tx_frames == 0
    _assert_quiet(sniff)


def test_listen_only_replay_sends_nothing_and_counts_errors(lo_engine, sniff, tmp_path):
    """user-guide.md 159-160: a replay onto a listen-only channel sends nothing; each frame is a send error."""
    path = _long_replay_file(tmp_path / "short.trc", n=5)
    p = lo_engine.start_replay(path)
    assert _wait(lambda: p.state == "done", 3.0), p.status()
    assert p.sent == 0 and p.errors == 5 and "listen-only" in p.last_error
    assert lo_engine.channel(0).tx_frames == 0
    _assert_quiet(sniff)


def test_listen_only_refuses_node_changes(lo_engine):
    """user-guide.md 159-160: on a listen-only channel node changes are refused."""
    ns = NodeService(lo_engine)
    with pytest.raises(NodeError, match="listen-only"):
        ns.plan_set_id(0, 5, 6)
    with pytest.raises(NodeError, match="listen-only"):
        ns.plan_baud(0, 500)
    with pytest.raises(NodeError, match="listen-only"):
        ns.plan_custom(0, [{"op": "wait", "ms": 1}])


def test_listen_only_scan_only_listens(lo_engine, channel, sniff):
    """user-guide.md 159-160: on a listen-only channel a node scan only listens."""
    slave = FakeSlave(channel, node_id=5, heartbeat_ms=50)
    try:
        r = NodeService(lo_engine).scan(0, passive_s=0.3, sdo_timeout=0.01)
    finally:
        slave.close()
    assert 5 in r.nodes and r.nodes[5].vendor_id is None             # found by heartbeat, identity not read
    assert lo_engine.channel(0).tx_frames == 0
    ids = {m.arbitration_id for m in _drain(sniff)}
    assert ids <= {0x705}, sorted(hex(i) for i in ids)                # only the slave's heartbeat on the bus


# ---------------------------------------------------------------------------------------------- transmit list

def _settings() -> dict:
    p = app_dir() / "settings.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def test_transmit_list_saved_automatically_and_cyclic_not_restarted(make_svc, channel, sniff):
    """user-guide.md 264-265: the transmit list is saved automatically; cyclic entries never start by themselves."""
    svc = make_svc()
    svc.tx_add({"can_id": "321", "data": "11 22", "period_ms": 10, "enabled": True, "channel": 0,
                "comment": "auto"})
    assert _wait(lambda: any(d.get("comment") == "auto" for d in _settings().get("tx", [])), 4.0)
    saved = [d for d in _settings()["tx"] if d["comment"] == "auto"]
    assert saved[0]["enabled"] is False
    svc.shutdown()

    svc2 = make_svc()                                                 # the restart
    (e,) = svc2.engine.tx.entries
    assert (e.can_id, e.data, e.period_ms, e.comment) == (0x321, "11 22", 10.0, "auto")
    assert e.enabled is False
    svc2.connect({"interface": "virtual", "channel": channel, "bitrate": 250000})
    _assert_quiet(sniff)


def test_cyclic_entry_saved_as_on_still_starts_off(make_svc, channel, sniff):
    """user-guide.md 264-265: even an entry stored with enabled=true does not start after a restart."""
    p = app_dir() / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"tx": [{"can_id": 0x55, "data": "01", "period_ms": 10, "enabled": True,
                                     "channel": 0}]}), encoding="utf-8")
    svc = make_svc()
    assert [e.enabled for e in svc.engine.tx.entries] == [False]
    svc.connect({"interface": "virtual", "channel": channel, "bitrate": 250000})
    _assert_quiet(sniff)


def test_adapters_never_connected_at_start(channel, sniff):
    """user-guide.md 780: adapters are never connected automatically at start."""
    p = app_dir() / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"connections": [{"interface": "virtual", "channel": channel, "bitrate": 250000,
                                              "index": 0}]}), encoding="utf-8")
    svc = Service(load_plugins=False)
    with TestClient(create_app(svc), base_url=BASE) as c:
        assert svc.engine.channels == {}
        assert c.get("/api/status").json()["channels"] == []
        assert svc.settings["connections"][0]["channel"] == channel   # remembered for the Connect dialog only
    _assert_quiet(sniff, 0.1)


# ---------------------------------------------------------------------------------------------- projects

def test_open_project_stops_cyclic_and_replaces_transmit_list(make_svc, channel, sniff):
    """user-guide.md 774-776: opening a project stops cyclic transmits and replaces the transmit list."""
    svc = make_svc()
    svc.connect({"interface": "virtual", "channel": channel, "bitrate": 250000})
    svc.tx_add({"can_id": "100", "data": "01", "period_ms": 0, "comment": "in project"})
    svc.save_project("p1")
    for e in list(svc.engine.tx.entries):
        svc.tx_remove(e.id)
    running = svc.tx_add({"can_id": "200", "data": "02", "period_ms": 10, "enabled": True})
    svc.settings["dashboards"] = [{"junk": 1}]
    assert _wait(lambda: any(m.arbitration_id == 0x200 for m in _drain(sniff)))

    r = svc.load_project("p1")

    assert [(e.can_id, e.comment, e.enabled) for e in svc.engine.tx.entries] == [(0x100, "in project", False)]
    assert all(e.id != running["id"] for e in svc.engine.tx.entries)
    assert svc.settings["dashboards"] == []
    assert [c["channel"] for c in r["connections"]] == [channel]
    _drain(sniff)
    _assert_quiet(sniff)


def test_open_project_does_not_connect_by_itself(make_svc, channel, sniff):
    """user-guide.md 774-776: with no adapter connected, opening a project only returns the adapters to ask about."""
    svc = make_svc()
    svc.connect({"interface": "virtual", "channel": channel, "bitrate": 250000})
    svc.save_project("p2")
    svc.disconnect(0)
    r = svc.load_project("p2")
    assert svc.engine.channels == {}                                  # the UI asks (connect.js); nothing connects here
    assert [(c["interface"], c["channel"], c["index"]) for c in r["connections"]] == [("virtual", channel, 0)]


# ---------------------------------------------------------------------------------------------- file channel

def test_file_channel_never_transmits(engine, tmp_path):
    """user-guide.md 343: a file channel never transmits."""
    path = _long_replay_file(tmp_path / "view.trc", n=5)
    (ch,) = engine.open_file_view(path)
    i = ch.index
    with pytest.raises(EngineError, match="not connected"):
        engine.send(i, 0x123, b"\x01")
    e = engine.tx.add(TxEntry(can_id=0x321, data="01", period_ms=10, enabled=True, channel=i))
    assert _wait(lambda: not e.enabled, 2.0) and e.count == 0
    with pytest.raises(EngineError):
        engine.start_replay(path, {1: i})
    with pytest.raises(NodeError):
        NodeService(engine).plan_custom(i, [{"op": "wait", "ms": 1}])
    assert ch.tx_frames == 0 and ch._bus is None


# ---------------------------------------------------------------------------------------------- several channels

@pytest.fixture
def two_buses(engine):
    names = [f"canopener-test-{uuid.uuid4()}" for _ in range(2)]
    for n in names:
        engine.connect(ChannelConfig("virtual", n))
    ext = [can.Bus(interface="virtual", channel=n) for n in names]
    yield ext
    for b in ext:
        b.shutdown()


def test_several_channels_at_once(engine, two_buses):
    """README.md 24: several channels at once (each sends and receives on its own bus)."""
    a, b = two_buses
    assert sorted(engine.channels) == [0, 1] and [c.name for c in engine.channels.values()] == ["CAN1", "CAN2"]
    engine.send(0, 0x111, b"\x01")
    engine.send(1, 0x222, b"\x02")
    assert a.recv(1.0).arbitration_id == 0x111 and a.recv(0.1) is None
    assert b.recv(1.0).arbitration_id == 0x222 and b.recv(0.1) is None
    a.send(can.Message(arbitration_id=0x333, is_extended_id=False, data=b"\x03"))
    assert _wait(lambda: engine.channel(0).rx_frames == 1, 1.0)
    assert engine.channel(1).rx_frames == 0


def test_recording_records_every_connected_channel(engine, two_buses, tmp_path):
    """user-guide.md 179: a recording records every connected channel."""
    a, b = two_buses
    rec = engine.start_recording(tmp_path / "both.trc")
    assert sorted(rec.bus_numbers) == [0, 1]
    a.send(can.Message(arbitration_id=0x101, is_extended_id=False, data=b"\x01"))
    b.send(can.Message(arbitration_id=0x202, is_extended_id=False, data=b"\x02"))
    engine.send(1, 0x212, b"\x03")
    assert _wait(lambda: engine.channel(0).rx_frames == 1 and engine.channel(1).rx_frames == 1, 1.0)
    engine.stop_recording()
    got = {(bus, fr[1]) for bus, fr in read_frames(rec.path)}
    assert got == {(1, 0x101), (2, 0x202), (2, 0x212)}


# ---------------------------------------------------------------------------------------------- refusals

@pytest.mark.parametrize("interface, listed", [
    ("pcan", "5k, 10k, 20k, 33k, 47k, 50k, 83k, 95k, 100k, 125k, 250k, 500k, 800k, 1000k"),
    ("slcan", "10k, 20k, 50k, 83.3k, 100k, 125k, 250k, 500k, 750k, 1000k"),
    ("ixxat", "10k, 20k, 50k, 100k, 125k, 250k, 500k, 666.666k, 800k, 1000k"),
])
def test_cannot_run_at_lists_the_rates(engine, interface, listed):
    """user-guide.md 1154-1155: the "cannot run at" message lists the rates the backend can do."""
    with pytest.raises(EngineError) as ei:
        engine.connect(ChannelConfig(interface, bitrate=300_000))
    msg = str(ei.value)
    assert "cannot run at 300000 bit/s" in msg and msg.endswith(f"it supports {listed}")
    for item in listed.split(", "):                                    # every listed rate is accepted
        backend_bitrate(interface, int(round(float(item[:-1]) * 1000)))


@pytest.mark.parametrize("interface", ["gs_usb", "ixxat", "socketcan"])
def test_listen_only_refused_where_the_interface_has_none(engine, interface):
    """user-guide.md 1157-1158: "cannot be opened listen-only" for an interface without listen-only."""
    with pytest.raises(EngineError, match="cannot be opened listen-only"):
        engine.connect(ChannelConfig(interface, listen_only=True))


def test_fd_refused_where_the_interface_has_none(engine):
    """user-guide.md 1157-1158: "does not support CAN FD" for an interface without CAN FD."""
    with pytest.raises(EngineError, match="does not support CAN FD"):
        engine.connect(ChannelConfig("gs_usb", fd=True))
