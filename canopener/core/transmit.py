"""Transmit list: frames sent once on demand or cyclically by one scheduler thread."""

from __future__ import annotations

import itertools
import logging
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

import can

from .channel import F_BRS, F_EXT, F_FD, F_RTR

log = logging.getLogger(__name__)

FD_LENGTHS = (0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64)
_ids = itertools.count(1)


def parse_data(text) -> bytes:
    """'11 22 33', '112233', '11,22' or a list of ints -> bytes."""
    if isinstance(text, (bytes, bytearray)):
        return bytes(text)
    if isinstance(text, (list, tuple)):
        return bytes(int(b) & 0xFF for b in text)
    s = str(text or "").replace(",", " ").replace("0x", "").replace("0X", "").strip()
    if not s:
        return b""
    parts = s.split()
    if len(parts) == 1 and len(parts[0]) > 2:
        p = parts[0]
        if len(p) % 2:
            raise ValueError(f"odd number of hex digits in {text!r}")
        parts = [p[i:i + 2] for i in range(0, len(p), 2)]
    try:
        return bytes(int(p, 16) for p in parts)
    except ValueError:
        raise ValueError(f"not hex bytes: {text!r}") from None


def parse_id(text) -> int:
    if isinstance(text, int):
        return text
    s = str(text).strip().lower()
    if s.endswith("h"):
        s = s[:-1]
    return int(s, 16)                      # ids are hex, with or without 0x


def build_message(can_id: int, data: bytes = b"", *, ext: Optional[bool] = None, fd: bool = False,
                  brs: bool = False, rtr: bool = False, dlc: Optional[int] = None) -> can.Message:
    if ext is None:
        ext = can_id > 0x7FF
    if not ext and can_id > 0x7FF:
        raise ValueError(f"id 0x{can_id:X} does not fit an 11-bit identifier; tick 'extended'")
    if can_id > 0x1FFFFFFF or can_id < 0:
        raise ValueError(f"id 0x{can_id:X} is out of range")
    if fd:
        if rtr:
            raise ValueError("CAN FD has no remote frames")
        if len(data) > 64:
            raise ValueError("CAN FD carries at most 64 data bytes")
        if len(data) not in FD_LENGTHS:              # pad to the next valid FD length
            target = next(n for n in FD_LENGTHS if n >= len(data))
            data = data + bytes(target - len(data))
    elif len(data) > 8:
        raise ValueError("classic CAN carries at most 8 data bytes (tick FD for more)")
    if rtr:
        return can.Message(arbitration_id=can_id, is_extended_id=ext, is_remote_frame=True,
                           dlc=dlc if dlc is not None else 0)
    return can.Message(arbitration_id=can_id, is_extended_id=ext, is_fd=fd, bitrate_switch=fd and brs,
                       data=data)


@dataclass
class TxEntry:
    can_id: int
    data: str = ""                 # hex text as typed
    ext: bool = False
    fd: bool = False
    brs: bool = False
    rtr: bool = False
    dlc: int = 0                   # remote frames only
    period_ms: float = 0.0         # 0 = manual (send on click)
    enabled: bool = False          # cyclic running
    channel: int = 0
    comment: str = ""
    dbc_message: str = ""          # set: can_id/ext/data are encoded from `signals` with the loaded DBC
    signals: dict = field(default_factory=dict)
    id: int = field(default_factory=lambda: next(_ids))
    count: int = 0
    error: str = ""
    next_due: float = 0.0

    def message(self) -> can.Message:
        return build_message(self.can_id, parse_data(self.data), ext=self.ext, fd=self.fd, brs=self.brs,
                             rtr=self.rtr, dlc=self.dlc)

    def flags(self) -> int:
        return ((F_EXT if self.ext else 0) | (F_FD if self.fd else 0) | (F_BRS if self.brs else 0)
                | (F_RTR if self.rtr else 0))

    def as_dict(self) -> dict:
        d = asdict(self)
        d.pop("next_due")
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "TxEntry":
        e = cls(can_id=parse_id(d.get("can_id", 0)))
        for k in ("data", "ext", "fd", "brs", "rtr", "dlc", "period_ms", "enabled", "channel", "comment"):
            if k in d:
                setattr(e, k, type(getattr(e, k))(d[k]) if d[k] is not None else getattr(e, k))
        e.data = str(d.get("data", "") or "")
        e.dbc_message = str(d.get("dbc_message") or "")
        e.signals = dict(d.get("signals") or {})
        return e


def _high_res_timer() -> None:
    """Windows: 1 ms timer resolution so 10 ms cycles do not jitter by the default 15.6 ms tick."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass


class TxScheduler:
    """Owns the transmit list. `send(channel_index, message)` is supplied by the engine."""

    def __init__(self, send: Callable[[int, can.Message], float]):
        self._send = send
        self.encoder: Optional[Callable[[TxEntry], None]] = None   # DBC entries (set by the decode manager)
        self.entries: list[TxEntry] = []
        self._cv = threading.Condition()
        self._stop = False
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        _high_res_timer()
        self._thread = threading.Thread(target=self._run, name="tx-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        if self._thread:
            self._thread.join(2)

    # ------------------------------------------------------------------ list editing
    def get(self, entry_id: int) -> TxEntry:
        for e in self.entries:
            if e.id == entry_id:
                return e
        raise KeyError(f"no transmit entry {entry_id}")

    def _encode(self, entry: TxEntry) -> None:
        if entry.dbc_message:
            if self.encoder is None:
                raise ValueError("no DBC decoder: load the DBC first")
            self.encoder(entry)

    def add(self, entry: TxEntry) -> TxEntry:
        self._encode(entry)
        entry.message()                              # validate now, not in the scheduler
        with self._cv:
            self.entries.append(entry)
            entry.next_due = 0.0
            self._cv.notify_all()
        return entry

    def update(self, entry_id: int, changes: dict) -> TxEntry:
        with self._cv:
            trial = TxEntry.from_dict({**self.get(entry_id).as_dict(), **changes})
        if "dbc_message" in changes or "signals" in changes:
            self._encode(trial)                    # outside the lock: encoding may wait for the decoder
        trial.message()
        with self._cv:
            e = self.get(entry_id)
            for k in ("can_id", "data", "ext", "fd", "brs", "rtr", "dlc", "period_ms", "enabled", "channel",
                      "comment", "dbc_message", "signals"):
                setattr(e, k, getattr(trial, k))
            if e.enabled and e.period_ms <= 0:
                e.enabled = False
            e.error = ""
            e.next_due = 0.0
            self._cv.notify_all()
            return e

    def remove(self, entry_id: int) -> None:
        with self._cv:
            self.entries = [e for e in self.entries if e.id != entry_id]
            self._cv.notify_all()

    def replace_all(self, entries: list[TxEntry]) -> None:
        with self._cv:
            self.entries = list(entries)
            for e in self.entries:
                e.next_due = 0.0
            self._cv.notify_all()

    def stop_all(self) -> None:
        with self._cv:
            for e in self.entries:
                e.enabled = False
            self._cv.notify_all()

    def send_once(self, entry_id: int) -> float:
        e = self.get(entry_id)
        try:
            ts = self._send(e.channel, e.message())
        except Exception as exc:
            e.error = str(exc)
            raise
        e.count += 1
        e.error = ""
        return ts

    # ------------------------------------------------------------------ scheduler
    def _run(self) -> None:
        while True:
            with self._cv:
                if self._stop:
                    return
                now = time.perf_counter()
                due, wait = [], 0.25
                for e in self.entries:
                    if not e.enabled or e.period_ms <= 0:
                        continue
                    if e.next_due == 0.0:
                        e.next_due = now
                    if e.next_due <= now:
                        due.append(e)
                    else:
                        wait = min(wait, e.next_due - now)
                if not due:
                    self._cv.wait(wait)
                    continue
            for e in due:
                period = e.period_ms / 1000.0
                try:
                    self._send(e.channel, e.message())
                    e.count += 1
                    e.error = ""
                except Exception as exc:
                    e.error = str(exc)
                    if "not connected" in e.error or "listen-only" in e.error:
                        e.enabled = False                 # nothing to send on: stop instead of spinning
                e.next_due += period
                if e.next_due < time.perf_counter() - period:   # fell behind (e.g. adapter busy): resync
                    e.next_due = time.perf_counter() + period
