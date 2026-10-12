"""A full 1 Mbit/s bus is ~8k frames/s. The channel must take every frame, keep the receive table exact,
and a recording must contain every frame, while a reader polls snapshots at 10 Hz."""

import os
import threading
import time

import can

from canopener.core.adapters import ChannelConfig
from canopener.core.recorder import read_frames

N = 40_000


def test_channel_keeps_up_and_records_every_frame(engine, channel, tmp_path):
    ch = engine.connect(ChannelConfig("virtual", channel, bitrate=1_000_000))
    out = tmp_path / "load.blf"
    engine.start_recording(out)
    sender = can.Bus(interface="virtual", channel=channel)
    stop = threading.Event()
    polls = []

    def ui_poller():                      # what the server does at ~10 Hz
        pos = 0
        seq = 0
        while not stop.is_set():
            pos, frames, skipped = ch.trace_since(pos, limit=2000)
            seq, rows = ch.rows_snapshot(seq)
            ch.stats()
            polls.append(skipped)
            time.sleep(0.1)

    t = threading.Thread(target=ui_poller, daemon=True)
    t.start()
    try:
        t0 = time.perf_counter()
        for i in range(N):
            sender.send(can.Message(arbitration_id=0x100 + (i % 64), is_extended_id=False,
                                    data=i.to_bytes(4, "little") + b"\x00" * 4))
        deadline = time.monotonic() + 30
        while ch.rx_frames < N and time.monotonic() < deadline:
            time.sleep(0.01)
        elapsed = time.perf_counter() - t0
    finally:
        stop.set()
        t.join(2)
        sender.shutdown()
    assert ch.rx_frames == N
    rate = N / elapsed
    # a full 1 Mbit/s bus is ~8k frames/s; shared CI runners set CANOPENER_MIN_FPS lower
    assert rate > float(os.environ.get("CANOPENER_MIN_FPS", 8_000)), f"only {rate:.0f} frames/s"
    _, rows = ch.rows_snapshot()
    assert sum(r[6] for r in rows) == N and len(rows) == 64
    st = engine.stop_recording()
    assert st["written"] == N and not st["error"]
    assert sum(1 for _ in read_frames(out)) == N
