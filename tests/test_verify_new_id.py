"""Tests for the wait_node check (sequence._wait_node), run after a node-id change: waits for a heartbeat/boot-up or a
successful SDO 0x1000 read on the new node id."""

from fakeslave import FakeSlave

from canopener.nodes import sequence


def _verify(network, new_id, timeout):
    ctx = sequence.Context(network=network, vars={}, send_raw=None)
    return sequence._wait_node(ctx, new_id, timeout)


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
