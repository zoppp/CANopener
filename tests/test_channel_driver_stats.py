"""Driver stats (bus.state, Kvaser get_stats) are polled off the stats() path: a hung driver call never blocks it."""
import time

import can

from canopener.core import channel as chmod
from canopener.core.adapters import ChannelConfig
from canopener.core.channel import Channel


class StatsBus(can.BusABC):
    """recv is quiet; `state` and get_stats() sleep `delay` seconds, then answer."""
    def __init__(self, delay):
        super().__init__(channel="x")
        self.delay = delay

    @property
    def state(self):
        time.sleep(self.delay)
        return can.BusState.ACTIVE

    def get_stats(self):
        time.sleep(self.delay)
        return type("S", (), {"overruns": 3, "busLoad": 1234})()

    def _recv_internal(self, timeout):
        time.sleep(min(timeout or 0.01, 0.01))
        return None, False

    def send(self, msg, timeout=None):
        pass


def _channel(bus):
    return Channel(0, ChannelConfig(interface="virtual", channel="x"), bus=bus).open()


def test_hung_driver_never_blocks_stats_or_close(monkeypatch):
    monkeypatch.setattr(chmod, "DRIVER_STATS_S", 0.05)
    ch = _channel(StatsBus(3.0))
    try:
        time.sleep(0.2)                           # the poller is now inside the hung call
        t = time.time()
        for _ in range(5):
            out = ch.stats()
        assert time.time() - t < 0.2
        assert "bus_state" not in out and "hw_load" not in out
    finally:
        t = time.time()
        ch.close()
        assert time.time() - t < 1.0


def test_cached_values_appear_and_clear_on_bus_loss(monkeypatch):
    monkeypatch.setattr(chmod, "DRIVER_STATS_S", 0.05)
    ch = _channel(StatsBus(0.0))
    try:
        deadline = time.time() + 2
        while "hw_load" not in ch.stats() and time.time() < deadline:
            time.sleep(0.02)
        out = ch.stats()
        assert out["bus_state"] == "ACTIVE" and out["hw_overruns"] == 3 and out["hw_load"] == 12.34
        ch._lose_bus("test")                      # bus dropped: stale values must go
        assert "hw_load" not in ch.stats()
    finally:
        ch.close()


def test_poller_stops_on_close():
    ch = _channel(StatsBus(0.0))
    th = ch._poll_thread
    ch.close()
    th.join(1.0)
    assert not th.is_alive()
