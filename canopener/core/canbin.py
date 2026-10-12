"""`.canbin` binary capture format: a compact fixed-size record format for capturing CAN on a PLC or other
small logger (simple to write from IEC 61131-3), read and converted to `.trc` by this tool.

Little-endian throughout.

Header, 32 bytes:
    CHAR[8]  magic     `CANBIN01`
    UDINT    count     frame count
    UDINT    bitrate   bit rate in bit/s (0 = unknown)
    UDINT    start_s   start time, seconds since 1970 (RTC, 0 = unknown)
    UDINT    start_us  start time, microsecond fraction
    USINT    channel
    USINT[7] reserved  (0)

Then `count` frames, 20 bytes each:
    UDINT    us        microseconds since capture start
    UDINT    id        CAN id (bit 31 set = 29-bit/extended id)
    USINT    dlc
    USINT    flags     bit 0 = RTR
    BYTE[2]  reserved
    BYTE[8]  data      only the first `dlc` bytes are meaningful
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Tuple

import can

from .trc import _write_trc_header, format_row

_NOOP = lambda *a, **k: None  # noqa: E731

MAGIC = b"CANBIN01"
HEADER_SIZE = 32
FRAME_SIZE = 20

_HEADER_STRUCT = struct.Struct("<8sIIIIB7x")
_FRAME_STRUCT = struct.Struct("<IIBB2s8s")

ID_EXTENDED_BIT = 1 << 31
FLAG_RTR = 1 << 0

# The OLE Automation epoch used by trace.py's $STARTTIME, reused here as the
# base for the header's `start_s`/`start_us` (seconds since 1970 - a plain
# Unix epoch, UTC).
_UNIX_EPOCH = datetime(1970, 1, 1)


class CanbinError(ValueError):
    """A `.canbin` file failed validation (bad magic, truncated, ...)."""


@dataclass
class CanbinHeader:
    count: int
    bitrate: int = 0
    start_s: int = 0
    start_us: int = 0
    channel: int = 0


@dataclass
class CanbinFrame:
    us: int
    can_id: int
    extended: bool
    dlc: int
    rtr: bool
    data: bytes = field(default=b"")


def read_canbin(path, warn: Callable[[str], None] = _NOOP) -> Tuple[CanbinHeader, List[CanbinFrame]]:
    """Read and validate a `.canbin` file. Raises `CanbinError` on a bad
    magic number or a file too small to hold the header.

    A logger streams frames to disk while recording and only patches the
    header's frame `count` when the capture is closed cleanly, so a file cut
    short by a power loss can have `count == 0` (frames are then read to end
    of file) or a trailing frame that never finished writing. Neither is
    fatal: a non-zero `count` that the file doesn't actually hold, and a
    trailing partial frame, are both reported to `warn` rather than raised."""
    data = Path(path).read_bytes()
    if len(data) < HEADER_SIZE:
        raise CanbinError(
            f"{path}: file too small to contain a .canbin header "
            f"({len(data)} byte(s), need at least {HEADER_SIZE})"
        )

    magic, count, bitrate, start_s, start_us, channel = _HEADER_STRUCT.unpack(data[:HEADER_SIZE])
    if magic != MAGIC:
        raise CanbinError(f"{path}: bad magic {magic!r}, expected {MAGIC!r}")

    available_frames, remainder = divmod(len(data) - HEADER_SIZE, FRAME_SIZE)
    if remainder:
        warn(
            f"{path}: {remainder} trailing byte(s) after the last full frame "
            f"- ignored (truncated write)"
        )

    if count == 0:
        frame_count = available_frames
    elif available_frames < count:
        warn(
            f"{path}: header declares {count} frame(s) but only "
            f"{available_frames} are present - reading what's there "
            f"(truncated write)"
        )
        frame_count = available_frames
    else:
        frame_count = count

    header = CanbinHeader(count=frame_count, bitrate=bitrate, start_s=start_s, start_us=start_us, channel=channel)

    frames = []
    offset = HEADER_SIZE
    for _ in range(frame_count):
        us, id_field, dlc, flags, _reserved, data_bytes = _FRAME_STRUCT.unpack(
            data[offset:offset + FRAME_SIZE]
        )
        frames.append(CanbinFrame(
            us=us,
            can_id=id_field & (ID_EXTENDED_BIT - 1),
            extended=bool(id_field & ID_EXTENDED_BIT),
            dlc=dlc,
            rtr=bool(flags & FLAG_RTR),
            data=data_bytes,
        ))
        offset += FRAME_SIZE

    return header, frames


def write_canbin(path, header: CanbinHeader, frames: List[CanbinFrame]) -> None:
    """Write a `.canbin` file. `header.count` is ignored - the count written
    is always `len(frames)`."""
    frames = list(frames)
    with open(path, "wb") as f:
        f.write(_HEADER_STRUCT.pack(
            MAGIC, len(frames), header.bitrate, header.start_s, header.start_us, header.channel,
        ))
        for frame in frames:
            id_field = frame.can_id | (ID_EXTENDED_BIT if frame.extended else 0)
            flags = FLAG_RTR if frame.rtr else 0
            data = bytes(frame.data[:8]).ljust(8, b"\x00")
            f.write(_FRAME_STRUCT.pack(frame.us, id_field, frame.dlc, flags, b"\x00\x00", data))


def canbin_to_trc(in_path, out_path, warn: Callable[[str], None] = _NOOP) -> int:
    """Convert a `.canbin` capture to a PCAN-View v1.1 `.trc` file. Returns
    the number of frames written. `$STARTTIME` comes from the header's
    start time (treated as UTC); each row's time offset is `us / 1000.0` ms
    per frame, straight from the header - not accumulated between frames.
    Any read warnings (see `read_canbin`) are passed to `warn`."""
    header, frames = read_canbin(in_path, warn=warn)
    start_dt = _UNIX_EPOCH + timedelta(seconds=header.start_s, microseconds=header.start_us)

    with open(out_path, "w", newline="\r\n", encoding="ascii") as f:
        _write_trc_header(f, start_dt)
        for n, frame in enumerate(frames, start=1):
            elapsed_ms = frame.us / 1000.0
            f.write(format_row(n, elapsed_ms, frame.can_id, frame.extended, frame.dlc, frame.data[:frame.dlc]))

    return len(frames)


class _CanbinCollector(can.Listener):
    """Collects received frames as `CanbinFrame`s, timestamped as
    microseconds since the first frame."""

    def __init__(self):
        self.frames: List[CanbinFrame] = []
        self._t0 = None

    def on_message_received(self, msg: can.Message) -> None:
        if msg.is_error_frame:
            return
        timestamp = msg.timestamp if msg.timestamp else time.time()
        if self._t0 is None:
            self._t0 = timestamp
        us = int(round((timestamp - self._t0) * 1_000_000))
        self.frames.append(CanbinFrame(
            us=us,
            can_id=msg.arbitration_id,
            extended=msg.is_extended_id,
            dlc=msg.dlc,
            rtr=msg.is_remote_frame,
            data=bytes(msg.data),
        ))


def capture_canbin(network, seconds: float, out_path, bitrate: int = 0, channel: int = 0,
                    log: Callable = _NOOP) -> int:
    """Capture bus traffic for `seconds` using the already-connected
    `network` and write it to `out_path` in `.canbin` format. Returns the
    number of frames written."""
    collector = _CanbinCollector()
    start_ts = time.time()  # POSIX timestamp (UTC-based), matching the header's epoch
    network.notifier.add_listener(collector)
    try:
        log(f"Capturing for {seconds}s -> {out_path}")
        time.sleep(seconds)
    finally:
        network.notifier.remove_listener(collector)

    header = CanbinHeader(
        count=len(collector.frames),
        bitrate=bitrate,
        start_s=int(start_ts),
        start_us=int(round((start_ts - int(start_ts)) * 1_000_000)),
        channel=channel,
    )
    write_canbin(out_path, header, collector.frames)
    log(f"Wrote {len(collector.frames)} frame(s) to {out_path}")
    return len(collector.frames)
