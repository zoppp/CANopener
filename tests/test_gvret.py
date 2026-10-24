"""GVRET bus: handshake, setup bytes, frame parsing and resync, and sending, against a fake serial device."""

import struct
import time

import can
import pytest
import serial

from canopener.core.gvret import GvretBus

SETUP = b"\xF1\x05"
DISABLED = 0x80000000


class FakeSerial:
    """Answers like ESP32RET: bus count (and device info) on request, records everything else the host writes."""

    def __init__(self, n_buses=2, responds=True):
        self.n_buses, self.responds = n_buses, responds
        self.rx = bytearray()                       # what the host will read next
        self.written = bytearray()
        self.setups: list[tuple[int, int]] = []
        self.events: list[tuple] = []
        self.timeout = 0.02                         # what GvretBus opens with (_POLL)
        self.is_open = False
        self.closed = False

    dtr = property(lambda s: None, lambda s, v: s.events.append(("dtr", v, s.is_open)))
    rts = property(lambda s: None, lambda s, v: s.events.append(("rts", v, s.is_open)))

    def open(self):
        self.events.append(("open",))
        self.is_open = True

    def close(self):
        self.closed, self.is_open = True, False

    @property
    def in_waiting(self):
        return len(self.rx)

    def read(self, n=1):
        end = time.monotonic() + (self.timeout if self.timeout is not None else 1.0)
        while not self.rx and time.monotonic() < end:
            time.sleep(0.001)
        out = bytes(self.rx[:n])
        del self.rx[:n]
        return out

    def write(self, data):
        self.written += data
        d, i = bytes(data), 0
        while i < len(d):
            if d[i] != 0xF1:
                i += 1
            elif d[i + 1] == 0x05:
                self.setups.append(struct.unpack_from("<II", d, i + 2))
                i += 10
            elif d[i + 1] == 0x00:
                i += 9 + d[i + 7]                  # F1 00, id, bus, len, data, checksum
            else:
                if self.responds and d[i + 1] == 0x0C:
                    self.rx += bytes([0xF1, 0x0C, self.n_buses])
                elif self.responds and d[i + 1] == 0x07:
                    self.rx += bytes([0xF1, 0x07, 0x2A, 0x01, 0, 0, 0, 0])      # build 298
                i += 2


def rx_frame(ident, data=b"", bus=0, ext=False):
    return (b"\xF1\x00" + struct.pack("<II", 123456, ident | (0x80000000 if ext else 0))
            + bytes([len(data) | bus << 4]) + data + b"\x00")


@pytest.fixture
def make(monkeypatch):
    opened = []

    def factory(fake=None, **kw):
        fake = fake or FakeSerial()
        calls = []

        def serial_for_url(url, **kwargs):
            calls.append((url, kwargs))
            return fake
        monkeypatch.setattr(serial, "serial_for_url", serial_for_url)
        bus = GvretBus("COM9", **kw)
        opened.append(bus)
        fake.calls = calls
        return bus, fake

    yield factory
    for b in opened:
        b.shutdown()


def test_handshake_and_setup_bytes(make):
    bus, fake = make(bitrate=250_000)
    assert fake.written.startswith(b"\xE7\xE7\xF1\x07\xF1\x0C")
    assert bus.build == 298 and bus.channel_info == "GVRET on COM9"
    assert fake.setups == [(250_000 | 0xC0000000, DISABLED)]
    assert fake.calls[0][1]["baudrate"] == 1_000_000 and fake.calls[0][1]["do_not_open"]


def test_setup_listen_only_and_bus_1(make):
    _, fake = make(bitrate=500_000, listen_only=True)
    assert fake.setups == [(500_000 | 0xE0000000, DISABLED)]
    _, fake = make(bitrate=500_000, can_bus=1)
    assert fake.setups == [(DISABLED, 500_000 | 0xC0000000)]
    _, fake = make(bitrate=125_000, listen_only=True, can_bus=1)
    assert fake.setups == [(DISABLED, 125_000 | 0xE0000000)]


def test_dtr_rts_cleared_before_open(make):
    _, fake = make()
    names = [e[0] for e in fake.events]
    assert names.index("open") > max(names.index("dtr"), names.index("rts"))
    assert ("dtr", False, False) in fake.events and ("rts", False, False) in fake.events


def test_no_reply_fails_within_timeout(monkeypatch):
    fake = FakeSerial(responds=False)
    monkeypatch.setattr(serial, "serial_for_url", lambda *a, **k: fake)
    t0 = time.monotonic()
    with pytest.raises(can.CanInitializationError, match="no GVRET device answered on COM9"):
        GvretBus("COM9", timeout_open=0.7)
    assert 0.6 < time.monotonic() - t0 < 2.0
    assert fake.closed and not fake.setups
    assert fake.written.count(b"\xE7\xE7") >= 2        # resent while waiting


def test_missing_bus_and_bad_arguments(monkeypatch):
    fake = FakeSerial(n_buses=1)
    monkeypatch.setattr(serial, "serial_for_url", lambda *a, **k: fake)
    with pytest.raises(can.CanInitializationError, match="can_bus=1"):
        GvretBus("COM9", can_bus=1)
    assert fake.closed and not fake.setups             # nothing is changed on a device we do not use
    for kw in ({"bitrate": 0}, {"bitrate": 1_000_001}, {"can_bus": 2}):
        with pytest.raises(ValueError):
            GvretBus("COM9", **kw)


def test_standard_extended_and_empty_frames(make):
    bus, fake = make()
    fake.rx += rx_frame(0x123, b"\x01\x02\x03") + rx_frame(0x18FEF100, b"\xAA" * 8, ext=True) + rx_frame(0x7FF)
    a, b, c = (bus.recv(1.0) for _ in range(3))        # all three came in one read
    assert (a.arbitration_id, a.is_extended_id, a.dlc, bytes(a.data)) == (0x123, False, 3, b"\x01\x02\x03")
    assert (b.arbitration_id, b.is_extended_id, b.dlc, bytes(b.data)) == (0x18FEF100, True, 8, b"\xAA" * 8)
    assert (c.arbitration_id, c.dlc, bytes(c.data)) == (0x7FF, 0, b"")
    assert a.channel == 0 and abs(a.timestamp - time.time()) < 5
    assert bus.recv(0.05) is None


def test_frame_split_across_reads(make):
    bus, fake = make()
    pkt = rx_frame(0x321, b"\x10\x20\x30\x40")
    fake.rx += pkt[:7]
    assert bus.recv(0.05) is None
    fake.rx += pkt[7:]
    assert bytes(bus.recv(1.0).data) == b"\x10\x20\x30\x40"


def test_other_bus_ignored(make):
    bus, fake = make(can_bus=1)
    fake.rx += rx_frame(0x100, b"\x01", bus=0) + rx_frame(0x200, b"\x02", bus=1)
    msg = bus.recv(1.0)
    assert msg.arbitration_id == 0x200 and msg.channel == 1
    assert bus.recv(0.05) is None


@pytest.mark.parametrize("junk", [b"ESP32RET v1.0\r\n", b"\x00\xFF\x42", b"\xF1\x7E", b"\xF1\xF1\xF1"])
def test_garbage_before_frame_resyncs(make, junk):
    bus, fake = make()
    fake.rx += junk + rx_frame(0x456, b"\x09")
    msg = bus.recv(1.0)
    assert msg.arbitration_id == 0x456 and bytes(msg.data) == b"\x09"
    assert bus.resyncs > 0


def test_replies_and_fd_skipped(make):
    bus, fake = make()
    timesync = b"\xF1\x01\x01\x02\x03\x04"
    keepalive = b"\xF1\x09\xDE\xAD"
    fd = b"\xF1\x14" + struct.pack("<II", 1, 0x555) + bytes([12, 0]) + bytes(range(12)) + b"\x00"
    fake.rx += timesync + keepalive + fd + rx_frame(0x11, b"\x01") + b"\xF1\x0D" + bytes(15) + rx_frame(0x12)
    assert bus.recv(1.0).arbitration_id == 0x11
    assert bus.recv(1.0).arbitration_id == 0x12
    assert bus.fd_dropped == 1 and bus.resyncs == 0


def test_send_exact_bytes(make):
    bus, fake = make(can_bus=1)
    fake.written.clear()
    bus.send(can.Message(arbitration_id=0x601, is_extended_id=False, data=b"\x40\x00\x10\x00"))
    bus.send(can.Message(arbitration_id=0x18FEF100, is_extended_id=True, data=b"\x01\x02"))
    bus.send(can.Message(arbitration_id=0x7FF, is_extended_id=False, data=b""))
    assert bytes(fake.written) == (
        b"\xF1\x00" + struct.pack("<I", 0x601) + b"\x01\x04" + b"\x40\x00\x10\x00" + b"\x00"
        + b"\xF1\x00" + struct.pack("<I", 0x18FEF100 | 0x80000000) + b"\x01\x02" + b"\x01\x02" + b"\x00"
        + b"\xF1\x00" + struct.pack("<I", 0x7FF) + b"\x01\x00" + b"\x00")


def test_send_refused(make):
    bus, _ = make()
    with pytest.raises(can.CanOperationError):
        bus.send(can.Message(arbitration_id=0x1, is_remote_frame=True, is_extended_id=False))
    with pytest.raises(can.CanOperationError):
        bus.send(can.Message(arbitration_id=0x1, is_fd=True, is_extended_id=False, data=b"\x00" * 12))
    listen, _ = make(listen_only=True)
    with pytest.raises(can.CanOperationError, match="listen-only"):
        listen.send(can.Message(arbitration_id=0x1, is_extended_id=False, data=b"\x00"))


def test_serial_errors_become_can_errors(make):
    bus, fake = make()

    def boom(*a):
        raise serial.SerialException("unplugged")
    fake.write = boom
    with pytest.raises(can.CanOperationError):
        bus.send(can.Message(arbitration_id=0x1, is_extended_id=False, data=b"\x00"))
    fake.read = boom
    with pytest.raises(can.CanOperationError):
        bus.recv(0.05)


def test_shutdown_disables_bus_and_closes(make):
    bus, fake = make()
    fake.written.clear()
    bus.shutdown()
    assert bytes(fake.written) == SETUP + struct.pack("<II", DISABLED, DISABLED)
    assert fake.closed
    bus.shutdown()                                      # safe to repeat
    assert bytes(fake.written).count(SETUP) == 1


def test_implausible_frame_header_is_skipped(make):
    bus, fake = make()
    fake.rx += rx_frame(0x900, b"\x01") + rx_frame(0x456, b"\x09", bus=5)[:-1] + rx_frame(0x123, b"\x02")
    msg = bus.recv(1.0)                                 # 0x900 is no 11-bit id, bus 5 does not exist
    assert msg.arbitration_id == 0x123 and bus.resyncs >= 2
