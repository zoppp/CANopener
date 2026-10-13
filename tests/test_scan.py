import can
from fakeslave import FakeSlave

from canopener.nodes import scan as scan_mod

# Short timeout: the fake slave is in-process on a virtual bus, so we don't
# need hardware-scale margins, and it keeps the 1..127 active ping fast.
SDO_TIMEOUT = 0.01


def test_scan_finds_fake_slave_and_reads_identity(channel, network):
    slave = FakeSlave(channel, node_id=72)
    try:
        result = scan_mod.scan(network, passive_seconds=0.5, sdo_timeout=SDO_TIMEOUT)

        assert 72 in result.nodes
        info = result.nodes[72]
        assert info.vendor_id == slave.vendor_id
        assert info.product_code == slave.product_code
        assert info.revision_number == slave.revision
        assert info.serial_number == slave.serial
        assert info.device_name == "Demo device A (test)"
        # Found via heartbeat (passive) and/or the active SDO ping.
        assert info.seen_by & {"heartbeat", "sdo"}
    finally:
        slave.close()


def test_scan_records_raw_ids_including_non_node_traffic(channel, network):
    """Conflict checking needs every raw ID on the bus, not just CANopen
    node traffic (e.g. a free-form-ID motor controller)."""
    slave = FakeSlave(channel, node_id=10, heartbeat_ms=50)
    # A free-form frame that isn't part of the CiA 301 predefined connection
    # set for any node - simulates free-form traffic. Sent from a
    # separate bus instance (python-can's virtual bus does not loop a
    # message back to its own sender) periodically so it's guaranteed to
    # land during the scan's passive-listen window regardless of timing.
    other_bus = can.Bus(interface="virtual", channel=channel, bitrate=250_000)
    free_form_task = other_bus.send_periodic(
        can.Message(arbitration_id=0x48A, is_extended_id=False, data=b"\x01\x02\x03\x04"), 0.05
    )
    try:
        result = scan_mod.scan(network, passive_seconds=0.4, sdo_timeout=SDO_TIMEOUT)

        assert 0x48A in result.raw_ids
        assert (0x700 + 10) in result.raw_ids
    finally:
        free_form_task.stop()
        other_bus.shutdown()
        slave.close()


def test_scan_does_not_list_phantom_node_from_pdo_range_free_form_traffic(channel, network):
    """0x48A is in the TxPDO4 window (0x480 + node id), so it LOOKS like
    node 10 - but it's really free-form traffic with no CANopen
    node behind it. It must not become a phantom node (which would also
    cost an identity-read timeout), and must be reported separately."""
    other_bus = can.Bus(interface="virtual", channel=channel, bitrate=250_000)
    free_form_task = other_bus.send_periodic(
        can.Message(arbitration_id=0x48A, is_extended_id=False, data=b"\x01\x02\x03\x04"), 0.05
    )
    try:
        result = scan_mod.scan(network, passive_seconds=0.3, sdo_timeout=SDO_TIMEOUT)

        assert 10 not in result.nodes
        assert 10 in result.pdo_only_ids
        assert result.pinged is True
    finally:
        free_form_task.stop()
        other_bus.shutdown()


def test_listen_only_scan_does_not_claim_pdo_ids_have_no_node(channel, network):
    """A listen-only scan never pings, so a PDO-range id without a heartbeat may still be a node (e.g. one with
    remapped PDO COB-IDs). It is reported as not asked, never as 'no CANopen node behind it'."""
    other_bus = can.Bus(interface="virtual", channel=channel, bitrate=250_000)
    task = other_bus.send_periodic(
        can.Message(arbitration_id=0x48A, is_extended_id=False, data=b"\x01\x02\x03\x04"), 0.05
    )
    lines = []
    try:
        result = scan_mod.scan(network, passive_seconds=0.3, log=lines.append, active=False)

        assert 10 in result.pdo_only_ids
        assert result.pinged is False and result.as_dict()["pinged"] is False
        text = "\n".join(lines)
        assert "no CANopen node behind" not in text and "not pinged" in text
    finally:
        task.stop()
        other_bus.shutdown()


def test_scan_promotes_pdo_hint_to_a_real_node_when_it_also_answers_sdo(channel, network):
    """A node that both sends PDO traffic AND answers the SDO ping is a real
    node, not just a PDO-only hint - and its seen_by should show both."""
    slave = FakeSlave(channel, node_id=14, heartbeat_ms=0)
    other_bus = can.Bus(interface="virtual", channel=channel, bitrate=250_000)
    # 0x48E == 0x480 + 14: this node's own TxPDO traffic.
    pdo_task = other_bus.send_periodic(
        can.Message(arbitration_id=0x48E, is_extended_id=False, data=b"\x00" * 8), 0.05
    )
    try:
        result = scan_mod.scan(network, passive_seconds=0.3, sdo_timeout=SDO_TIMEOUT)

        assert 14 in result.nodes
        assert 14 not in result.pdo_only_ids
        assert result.nodes[14].seen_by >= {"sdo", "pdo"}
    finally:
        pdo_task.stop()
        other_bus.shutdown()
        slave.close()


def test_scan_survives_rtr_and_extended_frames(channel, network):
    """An RTR (remote) frame on a heartbeat COB-ID carries no real payload
    even when its DLC says otherwise - reading data[0] off it used to raise
    IndexError inside the Notifier thread and kill reception for the rest
    of the scan. An extended (29-bit) id sharing a heartbeat COB-ID's low
    bits must not be mistaken for that node either. Neither should stop the
    scan from finding a real node."""
    slave = FakeSlave(channel, node_id=20, heartbeat_ms=50)
    other_bus = can.Bus(interface="virtual", channel=channel, bitrate=250_000)
    # RTR on node 30's heartbeat COB-ID, DLC=1 but (as an RTR) no data bytes.
    rtr_task = other_bus.send_periodic(
        can.Message(arbitration_id=0x700 + 30, is_extended_id=False, dlc=1, is_remote_frame=True), 0.05,
    )
    # Extended id whose low 11 bits alias node 20's heartbeat COB-ID.
    ext_task = other_bus.send_periodic(
        can.Message(arbitration_id=0x1FFFF700 + 20, is_extended_id=True, data=b"\xAA"), 0.05,
    )
    try:
        result = scan_mod.scan(network, passive_seconds=0.4, sdo_timeout=SDO_TIMEOUT)

        # The scan must still work (no dead notifier) and still find the
        # real node.
        assert 20 in result.nodes
        assert "heartbeat" in result.nodes[20].seen_by
        # The extended id must not be folded into the 11-bit raw_ids used
        # for conflict checking, nor mistaken for node 20's own heartbeat.
        assert (0x1FFFF700 + 20) in result.raw_ext_ids
        assert 30 not in result.nodes  # the RTR alone proves nothing
    finally:
        rtr_task.stop()
        ext_task.stop()
        other_bus.shutdown()
        slave.close()


def test_scan_finds_node_with_no_heartbeat_via_active_sdo_ping(channel, network):
    """A device that never heartbeats (some devices never send one) must still be
    found by the active SDO ping."""
    slave = FakeSlave(channel, node_id=5, heartbeat_ms=0)
    try:
        result = scan_mod.scan(network, passive_seconds=0.2, sdo_timeout=SDO_TIMEOUT)

        assert 5 in result.nodes
        assert "sdo" in result.nodes[5].seen_by
        assert "heartbeat" not in result.nodes[5].seen_by
    finally:
        slave.close()
