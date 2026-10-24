"""OBDLink backend against a fake STN dongle that answers like the FRPM describes (no hardware)."""

import threading
import time

import can
import pytest
import serial

from canopener.core import obdlink
from canopener.core.adapters import ChannelConfig, bus_kwargs, open_bus
from canopener.core.obdlink import ObdlinkBus, parse_frame


class FakeStn:
    """Command mode: lines up to CR, answered with a reply and '>'. Monitor mode: any byte stops it."""

    def __init__(self, ident="STN1155 v4.3.0", silent=False, rate_offset=0):
        self.ident, self.silent, self.rate_offset = ident, silent, rate_offset
        self.commands, self.monitoring, self.rate = [], False, 500_000
        self._out, self._line = bytearray(), bytearray()
        self._cv = threading.Condition()

    # pyserial surface
    @property
    def in_waiting(self):
        with self._cv:
            return len(self._out)

    def read(self, n=1):
        with self._cv:
            if not self._out:
                self._cv.wait(0.05)
            data = bytes(self._out[:n])
            del self._out[:n]
            return data

    def write(self, data):
        for b in data:
            if self.monitoring:
                self.monitoring = False             # the stopping character is discarded
                self.commands.append("<stop>")
                self.feed(b"STOPPED\r\r>")
                continue
            if b == 0x0D:
                self._command(self._line.decode().strip())
                self._line.clear()
            else:
                self._line.append(b)
        return len(data)

    def close(self):
        pass

    # test side
    def feed(self, data: bytes):
        with self._cv:
            self._out += data
            self._cv.notify_all()

    def _command(self, cmd):
        if self.silent:
            return
        self.commands.append(cmd)
        if cmd == "STM":
            self.monitoring = True
            return
        if cmd == "ATWS":
            reply = "ELM327 v1.4b"
        elif cmd == "STI":
            reply = self.ident
        elif cmd.startswith("STPBRR"):
            reply = str(self.rate + self.rate_offset)
        elif cmd.startswith("STPBR"):
            self.rate, reply = int(cmd.split()[1]), "OK"
        elif cmd.startswith("STPX"):
            reply = ""
        else:
            reply = "OK"
        self.feed((reply + "\r\r>").encode() if reply else b"\r>")


@pytest.fixture
def fake(monkeypatch):
    dev = FakeStn()
    monkeypatch.setattr(serial, "serial_for_url", lambda url, **kw: dev)
    return dev


def _wait(cond, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


SETUP = ["ATWS", "ATE0", "ATL0", "ATS1", "ATH1", "ATD1", "ATCAF0", "STI"]


def test_parse_frame():
    m = parse_frame("7E8 8 01 02 03 04 05 06 07 08")
    assert (m.arbitration_id, m.is_extended_id, m.dlc, bytes(m.data)) == (0x7E8, False, 8, bytes(range(1, 9)))
    m = parse_frame("18 DA F1 10 3 02 01 0C")
    assert (m.arbitration_id, m.is_extended_id, bytes(m.data)) == (0x18DAF110, True, b"\x02\x01\x0c")
    assert parse_frame("123 0").dlc == 0
    m = parse_frame("7E8 2 RTR")
    assert m.is_remote_frame and m.dlc == 2
    for line in ("OK", "STOPPED", "500000", "?", "ELM327 v1.4b", "STN1155 v4.3.0", "BUFFER FULL",
                 "7E8 8 01 02", "7E8 2 01 02 <DATA ERROR", "7E8 9 01 02 03 04 05 06 07 08 09", "CAN ERROR"):
        assert parse_frame(line) is None, line


def test_setup_sequence_and_receive(fake):
    bus = ObdlinkBus("COM9", bitrate=250_000)
    try:
        assert fake.commands == SETUP + ["STP 31", "STPBR 250000", "STCMM 1", "STFAC", "STFPA 00,00", "STPBRR", "STM"]
        assert bus.device == "STN1155 v4.3.0"
        fake.feed(b"7E8 3 01 02 03\r18 DA F1 10 1 AA\r")
        a, b = bus.recv(1.0), bus.recv(1.0)
        assert (a.arbitration_id, bytes(a.data), a.is_extended_id) == (0x7E8, b"\x01\x02\x03", False)
        assert (b.arbitration_id, b.is_extended_id) == (0x18DAF110, True)
        fake.feed(b"7E8 2 01 02 <RX ERROR\r")
        assert bus.recv(0.1) is None and _wait(lambda: bus.rx_errors == 1)
    finally:
        bus.shutdown()
    assert fake.commands[-1] == "<stop>"


def test_listen_only_and_pins(fake):
    bus = ObdlinkBus("COM9", bitrate=125_000, listen_only=True, can_pins="ms")
    try:
        assert "STP 51" in fake.commands and "STCMM 0" in fake.commands
        with pytest.raises(can.CanOperationError):
            bus.send(can.Message(arbitration_id=0x10, is_extended_id=False, data=b"\x01"))
    finally:
        bus.shutdown()
    with pytest.raises(ValueError):
        ObdlinkBus("COM9", can_pins="xx")


def test_send_stops_monitor_and_switches_preset(fake):
    bus = ObdlinkBus("COM9", bitrate=500_000)
    try:
        start = len(fake.commands)
        bus.send(can.Message(arbitration_id=0x123, is_extended_id=False, data=b"\x01\x02"))
        assert fake.commands[start:] == ["<stop>", "STPX h:123, d:0102, t:20", "STM"]
        start = len(fake.commands)
        bus.send(can.Message(arbitration_id=0x18DA10F1, is_extended_id=True, data=b""))
        assert fake.commands[start:] == ["<stop>", "STP 32", "STPBR 500000", "STCMM 1", "STFAC", "STFPA 00,00",
                                         "STPX h:18DA10F1, d:, t:20", "STM"]
        with pytest.raises(can.CanOperationError):
            bus.send(can.Message(arbitration_id=0x1, is_extended_id=False, is_remote_frame=True, dlc=1))
    finally:
        bus.shutdown()


def test_send_error_reply_raises_and_monitor_resumes(fake):
    bus = ObdlinkBus("COM9")
    try:
        orig = fake._command
        fake._command = lambda cmd: (fake.commands.append(cmd), fake.feed(b"CAN ERROR\r\r>")) \
            if cmd.startswith("STPX") else orig(cmd)
        with pytest.raises(can.CanOperationError, match="CAN ERROR"):
            bus.send(can.Message(arbitration_id=0x123, is_extended_id=False, data=b"\x01"))
        assert fake.commands[-1] == "STM" and fake.monitoring
    finally:
        bus.shutdown()


def test_monitor_restarts_after_buffer_full(fake):
    bus = ObdlinkBus("COM9")
    try:
        fake.monitoring = False
        fake.feed(b"BUFFER FULL\r\r>")
        assert _wait(lambda: bus.overruns == 1)
        bus.recv(0.05)
        assert fake.commands[-1] == "STM" and fake.monitoring
        fake.feed(b"100 1 FF\r")
        assert bus.recv(1.0).arbitration_id == 0x100
    finally:
        bus.shutdown()


def test_open_errors(monkeypatch):
    for dev, match in ((FakeStn(silent=True), "no OBDLink answered"), (FakeStn(ident="?"), "not an OBDLink"),
                       (FakeStn(rate_offset=20_000), "cannot run at")):
        monkeypatch.setattr(serial, "serial_for_url", lambda url, _d=dev, **kw: _d)
        t0 = time.monotonic()
        with pytest.raises(can.CanInitializationError, match=match):
            ObdlinkBus("COM9", timeout_open=0.3)
        assert time.monotonic() - t0 < 3


def test_adapters_open_obdlink(fake):
    kw = bus_kwargs(ChannelConfig("obdlink", "COM9", 250_000, listen_only=True))
    assert kw == {"interface": "obdlink", "channel": "COM9", "bitrate": 250_000, "listen_only": True}
    with pytest.raises(ValueError):
        bus_kwargs(ChannelConfig("obdlink", "COM9", 500_000, fd=True))
    bus = open_bus(ChannelConfig("obdlink", "COM9", 250_000))
    try:
        assert isinstance(bus, obdlink.ObdlinkBus) and "STPBR 250000" in fake.commands
    finally:
        bus.shutdown()


def test_channel_suffix_selects_port_or_pins():
    kw = bus_kwargs(ChannelConfig("obdlink", "COM7@ms", 125_000))
    assert kw["channel"] == "COM7" and kw["can_pins"] == "ms"
    kw = bus_kwargs(ChannelConfig("gvret", "socket://192.168.4.1:23@1", 500_000))
    assert kw["channel"] == "socket://192.168.4.1:23" and kw["can_bus"] == 1
    assert "can_bus" not in bus_kwargs(ChannelConfig("gvret", "COM6", 500_000))
    with pytest.raises(ValueError):
        bus_kwargs(ChannelConfig("gvret", "COM6@2", 500_000))


def test_send_reply_is_received(fake):
    bus = ObdlinkBus("COM9")
    try:
        orig = fake._command
        fake._command = lambda cmd: (fake.commands.append(cmd), fake.feed(b"581 8 43 00 10 00 01 00 00 00\r\r>")) \
            if cmd.startswith("STPX") else orig(cmd)
        bus.send(can.Message(arbitration_id=0x601, is_extended_id=False, data=b"\x40\x00\x10\x00\x00\x00\x00\x00"))
        assert bus.recv(1.0).arbitration_id == 0x581     # the answer that came while STPX listened
    finally:
        bus.shutdown()


def test_reset_while_monitoring_sets_up_again(fake):
    bus = ObdlinkBus("COM9", bitrate=250_000)
    try:
        start = len(fake.commands)
        fake.monitoring = False
        fake.feed(b"LV RESET\r\rELM327 v1.4b\r\r>")
        assert _wait(lambda: bus.overruns == 1)
        assert _wait(lambda: (bus.recv(0.05), fake.monitoring)[1], 3.0)
        assert fake.commands[start:] == ["ATE0", "ATL0", "ATS1", "ATH1", "ATD1", "ATCAF0", "STP 31",
                                         "STPBR 250000", "STCMM 1", "STFAC", "STFPA 00,00", "STM"]
    finally:
        bus.shutdown()
