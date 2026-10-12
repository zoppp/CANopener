"""Record to file (own writer thread, so the receive thread only appends to a queue) and read files back.

Formats, chosen by the file extension:
  .trc     PCAN-View v2.1 (FD, Tx/Rx, bus number)          own writer, python-can reader
  .asc     Vector ASCII                                      python-can
  .blf     Vector binary logging                             python-can
  .log     Linux candump -L                                  python-can
  .csv     python-can CSV                                    python-can
  .canbin  compact binary capture format for PLC loggers (classic CAN, one channel) own reader/writer
"""

from __future__ import annotations

import logging
import queue
import struct
import threading
import time
from pathlib import Path
from typing import Iterator, Optional

import can

from . import canbin
from .channel import F_ERR, F_EXT, F_RTR, F_TX, frame_to_message, msg_flags
from .trc import TrcWriter21

log = logging.getLogger(__name__)

RECORD_FORMATS = (".trc", ".asc", ".blf", ".log", ".csv", ".canbin")
READ_FORMATS = (".trc", ".asc", ".blf", ".log", ".csv", ".canbin")


class _CanbinStream:
    """Streaming .canbin writer: header first (count patched on close), then 20-byte frames."""

    def __init__(self, path, bitrate: int = 0, channel: int = 0):
        self._f = open(path, "wb")
        self._t0: Optional[float] = None
        self.count = 0
        self._bitrate = bitrate
        self._channel = channel
        self._f.write(b"\x00" * canbin.HEADER_SIZE)

    def write(self, bus: int, frame) -> None:
        ts, can_id, flags, dlc, data = frame
        if flags & F_ERR:
            return
        if self._t0 is None:
            self._t0 = ts
        us = int(round((ts - self._t0) * 1_000_000)) & 0xFFFFFFFF
        id_field = can_id | (canbin.ID_EXTENDED_BIT if flags & F_EXT else 0)
        fl = canbin.FLAG_RTR if flags & F_RTR else 0
        self._f.write(canbin._FRAME_STRUCT.pack(us, id_field, min(dlc, 8), fl, b"\x00\x00",
                                                bytes(data[:8]).ljust(8, b"\x00")))
        self.count += 1

    def flush(self) -> None:
        self._f.flush()

    def close(self) -> None:
        t0 = self._t0 or time.time()
        self._f.seek(0)
        self._f.write(canbin._HEADER_STRUCT.pack(canbin.MAGIC, self.count, self._bitrate, int(t0),
                                                 int(round((t0 - int(t0)) * 1_000_000)), self._channel))
        self._f.close()


class _PyCanStream:
    """Adapter from CANopener frames to a python-can writer (ASC, BLF, candump log, CSV)."""

    def __init__(self, writer):
        self._w = writer
        self.count = 0

    def write(self, bus: int, frame) -> None:
        ts, can_id, flags, dlc, data = frame
        self._w.on_message_received(frame_to_message(can_id, flags, data, dlc, ts, channel=bus - 1))
        self.count += 1

    def flush(self) -> None:
        f = getattr(self._w, "file", None)
        if f is not None and hasattr(f, "flush"):
            try:
                f.flush()
            except (ValueError, OSError):
                pass

    def close(self) -> None:
        self._w.stop()


def _open_writer(path: Path, bitrate: int):
    ext = path.suffix.lower()
    if ext == ".trc":
        return TrcWriter21(path)
    if ext == ".canbin":
        return _CanbinStream(path, bitrate=bitrate)
    if ext == ".asc":
        return _PyCanStream(can.ASCWriter(path))
    if ext == ".blf":
        return _PyCanStream(can.BLFWriter(path))
    if ext == ".log":
        return _PyCanStream(can.CanutilsLogWriter(path))
    if ext == ".csv":
        return _PyCanStream(can.CSVWriter(path))
    raise ValueError(f"cannot record to {ext or 'a file without extension'}; use one of {', '.join(RECORD_FORMATS)}")


class Recorder:
    """Writes every frame of the attached channels to one file. put() is called on the receive threads."""

    def __init__(self, path, channels: dict, bitrate: int = 0):
        """channels: {channel index: bus number written to the file (1-based)}"""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.bus_numbers = dict(channels)
        self._writer = _open_writer(self.path, bitrate)
        self._q: "queue.SimpleQueue" = queue.SimpleQueue()
        self._stop = threading.Event()
        self.started = time.time()
        self.stopped: Optional[float] = None
        self.error = ""
        self.received = 0
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()

    def put(self, ch_index: int, frame) -> None:
        self.received += 1
        self._q.put((ch_index, frame))

    def _run(self) -> None:
        w = self._writer
        get = self._q.get
        last_flush = time.monotonic()
        while True:
            try:
                ch, frame = get(timeout=0.2)
            except queue.Empty:
                if self._stop.is_set():
                    break
                continue
            try:
                w.write(self.bus_numbers.get(ch, ch + 1), frame)
            except Exception as exc:              # disk full, ...: keep draining, report it
                if not self.error:
                    log.exception("recorder write failed")
                self.error = f"{type(exc).__name__}: {exc}"
            if time.monotonic() - last_flush > 1.0:
                last_flush = time.monotonic()
                try:
                    w.flush()
                except Exception:
                    pass
        try:
            w.close()
        except Exception as exc:
            self.error = self.error or f"{type(exc).__name__}: {exc}"
            log.exception("recorder close failed")

    def stop(self) -> None:
        """Stop after everything queued so far is written (detach it from the channels first)."""
        self.stopped = time.time()
        self._stop.set()
        self._thread.join(30)

    @property
    def written(self) -> int:
        return getattr(self._writer, "count", 0)

    def status(self) -> dict:
        size = self.path.stat().st_size if self.path.exists() else 0
        return {"path": str(self.path), "format": self.path.suffix.lower().lstrip("."),
                "started": self.started, "stopped": self.stopped,
                "duration": round((self.stopped or time.time()) - self.started, 1),
                "received": self.received, "written": self.written, "pending": self._q.qsize(),
                "bytes": size, "error": self.error, "channels": sorted(self.bus_numbers)}


# ------------------------------------------------------------------------------------------------- reading

def read_frames(path) -> Iterator[tuple]:
    """Yield (bus, frame) from a recorded file; bus is 1-based (1 when the file has no channel info)."""
    p = Path(path)
    ext = p.suffix.lower()
    if ext == ".canbin":
        header, frames = canbin.read_canbin(p)
        t0 = header.start_s + header.start_us / 1e6 if header.start_s else 0.0
        for fr in frames:
            flags = (F_EXT if fr.extended else 0) | (F_RTR if fr.rtr else 0)
            data = b"" if fr.rtr else bytes(fr.data[:fr.dlc])
            yield 1, (t0 + fr.us / 1e6, fr.can_id, flags, fr.dlc, data)
        return
    if ext not in READ_FORMATS:
        raise ValueError(f"cannot read {ext or 'a file without extension'}; use one of {', '.join(READ_FORMATS)}")
    for msg in can.LogReader(str(p)):
        bus = 1
        ch = msg.channel
        if isinstance(ch, int):
            bus = ch + 1 if ext in (".asc", ".blf", ".log") else max(1, ch)
        elif isinstance(ch, str):
            digits = "".join(c for c in ch if c.isdigit())   # "1", "can1", "vcan0"
            if digits:
                bus = int(digits) + (1 if ext in (".asc", ".blf") else 0)
        flags = msg_flags(msg) | (0 if msg.is_rx else F_TX)
        yield max(1, bus), (msg.timestamp, msg.arbitration_id, flags, msg.dlc, bytes(msg.data))
