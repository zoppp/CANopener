import time

import can

from canopener.core.adapters import ChannelConfig, bus_kwargs
from canopener.core.channel import F_EXT, F_TX, Tap


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_receive_table_counts_and_cycle_time(engine, channel):
    ch = engine.connect(ChannelConfig("virtual", channel))
    other = can.Bus(interface="virtual", channel=channel)
    try:
        for i in range(5):
            other.send(can.Message(arbitration_id=0x123, is_extended_id=False, data=bytes([i, 2, 3])))
            time.sleep(0.02)
        other.send(can.Message(arbitration_id=0x18FEF100, is_extended_id=True, data=b"\x01"))
        assert _wait(lambda: ch.rx_frames == 6)
        seq, rows = ch.rows_snapshot()
        by_id = {r[1]: r for r in rows}
        r = by_id[0x123]
        assert r[6] == 5                                  # count
        assert r[4] == bytes([4, 2, 3]) and r[5] == bytes([3, 2, 3])   # data, previous data
        assert 0.01 < r[9] < 0.1                          # last cycle time ~20 ms
        assert by_id[0x18FEF100][2] & F_EXT
        _, changed = ch.rows_snapshot(seq)
        assert changed == []                              # nothing changed since seq
    finally:
        other.shutdown()


def test_trace_since_and_tx_entries(engine, channel):
    ch = engine.connect(ChannelConfig("virtual", channel))
    other = can.Bus(interface="virtual", channel=channel)
    try:
        engine.send(0, 0x10, b"\xAA")
        other.send(can.Message(arbitration_id=0x20, is_extended_id=False, data=b"\xBB"))
        assert _wait(lambda: ch.written == 2)
        pos, frames, skipped = ch.trace_since(0)
        assert pos == 2 and skipped == 0
        assert frames[0][1] == 0x10 and frames[0][2] & F_TX
        assert frames[1][1] == 0x20 and not frames[1][2] & F_TX
        got = other.recv(1.0)
        assert got.arbitration_id == 0x10                 # really went out on the bus
    finally:
        other.shutdown()


def test_tap_never_blocks_and_counts_drops(engine, channel):
    ch = engine.connect(ChannelConfig("virtual", channel))
    tap = ch.add_tap(Tap(maxlen=10))
    other = can.Bus(interface="virtual", channel=channel)
    try:
        for i in range(25):
            other.send(can.Message(arbitration_id=0x100 + i, is_extended_id=False, data=b""))
        assert _wait(lambda: ch.rx_frames == 25)
        frames = tap.drain()
        assert len(frames) == 10 and tap.dropped == 15
        assert frames[-1][1] == 0x100 + 24                # newest kept
    finally:
        other.shutdown()


def test_listen_only_refuses_transmit(engine, channel):
    engine.connect(ChannelConfig("virtual", channel, listen_only=True))
    try:
        engine.send(0, 0x10, b"")
        raise AssertionError("expected a refusal")
    except Exception as exc:
        assert "listen-only" in str(exc)


def test_bus_kwargs_per_backend():
    assert bus_kwargs(ChannelConfig("pcan", None, 500_000, listen_only=True))["state"] == can.BusState.PASSIVE
    k = bus_kwargs(ChannelConfig("kvaser", "1", 250_000, fd=True, data_bitrate=5_000_000, listen_only=True))
    assert k["channel"] == 1 and k["driver_mode"] is False
    assert isinstance(k["timing"], can.BitTimingFd) and k["timing"].data_bitrate == 5_000_000
    k = bus_kwargs(ChannelConfig("slcan", "COM7", 500_000, fd=True, data_bitrate=5_000_000))
    assert isinstance(k["timing"], can.BitTimingFd) and "bitrate" not in k
    try:
        bus_kwargs(ChannelConfig("gs_usb", 0, 500_000, fd=True))
        raise AssertionError("gs_usb FD should be refused")
    except ValueError:
        pass


def test_bitrates_are_never_silently_replaced():
    # python-can's PCAN backend would open an unknown rate at 500k: map 83.333k, refuse the rest
    assert bus_kwargs(ChannelConfig("pcan", None, 83_333))["bitrate"] == 83_000
    assert bus_kwargs(ChannelConfig("slcan", "COM3", 83_333))["bitrate"] == 83_300
    for cfg in (ChannelConfig("pcan", None, 62_500), ChannelConfig("slcan", "COM3", 800_000)):
        try:
            bus_kwargs(cfg)
            raise AssertionError(cfg)
        except ValueError as exc:
            assert "cannot run at" in str(exc)
    k = bus_kwargs(ChannelConfig("kvaser", 0, 800_000))           # not a canlib preset: explicit timing
    assert "bitrate" not in k and k["timing"].bitrate == 800_000
    assert bus_kwargs(ChannelConfig("kvaser", 0, 250_000))["bitrate"] == 250_000
    assert bus_kwargs(ChannelConfig("vector", "2", 500_000))["app_name"] is None


def test_clear_restarts_the_table_and_bumps_generation(engine, channel):
    ch = engine.connect(ChannelConfig("virtual", channel))
    other = can.Bus(interface="virtual", channel=channel)
    try:
        for i in range(5):
            other.send(can.Message(arbitration_id=0x100 + i, is_extended_id=False, data=b""))
        assert _wait(lambda: ch.rx_frames == 5)
        seq, _ = ch.rows_snapshot()
        gen = ch.generation
        ch.clear()
        other.send(can.Message(arbitration_id=0x200, is_extended_id=False, data=b""))
        assert _wait(lambda: ch.rx_frames == 1)
        _, rows = ch.rows_snapshot(seq)
        assert [r[1] for r in rows] == [0x200] and ch.generation == gen + 1
    finally:
        other.shutdown()


def test_request_enters_the_trace_before_its_reply(engine, channel):
    """A device answering instantly must never appear before the frame it answers."""
    ch = engine.connect(ChannelConfig("virtual", channel))
    echo = can.Bus(interface="virtual", channel=channel)
    stop = []
    import threading

    def responder():
        while not stop:
            m = echo.recv(0.05)
            if m is not None:
                echo.send(can.Message(arbitration_id=m.arbitration_id + 1, is_extended_id=False, data=b""))
    t = threading.Thread(target=responder, daemon=True)
    t.start()
    try:
        for i in range(200):
            engine.send(0, 0x100 + 2 * (i % 50), b"")
        assert _wait(lambda: ch.rx_frames == 200)
        _, frames, _ = ch.trace_since(0)
        sent = {}                                     # request id -> time of its latest send
        for fr in frames:
            if fr[2] & F_TX:
                sent[fr[1]] = fr[0]
            else:                                     # a reply comes after its request, in order and in time
                assert fr[1] - 1 in sent and fr[0] >= sent[fr[1] - 1]
    finally:
        stop.append(1)
        t.join(1)
        echo.shutdown()


def test_a_failing_tap_is_removed_and_receive_continues(engine, channel):
    ch = engine.connect(ChannelConfig("virtual", channel))
    ch.add_tap(Tap(accept=lambda cid, fl: 1 / 0))
    other = can.Bus(interface="virtual", channel=channel)
    try:
        for i in range(5):
            other.send(can.Message(arbitration_id=0x100 + i, is_extended_id=False, data=b""))
        assert _wait(lambda: ch.rx_frames == 5)
        assert ch.taps == [] and "plugin" in ch.error
    finally:
        other.shutdown()
