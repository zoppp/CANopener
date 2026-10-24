"""OBDLink (STN11xx/STN21xx) OBD-II dongles as a python-can bus: OBDLink MX, MX+, LX, SX, EX.

The dongle speaks text: AT and ST commands, answered with lines and a '>' prompt. To receive, it runs STM
(monitor) on a raw ISO 11898 preset with a pass-all filter. It cannot send while monitoring, so each send stops
the monitor, sends with STPX (which prints the frames that arrive in the next _REPLY_MS, so a node's answer is
not lost) and starts the monitor again; other frames during that gap are not seen.
Command reference: OBDLink Family Reference and Programming Manual (FRPM), sections 7-9.

The port is anything pyserial opens: "COM7" (Bluetooth or USB) or "socket://192.168.0.10:35000" (MX WiFi).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Optional

import can

log = logging.getLogger(__name__)

# STP presets: ISO 11898 (raw CAN, variable DLC), (11-bit Tx, 29-bit Tx), per transceiver. Every preset
# receives both 11- and 29-bit frames; the Tx id size is fixed by the preset (FRPM 8.6).
_PRESETS = {"hs": (31, 32), "ms": (51, 52), "sw": (61, 62)}
_PROMPT = object()
_REPLY_MS = 20                                  # how long STPX listens for answers after sending
_ERRORS = ("?", "CAN ERROR", "BUS ERROR", "BUS BUSY", "FB ERROR", "DATA ERROR", "OUT OF MEMORY", "UNABLE TO CONNECT")


def parse_frame(line: str) -> Optional[can.Message]:
    """One monitor line, printed with headers, spaces and DLC on (ATH1 ATS1 ATD1), or None if it is not a frame.

    11-bit: "7E8 8 02 01 0C 00 00 00 00 00"; 29-bit: "18 DA F1 10 3 02 01 0C"; remote frame: "7E8 0 RTR"."""
    t = line.split()
    try:
        if len(t) >= 2 and len(t[0]) == 3:
            arb, ext, rest = int(t[0], 16), False, t[1:]
        elif len(t) >= 5 and all(len(x) == 2 for x in t[:4]):
            arb, ext, rest = int("".join(t[:4]), 16), True, t[4:]
        else:
            return None
        if len(rest[0]) != 1:
            return None
        dlc = int(rest[0], 16)
        if rest[1:] == ["RTR"]:
            return can.Message(arbitration_id=arb, is_extended_id=ext, is_remote_frame=True, dlc=dlc)
        data = bytes(int(x, 16) for x in rest[1:])
    except ValueError:
        return None
    if dlc > 8 or len(data) != dlc or any(len(x) != 2 for x in rest[1:]):
        return None
    return can.Message(arbitration_id=arb, is_extended_id=ext, dlc=dlc, data=data)


class ObdlinkBus(can.BusABC):
    def __init__(self, channel: Any, bitrate: int = 500_000, listen_only: bool = False, can_pins: str = "hs",
                 tty_baudrate: int = 115_200, timeout_open: float = 3.0, **kwargs):
        import serial
        if can_pins not in _PRESETS:
            raise ValueError(f"can_pins must be one of {', '.join(_PRESETS)} (HS-CAN pins 6/14, MS-CAN 3/11, "
                             "single-wire pin 1)")
        self.channel_info = f"OBDLink on {channel}"
        self._presets, self._bitrate, self._listen_only = _PRESETS[can_pins], int(bitrate), bool(listen_only)
        self._rx: queue.SimpleQueue = queue.SimpleQueue()
        self._replies: queue.SimpleQueue = queue.SimpleQueue()
        self._lock = threading.Lock()           # one command exchange at a time
        self._state = threading.Lock()          # _monitoring/_restart: the reader thread and senders both change them
        self._monitoring = False                # set before STM, cleared before stopping it
        self._restart = ""                      # why the monitor ended by itself ("BUFFER FULL", a reset, ...)
        self._can_pins, self._tx_ext = can_pins, False
        self._closed = threading.Event()
        self._error: Optional[str] = None
        self._last_line = ""
        self.rx_errors = 0                      # frames printed with an error marker, or unreadable
        self.overruns = 0                       # monitor stopped by the dongle and restarted
        self.device = ""
        try:
            self._ser = serial.serial_for_url(channel, baudrate=tty_baudrate, timeout=0.05, write_timeout=2)
        except (serial.SerialException, ValueError) as exc:
            raise can.CanInitializationError(f"cannot open {channel}: {exc}") from exc
        self._reader = threading.Thread(target=self._read_loop, name=f"obdlink-{channel}", daemon=True)
        self._reader.start()
        try:
            self._setup(channel, timeout_open)
        except can.CanOperationError as exc:     # a refused or unanswered command while opening
            self._close_port()
            raise can.CanInitializationError(str(exc)) from exc
        except Exception:
            self._close_port()
            raise
        super().__init__(channel=channel, **kwargs)

    # ------------------------------------------------------------------ serial side
    def _read_loop(self) -> None:
        buf = bytearray()
        while not self._closed.is_set():
            try:
                chunk = self._ser.read(max(1, self._ser.in_waiting))
            except Exception as exc:              # unplugged, Bluetooth link lost
                if not self._closed.is_set():
                    self._error = f"{type(exc).__name__}: {exc}"
                return
            for b in chunk:
                if b in (0x0D, 0x0A, 0x3E):          # CR, LF, '>'
                    if buf:
                        self._line(buf.decode("ascii", "replace").strip())
                        buf.clear()
                    if b == 0x3E:
                        self._prompt()
                elif b:                              # NUL bytes appear after some resets
                    buf.append(b)

    def _line(self, line: str) -> None:
        if not line:
            return
        msg = parse_frame(line)
        if msg is not None:
            msg.timestamp = time.time()          # the dongle prints no time stamps
            self._rx.put(msg)
        elif self._monitoring:                   # "7E8 8 01 02 <DATA ERROR", "BUFFER FULL", a reset banner
            self.rx_errors += "<" in line
            self._last_line = line
        else:
            self._replies.put(line)

    def _prompt(self) -> None:
        with self._state:
            if self._monitoring:                 # the dongle left monitor mode on its own
                self._monitoring, self._restart = False, self._last_line or "no reason"
                self.overruns += 1
                log.warning("%s: monitor stopped (%s), restarting", self.channel_info, self._restart)
                return
        self._replies.put(_PROMPT)

    def _command(self, cmd: str, timeout: float = 2.0) -> list[str]:
        """Send one command and return its reply lines (up to the prompt)."""
        while True:                              # drop leftovers from earlier exchanges
            try:
                self._replies.get_nowait()
            except queue.Empty:
                break
        self._write(cmd.encode("ascii") + b"\r")
        lines, end = [], time.monotonic() + timeout
        while True:
            try:
                item = self._replies.get(timeout=max(0.0, end - time.monotonic()))
            except queue.Empty:
                self._drain()                    # a late prompt must not be taken as the next command's
                raise can.CanOperationError(f"OBDLink did not answer {cmd!r} within {timeout} s") from None
            if item is _PROMPT:
                return lines
            lines.append(item)

    def _check(self, cmd: str, timeout: float = 2.0) -> list[str]:
        lines = self._command(cmd, timeout)
        bad = [x for x in lines if x.upper().startswith(_ERRORS)]
        if bad:
            raise can.CanOperationError(f"OBDLink refused {cmd!r}: {' / '.join(bad)}")
        return lines

    def _drain(self, quiet: float = 0.3, limit: float = 3.0) -> None:
        """Drop replies until the dongle has been quiet for `quiet` seconds."""
        end = time.monotonic() + limit
        while time.monotonic() < end:
            try:
                self._replies.get(timeout=quiet)
            except queue.Empty:
                return

    def _write(self, data: bytes) -> None:
        if self._error:
            raise can.CanOperationError(self._error)
        try:
            self._ser.write(data)
        except Exception as exc:
            raise can.CanOperationError(f"OBDLink write failed: {exc}") from exc

    # ------------------------------------------------------------------ dongle setup
    def _setup(self, channel: Any, timeout_open: float) -> None:
        self._write(b" ")                        # stops a monitor left running by an earlier session
        self._drain()
        try:
            self._command("ATWS", timeout_open)  # warm start: protocol, filters and formatting back to defaults
        except can.CanOperationError:
            raise can.CanInitializationError(
                f"no OBDLink answered on {channel}. Check the port (Bluetooth: pair first, then use the "
                "outgoing COM port) and that the dongle is plugged into a powered OBD port.") from None
        self._configure()
        ident = self._command("STI")
        if not ident or ident[0].startswith("?"):
            raise can.CanInitializationError(
                f"the dongle on {channel} is not an OBDLink/STN device (plain ELM327 clones are not supported)")
        self.device = ident[0]
        try:
            self._set_preset(False)
        except can.CanOperationError as exc:
            if self._can_pins == "hs":
                raise
            raise can.CanInitializationError(f"{exc}; this OBDLink model may have no "
                                             f"{'MS-CAN' if self._can_pins == 'ms' else 'single-wire CAN'}") from exc
        actual = self._command("STPBRR")
        try:
            rate = int(actual[0])
        except (IndexError, ValueError):
            rate = 0
        if not rate or abs(rate - self._bitrate) > self._bitrate * 0.01:
            raise can.CanInitializationError(f"OBDLink cannot run at {self._bitrate} bit/s (it set {actual})")
        self._start_monitor()
        log.info("%s: %s, %d bit/s%s", self.channel_info, self.device, rate,
                 ", listen-only" if self._listen_only else "")

    def _configure(self) -> None:
        for cmd in ("ATE0", "ATL0", "ATS1", "ATH1", "ATD1", "ATCAF0"):
            self._check(cmd)

    def _set_preset(self, ext: bool) -> None:
        """Select the preset for 11- or 29-bit sending; STP resets the rate, monitor mode and filters, so set
        them again."""
        self._check(f"STP {self._presets[1 if ext else 0]}")
        self._check(f"STPBR {self._bitrate}")
        self._check(f"STCMM {0 if self._listen_only else 1}")   # 0: receive only, no ACKs; 1: normal node
        self._check("STFAC")
        self._check("STFPA 00,00")               # pass everything (STM uses the filters as set)
        self._tx_ext = ext

    def _start_monitor(self) -> None:
        with self._state:
            self._monitoring, self._restart, self._last_line = True, "", ""
        self._write(b"STM\r")

    def _stop_monitor(self) -> None:
        with self._state:                        # atomic with _prompt: a self-stop and our stop never both count
            if not self._monitoring:
                return
            self._monitoring = False
        self._write(b" ")                        # any character stops it; a bare CR would repeat a command
        end = time.monotonic() + 1.0
        while time.monotonic() < end:            # wait for "STOPPED" and the prompt
            try:
                if self._replies.get(timeout=max(0.0, end - time.monotonic())) is _PROMPT:
                    return
            except queue.Empty:
                break
        raise can.CanOperationError("OBDLink did not stop monitoring")

    # ------------------------------------------------------------------ python-can
    def _recv_internal(self, timeout: Optional[float]) -> tuple[Optional[can.Message], bool]:
        if self._restart and self._lock.acquire(blocking=False):
            try:
                if self._restart:
                    self._resume()
            finally:
                self._lock.release()
        try:
            msg = self._rx.get(timeout=timeout) if timeout is None or timeout > 0 else self._rx.get_nowait()
        except queue.Empty:
            if self._error:
                raise can.CanOperationError(self._error) from None
            return None, False
        msg.channel = self.channel_info
        return msg, False

    def _resume(self) -> None:
        """Restart a monitor that ended by itself. After anything but BUFFER FULL (a brown-out reset while
        cranking prints its banner and a prompt) the dongle is back on defaults, so set it up again."""
        reason = self._restart
        self._restart = ""
        try:
            if "BUFFER FULL" not in reason.upper():
                self._drain()
                self._configure()
                self._set_preset(self._tx_ext)
            self._start_monitor()
        except can.CanError as exc:
            self._restart = reason               # try again on the next receive call
            log.warning("%s: restart failed: %s", self.channel_info, exc)

    def send(self, msg: can.Message, timeout: Optional[float] = None) -> None:
        if self._listen_only:
            raise can.CanOperationError("OBDLink opened listen-only: sending is off")
        if msg.is_remote_frame or msg.is_fd or msg.dlc > 8:
            raise can.CanOperationError("OBDLink sends classic data frames of up to 8 bytes only")
        ident = f"{msg.arbitration_id:08X}" if msg.is_extended_id else f"{msg.arbitration_id:03X}"
        with self._lock:
            self._stop_monitor()
            try:
                if msg.is_extended_id != self._tx_ext:
                    self._set_preset(msg.is_extended_id)
                # no response count: answers printed within t are frames and go to the receive queue
                self._check(f"STPX h:{ident}, d:{bytes(msg.data[:msg.dlc]).hex().upper()}, t:{_REPLY_MS}")
            finally:
                self._start_monitor()

    def shutdown(self) -> None:
        super().shutdown()
        if self._lock.acquire(timeout=2.0):
            try:
                self._stop_monitor()
            except can.CanError:
                pass
            finally:
                self._lock.release()
        self._close_port()

    def _close_port(self) -> None:
        self._closed.set()
        try:
            self._ser.close()
        except Exception:
            pass
        self._reader.join(1.0)
