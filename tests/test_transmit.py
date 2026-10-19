import time

import can
import pytest

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


def _slow_scheduler():
    """A scheduler whose send of id 1 takes 0.3 s (an adapter that blocks); records what went out."""
    import threading

    from canopener.core.transmit import TxScheduler
    sent, started = [], threading.Event()

    def send(ch, msg):
        if msg.arbitration_id == 1:
            started.set()
            time.sleep(0.3)
        sent.append(msg.arbitration_id)
        return 0.0
    tx = TxScheduler(send)
    return tx, sent, started


def test_nothing_is_sent_after_stop_all_returns():
    """Entries due together: stop_all() during the first (slow) send used to let the second one out afterwards."""
    tx, sent, started = _slow_scheduler()
    tx.add(TxEntry(can_id=1, period_ms=1000, enabled=True))
    tx.add(TxEntry(can_id=2, period_ms=1000, enabled=True))
    tx.start()
    try:
        assert started.wait(2)
        tx.stop_all()
        after = list(sent)
        time.sleep(0.5)
        assert sent == after and 2 not in sent
    finally:
        tx.stop()


def test_nothing_is_sent_after_disable_or_remove_returns():
    for how in ("disable", "remove"):
        tx, sent, started = _slow_scheduler()
        tx.add(TxEntry(can_id=1, period_ms=1000, enabled=True))
        e2 = tx.add(TxEntry(can_id=2, period_ms=1000, enabled=True))
        tx.start()
        try:
            assert started.wait(2)
            tx.update(e2.id, {"enabled": False}) if how == "disable" else tx.remove(e2.id)
            after = list(sent)
            time.sleep(0.5)
            assert sent == after and 2 not in sent, how
        finally:
            tx.stop()


def test_send_once_and_disconnect_stops_cyclic(engine, channel):
    engine.connect(ChannelConfig("virtual", channel))
    e = engine.tx.add(TxEntry(can_id=0x10, data="AA"))
    engine.tx.send_once(e.id)
    assert e.count == 1
    c = engine.tx.add(TxEntry(can_id=0x11, period_ms=5, enabled=True))
    time.sleep(0.05)
    engine.disconnect(0)
    assert not c.enabled


@pytest.mark.parametrize("d", [{"period_ms": True}, {"channel": True}, {"channel": 1.9}, {"can_id": True},
                               {"rtr": True, "dlc": 9}, {"rtr": True, "dlc": -1}, {"rtr": True, "dlc": "x"}])
def test_tx_entry_refuses_loose_numbers(d):
    """Review: true became a 1 ms cycle or CAN2, 1.9 became channel 1, a remote frame took any DLC."""
    with pytest.raises(ValueError):
        TxEntry.from_dict({"can_id": 0x100, **d}).message()


@pytest.mark.parametrize("data", [[256], [-1], [True, 1], [1.9]])
def test_data_as_a_list_needs_bytes(data):
    with pytest.raises(ValueError, match="0..255"):
        parse_data(data)


def test_channel_config_parses_fd_and_listen_only_strictly():
    cfg = ChannelConfig.from_dict({"interface": "virtual", "fd": "false", "listen_only": "true"})
    assert cfg.fd is False and cfg.listen_only is True
    with pytest.raises(ValueError):
        ChannelConfig.from_dict({"interface": "virtual", "fd": "maybe"})
