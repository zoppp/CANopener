"""Tests for the wait_node check (sequence._wait_node), run after a node-id change: waits for a successful SDO read
on the new node id; with an identity, 0x1018 must show the device that was moved."""

import can
import pytest
from fakeslave import FakeSlave

from canopener.nodes import sequence


def _verify(network, new_id, timeout, identity=None):
    ctx = sequence.Context(network=network, vars={}, send_raw=None)
    return sequence._wait_node(ctx, new_id, timeout, identity or {})


def test_verify_new_id_true_when_node_answers_sdo(channel, network):
    slave = FakeSlave(channel, node_id=15, heartbeat_ms=0)
    try:
        assert _verify(network, 15, 2.0) is True
    finally:
        slave.close()


def test_verify_new_id_true_when_node_heartbeats(channel, network):
    slave = FakeSlave(channel, node_id=16, heartbeat_ms=20)
    try:
        assert _verify(network, 16, 2.0) is True
    finally:
        slave.close()


def test_verify_new_id_false_when_nothing_answers(channel, network):
    # No slave at all on this channel.
    assert _verify(network, 99, 0.2) is False


def test_verify_new_id_removes_the_temporary_node_afterwards(channel, network):
    """wait_node adds a scratch RemoteNode to probe with - it must not
    leak into network.nodes once it's done, success or failure."""
    slave = FakeSlave(channel, node_id=17, heartbeat_ms=0)
    try:
        _verify(network, 17, 1.0)
        assert 17 not in network.nodes

        _verify(network, 98, 0.2)
        assert 98 not in network.nodes
    finally:
        slave.close()


def test_verify_new_id_is_not_fooled_by_j1939_or_free_form_frames(channel, network):
    """canopen's scanner counted 0x0CF00482 (J1939 EEC1 from SA 0x82) as node 2: 'verified' with nobody there."""
    bus = can.Bus(interface="virtual", channel=channel, bitrate=250_000)
    try:
        for _ in range(5):
            bus.send(can.Message(arbitration_id=0x0CF00482, is_extended_id=True, data=bytes(8)))
            bus.send(can.Message(arbitration_id=0x182, is_extended_id=False, data=bytes(8)))   # a free-form id
        assert _verify(network, 2, 0.3) is False
    finally:
        bus.shutdown()


def test_verify_new_id_checks_the_identity(channel, network):
    """Another device answering at the new id is not the moved one."""
    slave = FakeSlave(channel, node_id=18, vendor_id=0x1234, heartbeat_ms=0)
    try:
        assert _verify(network, 18, 2.0, {1: 0x1234}) is True
        with pytest.raises(sequence.SequenceError, match="another device"):
            _verify(network, 18, 2.0, {1: 0x9999})
    finally:
        slave.close()
