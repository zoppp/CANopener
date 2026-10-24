"""An adapter that is pulled and plugged back in: the channel notices and reopens it by itself."""
import time

import can

from canopener.core import channel as chmod
from canopener.core.adapters import ChannelConfig
from canopener.core.channel import Channel


class SilentBus(can.BusABC):
    """Like python-can's gs_usb: never raises, just goes quiet when 'unplugged'."""
    def __init__(self):
        super().__init__(channel="x")
        self.shut = False

    def _recv_internal(self, timeout):
        time.sleep(min(timeout or 0.01, 0.01))
        return None, False

    def send(self, msg, timeout=None):
        pass

    def shutdown(self):
        self.shut = True
        super().shutdown()


def test_unplug_and_replug(monkeypatch):
    present = {"v": True}
    buses = []
    monkeypatch.setattr(chmod, "QUIET_PROBE_S", 0.1)
    monkeypatch.setattr(chmod, "RETRY_S", 0.05)
    monkeypatch.setattr(chmod, "device_present", lambda cfg: present["v"])

    def fake_open(cfg):
        b = SilentBus()
        buses.append(b)
        return b
    monkeypatch.setattr(chmod, "open_bus", fake_open)
    ch = Channel(0, ChannelConfig(interface="gs_usb", channel=0)).open()
    try:
        assert ch.state == "open"
        present["v"] = False
        deadline = time.time() + 3
        while ch.state != "error" and time.time() < deadline:
            time.sleep(0.02)
        assert ch.state == "error" and "unplugged" in ch.error and buses[0].shut
        present["v"] = True
        deadline = time.time() + 3
        while ch.state != "open" and time.time() < deadline:
            time.sleep(0.02)
        assert ch.state == "open" and ch.reconnects == 1 and len(buses) == 2
    finally:
        ch.close()
    assert ch.state == "closed" and buses[1].shut
