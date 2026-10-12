import time

import can
import pytest

from canopener.core.adapters import ChannelConfig
from canopener.core.channel import F_BRS, F_EXT, F_FD, F_RTR, F_TX
from canopener.core.recorder import Recorder, read_frames

FRAMES = [
    (1000.000, 0x123, 0, 8, bytes(range(8))),
    (1000.010, 0x1ABCDEF0, F_EXT, 2, b"\x01\x02"),
    (1000.020, 0x7FF, F_TX, 1, b"\xFF"),
    (1000.030, 0x200, F_FD | F_BRS, 12, bytes(range(12))),
]


@pytest.mark.parametrize("ext", [".trc", ".asc", ".blf", ".log", ".csv"])
def test_round_trip(tmp_path, ext):
    path = tmp_path / f"r{ext}"
    rec = Recorder(path, {0: 1})
    for fr in FRAMES:
        rec.put(0, fr)
    rec.stop()
    got = [fr for _, fr in read_frames(path)]
    assert [(f[1], f[4]) for f in got] == [(f[1], f[4]) for f in FRAMES]
    assert got[1][2] & F_EXT
    assert abs((got[3][0] - got[0][0]) - 0.030) < 0.002            # relative timing kept
    if ext in (".trc", ".asc", ".blf"):
        assert got[2][2] & F_TX                                      # direction kept
        assert got[3][2] & F_FD                                      # CAN FD kept


def test_trc_remote_frame_and_bus_number(tmp_path):
    path = tmp_path / "two.trc"
    rec = Recorder(path, {0: 1, 3: 2})
    rec.put(0, (5.0, 0x700, F_RTR, 1, b""))
    rec.put(3, (5.1, 0x181, 0, 2, b"\x01\x02"))
    rec.stop()
    text = path.read_text()
    assert ";$FILEVERSION=2.1" in text and " RR " in text
    got = list(read_frames(path))
    assert [b for b, _ in got] == [1, 2]
    assert got[0][1][2] & F_RTR


def test_canbin_record_and_read(tmp_path):
    path = tmp_path / "c.canbin"
    rec = Recorder(path, {0: 1}, bitrate=250_000)
    for fr in FRAMES[:3]:
        rec.put(0, fr)
    rec.stop()
    got = [fr for _, fr in read_frames(path)]
    assert [(f[1], f[4]) for f in got] == [(f[1], f[4]) for f in FRAMES[:3]]


def test_open_file_view_restores_cycle_times(engine, tmp_path):
    path = tmp_path / "v.trc"
    rec = Recorder(path, {0: 1})
    for i in range(10):
        rec.put(0, (100.0 + i * 0.1, 0x181, 0, 1, bytes([i])))
    rec.stop()
    (ch,) = engine.open_file_view(path)
    _, rows = ch.rows_snapshot()
    assert rows[0][6] == 10 and abs(rows[0][9] - 0.1) < 0.001
    assert ch.state == "file"


def test_replay_sends_with_timing(engine, channel, tmp_path):
    path = tmp_path / "p.trc"
    rec = Recorder(path, {0: 1})
    for i in range(5):
        rec.put(0, (10.0 + i * 0.05, 0x300 + i, 0, 1, bytes([i])))
    rec.stop()
    engine.connect(ChannelConfig("virtual", channel))
    sniff = can.Bus(interface="virtual", channel=channel)
    try:
        t0 = time.monotonic()
        p = engine.start_replay(path)
        got = [sniff.recv(2.0) for _ in range(5)]
        dt = time.monotonic() - t0
        assert [m.arbitration_id for m in got] == [0x300 + i for i in range(5)]
        assert 0.15 < dt < 1.0                                     # ~0.2 s of recorded timing
        time.sleep(0.05)
        assert p.status()["state"] == "done" and p.sent == 5
    finally:
        sniff.shutdown()
