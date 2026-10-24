"""GVRET bus for python-can: ESP32RET, EVTV, M2RET and other SavvyCAN-style serial or WiFi adapters.

python-can has no GVRET backend, so this is a small one. It speaks the binary GVRET protocol (classic CAN only)
over a serial port or a pyserial URL such as socket://192.168.4.1:23, which is where ESP32RET listens over WiFi.
Everything is little-endian. A packet is 0xF1, a command byte, then a payload whose length depends on the command.
"""

from __future__ import annotations

import collections
import logging
import struct
import threading
import time
from typing import Optional

import can
import serial

log = logging.getLogger(__name__)

_SOF = 0xF1
_BINARY = b"\xE7\xE7"                          # each E7 switches the device to binary mode
_CMD_FRAME, _CMD_TIME, _CMD_SETUP, _CMD_PARAMS, _CMD_INFO, _CMD_KEEPALIVE = 0x00, 0x01, 0x05, 0x06, 0x07, 0x09
_CMD_NUM_BUSES, _CMD_EXT_BUSES, _CMD_FD_FRAME = 0x0C, 0x0D, 0x14
# Payload bytes of the replies we only have to skip (or, for 07 and 0C, read) to stay in sync.
_REPLY_LEN = {_CMD_TIME: 4, _CMD_PARAMS: 10, _CMD_INFO: 6, _CMD_KEEPALIVE: 2, _CMD_NUM_BUSES: 1, _CMD_EXT_BUSES: 15}
_EXT_FLAG = 0x80000000
_POLL = 0.02                                    # port read timeout; set once (on Windows a change reconfigures the port)
_FLAGS_PRESENT = _BUS_DISABLED = 0x80000000     # a setup value with only this bit set is a disabled bus
_FLAG_ENABLED, _FLAG_LISTEN_ONLY = 0x40000000, 0x20000000


class GvretBus(can.BusABC):
    """One CAN bus of a GVRET device (classic CAN only; no remote frames, no FD)."""

    def __init__(self, channel, bitrate: int = 500_000, listen_only: bool = False, can_bus: int = 0,
                 tty_baudrate: int = 1_000_000, timeout_open: float = 3.0, **kwargs):
        if not 1 <= int(bitrate) <= 1_000_000:
            raise ValueError(f"GVRET bit rate must be 1..1000000 bit/s, not {bitrate}")
        if can_bus not in (0, 1):
            raise ValueError(f"GVRET can_bus must be 0 or 1, not {can_bus}")
        self.channel_info = f"GVRET on {channel}"
        super().__init__(channel, **kwargs)
        self._bitrate, self._listen_only, self._bus = int(bitrate), bool(listen_only), can_bus
        self._wlock = threading.Lock()              # recv only reads and send only writes, so only writes need it
        self._buf = bytearray()
        self._frames: collections.deque = collections.deque()
        self._replies: dict[int, bytes] = {}
        self.resyncs = 0                            # times data that was not a known packet had to be skipped
        self.fd_dropped = 0
        self.build: Optional[int] = None            # firmware build number, if the device told us
        self._ser = None
        try:
            self._open(channel, tty_baudrate, timeout_open)
        except BaseException:
            self._close(self._ser)                  # no disable command: the device or its other bus may be in use
            self._ser = None
            super().shutdown()
            raise

    # ------------------------------------------------------------------------------------------ open / close

    def _open(self, channel, tty_baudrate, timeout_open):
        try:
            ser = serial.serial_for_url(channel, do_not_open=True, baudrate=tty_baudrate, timeout=_POLL)
            for line in ("dtr", "rts"):             # ESP32 boards reset or enter the boot loader if these are asserted
                try:
                    setattr(ser, line, False)
                except (AttributeError, NotImplementedError, serial.SerialException, OSError):
                    pass                            # e.g. socket:// has no modem lines
            ser.open()
        except (serial.SerialException, OSError, ValueError) as exc:
            raise can.CanInitializationError(f"cannot open {channel}: {exc}") from exc
        self._ser = ser
        try:
            n_buses = self._handshake(timeout_open)
        except (serial.SerialException, OSError, can.CanOperationError) as exc:
            raise can.CanInitializationError(f"lost {channel} during GVRET setup: {exc}") from exc
        if n_buses is None:
            raise can.CanInitializationError(
                f"no GVRET device answered on {channel}; check the port, the firmware, and for USB-UART boards "
                f"the baud rate (default {tty_baudrate})")
        if self._bus >= n_buses:
            raise can.CanInitializationError(f"the GVRET device has {n_buses} CAN bus(es); can_bus={self._bus} does not exist")
        self._n_buses = n_buses
        flags = _FLAGS_PRESENT | _FLAG_ENABLED | (_FLAG_LISTEN_ONLY if self._listen_only else 0)
        try:
            self._write_setup(self._bitrate | flags)
        except (serial.SerialException, OSError) as exc:
            raise can.CanInitializationError(f"lost {channel} during GVRET setup: {exc}") from exc
        self._frames.clear()                        # anything received so far is from before the setup

    def _handshake(self, timeout: float) -> Optional[int]:
        """Switch to binary mode and ask for the bus count; None if nothing answers within `timeout`."""
        end = time.monotonic() + timeout
        while True:
            # device info first, so its reply is already in hand when the bus count arrives
            self._write(_BINARY + bytes([_SOF, _CMD_INFO, _SOF, _CMD_NUM_BUSES]))   # a rebooting board needs repeats
            retry = time.monotonic() + 0.5
            while time.monotonic() < min(retry, end):
                self._fill()
                self._parse()
                if _CMD_NUM_BUSES in self._replies:
                    info = self._replies.get(_CMD_INFO)
                    if info:
                        self.build = int.from_bytes(info[:2], "little")
                    return self._replies[_CMD_NUM_BUSES][0]
            if time.monotonic() >= end:
                return None

    def _write_setup(self, wanted: int, ser=None) -> None:
        values = [_BUS_DISABLED, _BUS_DISABLED]
        values[self._bus] = wanted
        self._write(struct.pack("<BBII", _SOF, _CMD_SETUP, *values), ser)

    @staticmethod
    def _close(ser) -> None:
        try:
            if ser is not None:
                ser.close()
        except Exception:
            pass

    def shutdown(self) -> None:
        super().shutdown()
        ser, self._ser = self._ser, None
        if ser is None:
            return
        try:
            self._write_setup(_BUS_DISABLED, ser)
        except Exception:                           # the device may already be gone
            pass
        self._close(ser)

    # ------------------------------------------------------------------------------------------ receive

    def _fill(self) -> None:
        """Wait up to _POLL for a byte, then take everything that is waiting."""
        try:
            data = self._ser.read(1)
            if data:
                data += self._ser.read(self._ser.in_waiting)
        except (serial.SerialException, OSError) as exc:
            raise can.CanOperationError(f"GVRET read failed: {exc}") from exc
        self._buf += data

    def _parse(self) -> None:
        """Turn whole packets in the buffer into frames and replies; skip anything else."""
        buf = self._buf
        while buf:
            if buf[0] != _SOF:                      # console text, or the tail of a damaged packet
                nxt = buf.find(_SOF)
                del buf[:nxt if nxt >= 0 else len(buf)]
                self.resyncs += 1
                continue
            if len(buf) < 2:
                return
            cmd = buf[1]
            if cmd in (_CMD_FRAME, _CMD_FD_FRAME):
                if len(buf) < 11:
                    return
                n = (buf[10] & 0x0F) if cmd == _CMD_FRAME else buf[10]
                if n > (8 if cmd == _CMD_FRAME else 64):    # not a real length: this F1 was inside garbage
                    del buf[:1]
                    self.resyncs += 1
                    continue
                total = 12 + n if cmd == _CMD_FRAME else 13 + n    # FD has a separate bus byte
                if len(buf) < total:
                    return
                if cmd == _CMD_FRAME and not self._plausible(buf):     # an F1 00 inside garbage
                    del buf[:1]
                    self.resyncs += 1
                    continue
                if cmd == _CMD_FRAME:
                    self._add_frame(bytes(buf[:total]))
                else:
                    if not self.fd_dropped:
                        log.debug("GVRET: CAN FD frame dropped (classic CAN only)")
                    self.fd_dropped += 1
            elif cmd in _REPLY_LEN:
                total = 2 + _REPLY_LEN[cmd]
                if len(buf) < total:
                    return
                self._replies[cmd] = bytes(buf[2:total])
            else:                                   # unknown command: drop the F1 and look for the next one
                del buf[:1]
                self.resyncs += 1
                continue
            del buf[:total]

    def _plausible(self, buf: bytearray) -> bool:
        raw_id = int.from_bytes(buf[6:10], "little")
        limit = 0x1FFFFFFF if raw_id & _EXT_FLAG else 0x7FF
        return (raw_id & 0x7FFFFFFF) <= limit and buf[10] >> 4 < getattr(self, "_n_buses", 2)

    def _add_frame(self, pkt: bytes) -> None:
        # The device timestamp is a microsecond counter that wraps every ~71 minutes, so use the host clock.
        _, raw_id, info = struct.unpack_from("<IIB", pkt, 2)
        n = info & 0x0F
        if info >> 4 != self._bus:
            return
        self._frames.append(can.Message(
            arbitration_id=raw_id & 0x7FFFFFFF, is_extended_id=bool(raw_id & _EXT_FLAG), dlc=n,
            data=pkt[11:11 + n], channel=self._bus, timestamp=time.time()))

    def _recv_internal(self, timeout: Optional[float]):
        end = None if timeout is None else time.monotonic() + timeout
        while not self._frames:
            self._fill()
            self._parse()
            if end is not None and time.monotonic() >= end:
                break
        return (self._frames.popleft() if self._frames else None), False

    # ------------------------------------------------------------------------------------------ send

    def _write(self, data: bytes, ser=None) -> None:
        with self._wlock:
            (ser or self._ser).write(data)

    def send(self, msg: can.Message, timeout: Optional[float] = None) -> None:
        if self._listen_only:
            raise can.CanOperationError("GVRET bus is open listen-only; it cannot send")
        if msg.is_remote_frame:
            raise can.CanOperationError("GVRET has no remote frames")
        if msg.is_fd or msg.dlc > 8:
            raise can.CanOperationError("GVRET supports classic CAN only (up to 8 data bytes)")
        ident = msg.arbitration_id | (_EXT_FLAG if msg.is_extended_id else 0)
        data = bytes(msg.data[:msg.dlc])
        try:
            self._write(struct.pack("<BBIBB", _SOF, _CMD_FRAME, ident, self._bus, len(data)) + data + b"\x00")
        except (serial.SerialException, OSError) as exc:
            raise can.CanOperationError(f"GVRET write failed: {exc}") from exc
