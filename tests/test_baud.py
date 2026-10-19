"""Tests for baud.py: the LSS bit-rate sequence (bytes, ordering, and the
final "always switch back to waiting" cleanup) and the baud_sdo path."""

import struct
import time

import can

from canopener.nodes import baud as baud_mod
from canopener.nodes.scan import NodeInfo

LSS_MASTER_TX = 0x7E5  # master -> slave
LSS_MASTER_RX = 0x7E4  # slave -> master

CS_SWITCH_STATE_GLOBAL = 0x04
CS_CONFIGURE_NODE_ID = 0x11
CS_CONFIGURE_BIT_TIMING = 0x13
CS_ACTIVATE_BIT_TIMING = 0x15
CS_STORE_CONFIGURATION = 0x17
CS_SWITCH_STATE_SELECTIVE_SERIAL_NUMBER = 0x43
CS_SWITCH_STATE_SELECTIVE_RESPONSE = 0x44

_NOOP = lambda *a, **k: None  # noqa: E731


class FakeLssSlave(can.Listener):
    """Minimal LSS slave: replies "success" to every command that expects a
    reply (configure_node_id, configure_bit_timing, store_configuration,
    and the switch-selective serial-number message), and records every
    request byte-for-byte for assertions. Good enough to exercise
    baud.py's/setid.py's LSS sequences without a full CiA 305 slave.
    """

    def __init__(self, bus):
        self.bus = bus
        self.received: list[bytes] = []

    def on_message_received(self, msg: can.Message) -> None:
        if msg.arbitration_id != LSS_MASTER_TX or msg.is_remote_frame:
            return
        data = bytes(msg.data)
        self.received.append(data)
        cs = data[0]
        if cs == CS_SWITCH_STATE_SELECTIVE_SERIAL_NUMBER:
            self._reply(bytes([CS_SWITCH_STATE_SELECTIVE_RESPONSE]) + b"\x00" * 7)
        elif cs in (CS_CONFIGURE_NODE_ID, CS_CONFIGURE_BIT_TIMING, CS_STORE_CONFIGURATION):
            self._reply(bytes([cs, 0x00]) + b"\x00" * 6)
        # CS_SWITCH_STATE_GLOBAL, the vendor/product/revision selective
        # sub-messages, and CS_ACTIVATE_BIT_TIMING get no reply in real LSS.

    def _reply(self, data: bytes) -> None:
        self.bus.send(can.Message(arbitration_id=LSS_MASTER_RX, is_extended_id=False, data=data))


def test_change_bitrate_lss_global_sends_configure_store_activate_then_waiting(channel, network):
    """The order must be: switch global -> configure (0x13) -> store (0x17)
    -> activate (0x15) -> switch global back to WAITING (0x04/0). The store
    is what makes the rate survive a power cycle, and it comes before the
    activate: after activate, CiA 305 slaves may ignore frames during the
    switch delay, so a store sent then could be lost."""
    other_bus = can.Bus(interface="virtual", channel=channel, bitrate=250_000)
    fake_lss = FakeLssSlave(other_bus)
    notifier = can.Notifier(other_bus, [fake_lss], 1.0)
    try:
        baud_mod.change_bitrate(
            network, new_kbit=500, node_info=None,
            switch_delay_ms=10, verify_timeout=0.2, log=_NOOP,
        )
        time.sleep(0.1)   # let the sniffer's notifier thread take the final switch-to-waiting

        cs_sequence = [data[0] for data in fake_lss.received]
        assert cs_sequence == [
            CS_SWITCH_STATE_GLOBAL,       # -> CONFIGURATION_STATE
            CS_CONFIGURE_BIT_TIMING,
            CS_STORE_CONFIGURATION,
            CS_ACTIVATE_BIT_TIMING,
            CS_SWITCH_STATE_GLOBAL,       # -> WAITING_STATE (final cleanup)
        ]
        assert fake_lss.received[0][1] == 1   # CONFIGURATION_STATE
        assert fake_lss.received[1][2] == 2   # table index 2 == 500 kbit/s
        assert fake_lss.received[-1][1] == 0  # WAITING_STATE
    finally:
        notifier.stop()
        other_bus.shutdown()


def test_change_bitrate_lss_selective_addresses_the_requested_node(channel, network):
    other_bus = can.Bus(interface="virtual", channel=channel, bitrate=250_000)
    fake_lss = FakeLssSlave(other_bus)
    notifier = can.Notifier(other_bus, [fake_lss], 1.0)
    node_info = NodeInfo(node_id=7, vendor_id=0x0CA2, product_code=0x1001,
                          revision_number=0x10000, serial_number=0x42)
    try:
        baud_mod.change_bitrate(
            network, new_kbit=125, node_info=node_info,
            switch_delay_ms=10, verify_timeout=0.2, log=_NOOP,
        )

        # First 4 requests are the selective vendor/product/revision/serial.
        vendor, product, revision, serial = (
            struct.unpack("<I", fake_lss.received[i][1:5])[0] for i in range(4)
        )
        assert (vendor, product, revision, serial) == (0x0CA2, 0x1001, 0x10000, 0x42)
    finally:
        notifier.stop()
        other_bus.shutdown()


def test_change_bitrate_lss_still_switches_to_waiting_when_a_step_fails(channel, network):
    """If a required-reply step never gets one, change_bitrate() must raise
    BaudChangeError (wrapping the canopen LssError) but still send the
    final switch-to-waiting - a failed sequence must not strand the bus's
    LSS devices in configuration state."""
    other_bus = can.Bus(interface="virtual", channel=channel, bitrate=250_000)

    class _NoReplySlave(can.Listener):
        def __init__(self):
            self.received = []

        def on_message_received(self, msg):
            if msg.arbitration_id == LSS_MASTER_TX:
                self.received.append(bytes(msg.data))
            # Never replies to anything - simulates a device that doesn't
            # support LSS at all (or one that's absent).

    no_reply = _NoReplySlave()
    notifier = can.Notifier(other_bus, [no_reply], 1.0)
    # Speed the test up: the real RESPONSE_TIMEOUT (0.5s) would make this
    # test slow across 1 failing call.
    network.lss.RESPONSE_TIMEOUT = 0.05
    try:
        raised = False
        try:
            baud_mod.change_bitrate(
                network, new_kbit=250, node_info=None,
                switch_delay_ms=10, verify_timeout=0.2, log=_NOOP,
            )
        except baud_mod.BaudChangeError:
            raised = True
        assert raised

        # Give the sniffer's Notifier thread a moment to dequeue the final
        # switch-to-waiting message (it's sent right as change_bitrate()
        # raises, and delivery to the listener happens on another thread).
        time.sleep(0.1)

        # switch global (config) was sent, configure_bit_timing was sent and
        # timed out (raising), and the finally block still sent the
        # switch-to-waiting afterwards.
        cs_sequence = [data[0] for data in no_reply.received]
        assert cs_sequence == [CS_SWITCH_STATE_GLOBAL, CS_CONFIGURE_BIT_TIMING, CS_SWITCH_STATE_GLOBAL]
        assert no_reply.received[0][1] == 1   # -> CONFIGURATION_STATE
        assert no_reply.received[-1][1] == 0  # -> WAITING_STATE (cleanup)
    finally:
        notifier.stop()
        other_bus.shutdown()


def test_change_bitrate_sdo_power_cycle_writes_value_then_store_and_returns_none(channel, network):
    from fakeslave import FakeSlave

    slave = FakeSlave(channel, node_id=1, heartbeat_ms=0)
    try:
        spec = {
            "index": "0x2101", "sub": 0, "type": "u8", "unit": "enum",
            "enum": {"250": 3}, "store": "0x1010:01", "effect": "power_cycle",
        }
        result = baud_mod.change_bitrate_sdo(
            network, old_id=1, spec=spec, new_kbit=250,
            verify_timeout=0.2, log=_NOOP,
        )

        assert result is None  # distinct from False - nothing to verify yet
        assert slave.writes == [
            (0x2101, 0, bytes([3])),
            (0x1010, 1, struct.pack("<I", 0x65766173)),
        ]
    finally:
        slave.close()


def test_sdo_bit_rate_change_cannot_be_stopped_after_the_reset():
    """A stop after the NMT reset left the node at the new rate and the adapter at the old one (as LSS does)."""
    from canopener.nodes import recipes as recipes_module
    spec = {"index": "0x2101", "sub": 0, "type": "u8", "values": {"250": 3, "500": 2}, "effect": "nmt_reset"}
    steps, _ = recipes_module.baud_sdo_steps(spec, 500)
    reset = next(i for i, s in enumerate(steps) if s["op"] == "nmt")
    after = [s for s in steps[reset + 1:] if s["op"] in ("delay", "reopen")]
    assert after and all(s.get("nostop") for s in after)
