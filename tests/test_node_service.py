import struct
import time

import can
import pytest
from fakeslave import FakeSlave
from test_baud import FakeLssSlave

from canopener.core.adapters import ChannelConfig
from canopener.nodes import recipes as recipes_module
from canopener.nodes.service import NodeError, NodeService


class Sniffer(can.Listener):
    def __init__(self):
        self.frames = []

    def on_message_received(self, msg):
        self.frames.append((msg.arbitration_id, bytes(msg.data)))


@pytest.fixture
def svc(engine, channel):
    engine.connect(ChannelConfig("virtual", channel))
    return NodeService(engine)


@pytest.fixture
def sniffer(channel):
    bus = can.Bus(interface="virtual", channel=channel)
    s = Sniffer()
    n = can.Notifier(bus, [s], 0.5)
    yield s
    n.stop()
    bus.shutdown()


def test_scan_job_through_the_bridge(svc, channel):
    slave = FakeSlave(channel, node_id=5, heartbeat_ms=50)
    try:
        job = svc.wait(svc.scan_job(0, passive_s=0.3, sdo_timeout=0.01))
        assert job.state == "done", job.error
        nodes = job.result["nodes"]
        assert [n["node_id"] for n in nodes] == [5]
        assert nodes[0]["recipe"] == "sim-demo-a" and nodes[0]["baud"] == "sdo"
    finally:
        slave.close()


def test_set_id_preview_matches_wire_and_token_is_single_use(svc, channel, sniffer):
    slave = FakeSlave(channel, node_id=1, heartbeat_ms=50)             # demo device A
    try:
        svc.scan(0, passive_s=0.3, sdo_timeout=0.01)
        plan = svc.plan_set_id(0, 1, 2, verify_timeout=0.3)
        previewed = [(f["id"], bytes.fromhex(f["data"])) for p in plan.preview for f in p["frames"]
                     if p["op"] != "wait_node"]          # its poll only goes out if no heartbeat comes first
        sniffer.frames.clear()
        res = svc.run_now(plan.token)
        time.sleep(0.1)
        sent = [f for f in sniffer.frames if f[0] in (0x000, 0x601)]
        assert sent == previewed
        assert slave.writes == [(0x2100, 0, b"\x02"), (0x1010, 1, struct.pack("<I", 0x65766173))]
        assert res["ok"] and not res["power_cycle_required"]
        with pytest.raises(NodeError):
            svc.run_now(plan.token)                      # already used
    finally:
        slave.close()


def test_set_id_needs_a_scan_and_refuses_readonly(svc, channel):
    with pytest.raises(NodeError, match="scan"):
        svc.plan_set_id(0, 1, 2)
    slave = FakeSlave(channel, node_id=50, vendor_id=0x777, heartbeat_ms=50)
    svc.recipes = lambda: {"default": {"method": "lss"},
                           "vendors": {"0x777": {"name": "locked", "method": "readonly", "note": "this tool cannot change it"}}}
    try:
        svc.scan(0, passive_s=0.3, sdo_timeout=0.01)
        with pytest.raises(NodeError, match="read-only"):
            svc.plan_set_id(0, 50, 51)
    finally:
        slave.close()


def test_custom_sequence_runs_sdo_raw_delay_and_logs(svc, channel, sniffer):
    slave = FakeSlave(channel, node_id=9, heartbeat_ms=0)
    try:
        steps = [
            {"op": "sdo_read", "node": 9, "index": "0x1018", "sub": 1, "type": "u32", "expect": "0x0CA0"},
            {"op": "sdo_write", "node": "$OLD_ID", "index": "0x2110", "sub": 1, "type": "u8", "value": "$NEW_ID"},
            {"op": "raw", "id": "0x7FF", "data": "AA $NEW_ID 55"},
            {"op": "delay", "ms": 20},
        ]
        plan = svc.plan_custom(0, steps, {"OLD_ID": 9, "NEW_ID": 0x30}, "try it")
        assert plan.preview[2]["frames"][0]["data"] == "AA 30 55"
        out = svc.run_now(plan.token)
        assert out["ok"] and out["steps"][0]["value"] == 0x0CA0
        time.sleep(0.1)
        assert (0x7FF, b"\xAA\x30\x55") in sniffer.frames
        assert slave.writes == [(0x2110, 1, b"\x30")]
        assert "try it" in svc.log_path.read_text()
    finally:
        slave.close()


def test_custom_sequence_failure_still_runs_always_steps(svc, channel, sniffer):
    steps = [
        {"op": "sdo_read", "node": 40, "index": "0x1000", "sub": 0},          # nobody there: fails
        {"op": "raw", "id": "0x100", "data": "01"},                           # skipped
        {"op": "raw", "id": "0x101", "data": "02", "always": True},           # cleanup: runs
    ]
    plan = svc.plan_custom(0, steps, {})
    with pytest.raises(NodeError):
        svc.run_now(plan.token)
    time.sleep(0.1)
    ids = [f[0] for f in sniffer.frames]
    assert 0x101 in ids and 0x100 not in ids


def test_global_lss_baud_change_reopens_and_verifies(svc, engine, channel):
    other = can.Bus(interface="virtual", channel=channel)
    fake = FakeLssSlave(other)
    notifier = can.Notifier(other, [fake], 0.5)
    hb = other.send_periodic(can.Message(arbitration_id=0x705, is_extended_id=False, data=b"\x05"), 0.05)
    try:
        plan = svc.plan_baud(0, 500, None, switch_delay_ms=10, verify_timeout=1.0)
        assert any("Every LSS device" in w for w in plan.warnings)
        out = svc.run_now(plan.token)
        assert out["ok"] and out["verified"]
        assert engine.channel(0).cfg.bitrate == 500_000
        cs = [d[0] for d in fake.received]
        assert cs == [0x04, 0x13, 0x17, 0x15, 0x04]
    finally:
        hb.stop()
        notifier.stop()
        other.shutdown()


def test_saved_user_recipe_is_picked_by_vendor(svc, channel):
    steps = [{"op": "sdo_write", "node": "$OLD_ID", "index": "0x2110", "sub": 1, "type": "u8", "value": "$NEW_ID"}]
    svc.save_recipe(0x0CA0, "my-demo", "set_id", steps, effect="power_cycle")
    rec = recipes_module.resolve_recipe(recipes_module.load_recipes(), 0x0CA0, product_code=0x53494D01)
    assert rec["name"] == "my-demo" and rec["method"] == "sequence" and rec["user"]
    assert rec.get("baud_sdo")                    # bundled fields kept under the user ones
    slave = FakeSlave(channel, node_id=3, heartbeat_ms=50)
    try:
        svc.scan(0, passive_s=0.3, sdo_timeout=0.01)
        plan = svc.plan_set_id(0, 3, 4)
        assert plan.meta["recipe"] == "my-demo"
        svc.run_now(plan.token)
        assert slave.writes == [(0x2110, 1, b"\x04")]
    finally:
        slave.close()


def test_plan_is_bound_to_the_adapter_and_cannot_be_edited(svc, engine, channel):
    steps = [{"op": "raw", "id": "0x123", "data": "01"}]
    plan = svc.plan_custom(0, steps, {})
    steps[0]["data"] = "FF"                               # editing the caller's list changes nothing
    assert svc.plans[plan.token].steps[0]["data"] == "01"
    plan2 = svc.plan_custom(0, [{"op": "raw", "id": "0x124", "data": "02"}], {})
    engine.connect(ChannelConfig("virtual", channel + "-other"), 0)       # reconnect another bus at index 0
    with pytest.raises(NodeError, match="reconnected"):
        svc.run_now(plan2.token)


def test_cli_style_run_is_logged(svc, channel):
    plan = svc.plan_custom(0, [{"op": "raw", "id": "0x321", "data": "AA"}], {}, "logged run")
    svc.run_now(plan.token)                               # no log callback, like the CLI with -v off
    text = svc.log_path.read_text()
    assert "confirmed by user" in text and "step 1: Raw frame 0x321" in text and "result: ok" in text


def test_interrupt_still_runs_always_steps(svc, channel, sniffer):
    from canopener.nodes import sequence
    calls = []
    steps = [{"op": "delay", "ms": 10}, {"op": "raw", "id": "0x100", "data": "01"},
             {"op": "raw", "id": "0x101", "data": "02", "always": True}]

    def send_raw(can_id, data, *a):
        calls.append(can_id)
    ctx = sequence.Context(network=None, vars={}, send_raw=send_raw)
    real = sequence._run_step

    def boom(c, step, v):
        if step["op"] == "delay":
            raise KeyboardInterrupt
        return real(c, step, v)
    sequence._run_step = boom
    try:
        with pytest.raises(KeyboardInterrupt):
            sequence.execute(steps, ctx)
    finally:
        sequence._run_step = real
    assert calls == [0x101]


def test_preview_shows_fd_padding_and_wait_node_polling():
    from canopener.nodes import sequence
    pv = sequence.preview([{"op": "raw", "id": "0x100", "data": "01 02 03 04 05 06 07 08 09", "fd": True, "brs": True},
                           {"op": "wait_node", "node": 5}], {})
    assert len(bytes.fromhex(pv[0]["frames"][0]["data"])) == 12 and pv[0]["frames"][0]["brs"]
    assert pv[1]["frames"][0]["id"] == 0x605
    with pytest.raises(sequence.SequenceError):
        sequence.preview([{"op": "sdo_write", "node": 1, "index": "0x2100", "sub": 0, "type": "bytes", "value": ""}], {})


def test_set_id_refuses_an_id_already_in_use(svc, channel):
    """Seen in the UI: 5 -> 10 with node 10 present was only a warning, and its answer 'verified' the move."""
    a, b = FakeSlave(channel, node_id=5, heartbeat_ms=50), FakeSlave(channel, node_id=10, heartbeat_ms=50)
    try:
        svc.scan(0, passive_s=0.3, sdo_timeout=0.01)
        with pytest.raises(NodeError, match="already in use"):
            svc.plan_set_id(0, 5, 10)
        assert svc.plan_set_id(0, 5, 11).token                          # a free id still works
    finally:
        a.close(); b.close()


def test_a_reconnect_makes_the_scan_stale(svc, engine, channel):
    """After a reconnect the device at an id may be another one: the old scan must not drive a recipe."""
    slave = FakeSlave(channel, node_id=5, heartbeat_ms=50)
    try:
        svc.scan(0, passive_s=0.3, sdo_timeout=0.01)
        assert svc.scan_dict(0) is not None
        engine.connect(ChannelConfig("virtual", channel), index=0)        # reconnect at the same index
        with pytest.raises(NodeError, match="scan the bus first"):
            svc.plan_set_id(0, 5, 6)
        with pytest.raises(NodeError, match="not found in the last scan"):
            svc.plan_baud(0, 500, node=5)
        assert svc.scan_dict(0) is None
    finally:
        slave.close()


def test_a_vendor_wide_recipe_warns_in_the_preview(svc, channel, monkeypatch):
    """A recipe without a product code is picked for every product of the vendor: the plan must say so."""
    bundled = recipes_module.load_recipes()
    wide = dict(bundled["vendors"]["0x0CA0"])
    wide.pop("product_code")
    monkeypatch.setattr(svc, "recipes", lambda: {**bundled, "vendors": {"0x0CA0": wide}})
    slave = FakeSlave(channel, node_id=1, product_code=0x7777, heartbeat_ms=50)
    try:
        svc.scan(0, passive_s=0.3, sdo_timeout=0.01)
        plan = svc.plan_set_id(0, 1, 2)
        assert any("every product of vendor 0xCA0" in w and "0x7777" in w for w in plan.warnings), plan.warnings
        assert any("every product" in w for w in svc.plan_baud(0, 500, node=1).warnings)
        monkeypatch.setattr(svc, "recipes", lambda: bundled)          # the bundled one names its product
        slave.close()
        slave = FakeSlave(channel, node_id=1, heartbeat_ms=50)
        svc.scan(0, passive_s=0.3, sdo_timeout=0.01)
        assert not any("every product" in w for w in svc.plan_set_id(0, 1, 2).warnings)
    finally:
        slave.close()


def test_a_user_recipe_does_not_inherit_the_bundled_product_code(svc):
    """Review: a saved recipe kept the bundled model's product code, so it replaced that model's recipe only."""
    steps = [{"op": "sdo_write", "node": "$OLD_ID", "index": "0x2110", "sub": 1, "type": "u8", "value": "$NEW_ID"}]
    svc.save_recipe(0x0CA0, "my-other-model", "set_id", steps)
    rec = recipes_module.resolve_recipe(recipes_module.load_recipes(), 0x0CA0, product_code=0x7777)
    assert rec["name"] == "my-other-model" and recipes_module.vendor_wide(rec)    # every product, with a warning


def test_sdo_bit_rate_change_first_checks_the_device_identity(svc, channel):
    """Review: Set ID read 0x1018 before writing, the SDO bit-rate change did not (another machine since the scan)."""
    slave = FakeSlave(channel, node_id=1, heartbeat_ms=50)              # demo device A: an SDO bit-rate recipe
    try:
        svc.scan(0, passive_s=0.3, sdo_timeout=0.01)
        plan = svc.plan_baud(0, 500, node=1)
        first = plan.steps[0]
        assert first["op"] == "sdo_read" and first["index"] == "0x1018" and first["node"] == "$OLD_ID"
        assert "expect" in first
    finally:
        slave.close()


def test_a_stopped_or_failed_run_says_what_was_already_sent(svc, channel):
    slave = FakeSlave(channel, node_id=9, heartbeat_ms=0)
    try:
        steps = [{"op": "sdo_write", "node": 9, "index": "0x2110", "sub": 1, "type": "u8", "value": 5},
                 {"op": "sdo_read", "node": 40, "index": "0x1000", "sub": 0}]       # nobody there: fails
        plan = svc.plan_custom(0, steps, {})
        with pytest.raises(NodeError, match="Already sent before that: .*0x2110"):
            svc.run_now(plan.token)
    finally:
        slave.close()


def test_a_custom_run_that_writes_makes_the_scan_stale(svc, channel):
    """Review: custom mode can move a node; the 'id already in use' check must not use the old scan."""
    slave = FakeSlave(channel, node_id=9, heartbeat_ms=50)
    try:
        svc.scan(0, passive_s=0.3, sdo_timeout=0.01)
        read = svc.plan_custom(0, [{"op": "sdo_read", "node": 9, "index": "0x1000", "sub": 0}], {})
        svc.run_now(read.token)
        assert svc.scan_dict(0) is not None                              # a read changes nothing
        write = svc.plan_custom(0, [{"op": "sdo_write", "node": 9, "index": "0x2110", "sub": 1, "type": "u8",
                                     "value": 5}], {})
        svc.run_now(write.token)
        assert svc.scan_dict(0) is None
    finally:
        slave.close()


def test_a_recipe_forced_by_name_warns_when_the_product_differs(svc, channel):
    slave = FakeSlave(channel, node_id=1, product_code=0x7777, heartbeat_ms=50)
    try:
        svc.scan(0, passive_s=0.3, sdo_timeout=0.01)
        plan = svc.plan_set_id(0, 1, 2, method="sdo:sim-demo-a")
        assert any("is for product" in w and "0x7777" in w for w in plan.warnings), plan.warnings
    finally:
        slave.close()
