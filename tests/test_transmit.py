import time

import can

from canopener.core.adapters import ChannelConfig
from canopener.core.transmit import TxEntry, build_message, parse_data


def test_parse_data_forms():
    assert parse_data("11 22 33") == b"\x11\x22\x33"
    assert parse_data("112233") == b"\x11\x22\x33"
    assert parse_data("0x11,0x22") == b"\x11\x22"
    assert parse_data("") == b""


def test_build_message_rules():
    m = build_message(0x18FF0000, b"\x01")
    assert m.is_extended_id
    m = build_message(0x100, bytes(10), fd=True)
    assert len(m.data) == 12                       # padded to the next CAN FD length
    for bad in (dict(can_id=0x800, data=b"", ext=False), dict(can_id=0x100, data=bytes(9))):
        try:
            build_message(**bad)
            raise AssertionError(bad)
        except ValueError:
            pass


def test_cyclic_entry_sends_at_period(engine, channel):
    engine.connect(ChannelConfig("virtual", channel))
    sniff = can.Bus(interface="virtual", channel=channel)
    try:
        e = engine.tx.add(TxEntry(can_id=0x321, data="01 02", period_ms=10, enabled=True))
        time.sleep(0.5)
        engine.tx.update(e.id, {"enabled": False})
        n = 0
        while sniff.recv(0.05) is not None:
            n += 1
        assert 35 <= n <= 60, n                    # ~50 frames in 0.5 s
        assert e.count == n
    finally:
        sniff.shutdown()


def test_send_once_and_disconnect_stops_cyclic(engine, channel):
    engine.connect(ChannelConfig("virtual", channel))
    e = engine.tx.add(TxEntry(can_id=0x10, data="AA"))
    engine.tx.send_once(e.id)
    assert e.count == 1
    c = engine.tx.add(TxEntry(can_id=0x11, period_ms=5, enabled=True))
    time.sleep(0.05)
    engine.disconnect(0)
    assert not c.enabled
