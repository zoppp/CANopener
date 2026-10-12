"""Quick signals: named values cut out of raw frames, without a DBC file.

A definition is a dict {name, channel (None = any), can_id, extended, start_bit, length, byte_order
("little" | "big"), signed, factor, offset, unit, min, max}. Bit numbering is the DBC one: for little endian
(Intel) start_bit is the least significant bit, for big endian (Motorola) it is the most significant bit, with
bit 7 of byte 0 being the first bit on the wire.

The definitions live in svc.settings["raw_signals"] (and in projects). RawDecoder reads frames from an engine
Tap on its own thread and publishes "raw/<name>" into svc.signals. The Tap's accept() is a set lookup, so the
receive threads only pay for frames that carry a quick signal.
"""

from __future__ import annotations

import logging
import math
import re
import threading
from typing import Optional

from ..core.channel import F_ERR, F_EXT, F_RTR, Tap
from ..core.transmit import parse_id
from ..service import ServiceError

log = logging.getLogger(__name__)

PREFIX = "raw/"
MAX_SIGNALS = 500
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,60}$")


def _num(d: dict, key: str, default=None) -> Optional[float]:
    v = d.get(key, default)
    if v in (None, ""):
        return None
    if isinstance(v, bool):
        raise ServiceError(f"{key}: {v!r} is not a number")
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        raise ServiceError(f"{key}: {v!r} is not a number") from None
    if not math.isfinite(f):                     # NaN/inf would break JSON for every client
        raise ServiceError(f"{key}: {v!r} is not a finite number")
    return f


def _int(d: dict, key: str, default: int) -> int:
    v = d.get(key, default)
    if isinstance(v, bool) or (isinstance(v, float) and not v.is_integer()):
        raise ServiceError(f"{key}: {v!r} is not a whole number")
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        raise ServiceError(f"{key}: {v!r} is not a whole number") from None


def clean_list(defs) -> list:
    """The valid definitions of a settings or project value (anything else is dropped, with a log line)."""
    out = []
    for d in defs if isinstance(defs, list) else []:
        try:
            out.append(normalize(d))
        except ServiceError as exc:
            log.warning("dropped quick signal %r: %s", d, exc)
    return out[:MAX_SIGNALS]


def normalize(d: dict) -> dict:
    """Check one definition and return it in the stored form (raises ServiceError with a message)."""
    if not isinstance(d, dict):
        raise ServiceError("a quick signal must be an object")
    name = str(d.get("name") or "").strip()
    if not _NAME_RE.match(name):
        raise ServiceError(f"invalid signal name {name!r}: use letters, digits, _ . - (no spaces)")
    channel = None if d.get("channel") in (None, "", "any") else _int(d, "channel", 0)
    if channel is not None and channel < 0:
        raise ServiceError(f"{name}: invalid channel {channel}")
    try:
        can_id = parse_id(d.get("can_id", ""))
    except ValueError:
        raise ServiceError(f"{name}: invalid CAN id {d.get('can_id')!r} (hex)") from None
    extended = d.get("extended")
    extended = can_id > 0x7FF if extended is None else bool(extended)
    if not 0 <= can_id <= (0x1FFFFFFF if extended else 0x7FF):
        raise ServiceError(f"{name}: id 0x{can_id:X} does not fit {'29' if extended else '11'} bits")
    start, length = _int(d, "start_bit", 0), _int(d, "length", 8)
    order = d.get("byte_order", "little")
    if order not in ("little", "big"):
        raise ServiceError(f"{name}: byte_order must be 'little' or 'big'")
    if not 1 <= length <= 64 or not 0 <= start < 512:
        raise ServiceError(f"{name}: start bit 0..511 and length 1..64")
    last = start + length - 1 if order == "little" else _msb_pos(start) + length - 1
    if last >= 512:
        raise ServiceError(f"{name}: the signal runs past the end of a 64-byte frame")
    factor = _num(d, "factor", 1.0)
    return {"name": name, "channel": channel, "can_id": can_id, "extended": extended, "start_bit": start,
            "length": length, "byte_order": order, "signed": bool(d.get("signed")),
            "factor": 1.0 if factor is None else factor, "offset": _num(d, "offset", 0.0) or 0.0,
            "unit": str(d.get("unit") or "")[:20], "min": _num(d, "min"), "max": _num(d, "max")}


def normalize_all(defs: list) -> list[dict]:
    if not isinstance(defs, list):
        raise ServiceError("signals must be a list")
    if len(defs) > MAX_SIGNALS:
        raise ServiceError(f"at most {MAX_SIGNALS} quick signals")
    out = [normalize(d) for d in defs]
    names = [d["name"] for d in out]
    dup = {n for n in names if names.count(n) > 1}
    if dup:
        raise ServiceError(f"duplicate signal name: {', '.join(sorted(dup))}")
    return out


def _msb_pos(start_bit: int) -> int:
    """DBC big-endian start bit -> position counted from the first bit on the wire (bit 7 of byte 0 = 0)."""
    return (start_bit // 8) * 8 + 7 - start_bit % 8


class RawSignal:
    """One compiled definition: extract(data) -> the scaled value, or None when the frame is too short."""
    __slots__ = ("key", "channel", "can_id", "extended", "unit", "minimum", "maximum",
                 "little", "start", "last", "mask", "sign", "factor", "offset", "plain")

    def __init__(self, d: dict):
        self.key = PREFIX + d["name"]
        self.channel, self.can_id, self.extended = d["channel"], d["can_id"], d["extended"]
        self.unit, self.minimum, self.maximum = d["unit"], d["min"], d["max"]
        length = d["length"]
        self.little = d["byte_order"] == "little"
        self.start = d["start_bit"]
        self.last = self.start + length - 1 if self.little else _msb_pos(self.start) + length - 1
        self.mask = (1 << length) - 1
        self.sign = 1 << (length - 1) if d["signed"] else 0
        self.factor, self.offset = d["factor"], d["offset"]
        self.plain = self.factor == 1 and self.offset == 0      # publish integers as integers

    def extract(self, data: bytes):
        nbits = len(data) * 8
        if self.last >= nbits:
            return None
        if self.little:
            raw = (int.from_bytes(data, "little") >> self.start) & self.mask
        else:
            raw = (int.from_bytes(data, "big") >> (nbits - 1 - self.last)) & self.mask
        if self.sign and raw & self.sign:
            raw -= self.sign << 1
        return raw if self.plain else raw * self.factor + self.offset


class RawDecoder:
    """Publishes the quick signals of svc.settings["raw_signals"]; picks up changes to that list by itself
    (a new list object: the REST routes and Service.load_project always assign a new one)."""

    def __init__(self, svc):
        self.svc = svc
        self._src = object()                     # the settings list compiled last (never matches at first)
        self._by_id: dict[tuple[int, bool], list[RawSignal]] = {}
        self._ids: frozenset = frozenset()
        self.tap = Tap(maxlen=100_000, accept=self._accept)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _accept(self, can_id: int, flags: int) -> bool:          # receive thread: keep it cheap
        return can_id in self._ids and not flags & (F_RTR | F_ERR)

    def start(self) -> None:
        self.refresh()
        self.svc.engine.add_tap(self.tap)
        self._thread = threading.Thread(target=self._run, name="raw-signals", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.svc.engine.remove_tap(self.tap)
        if self._thread is not None:
            self._thread.join(2)

    def refresh(self) -> None:
        defs = self.svc.settings.get("raw_signals")
        if defs is self._src:
            return
        old = self._src if isinstance(self._src, list) else []
        self._src = defs
        if not isinstance(defs, list):             # a broken settings/project value: decode nothing
            defs = []
        by_id: dict[tuple[int, bool], list[RawSignal]] = {}
        for d in defs or []:
            try:
                s = RawSignal(normalize(d))
            except (ServiceError, ValueError, TypeError, KeyError) as exc:
                log.warning("skipped quick signal %r: %s", d, exc)
                continue
            by_id.setdefault((s.can_id, s.extended), []).append(s)
        self._by_id = by_id
        self._ids = frozenset(cid for cid, _ in by_id)
        if any(d not in (defs or []) for d in old):         # removed or changed: drop their old values
            self.svc.signals.clear(PREFIX)

    def decode(self, items) -> None:
        """items: (channel index, frame) tuples from the tap."""
        by_id, publish = self._by_id, self.svc.signals.publish
        for ch, (ts, can_id, flags, dlc, data) in items:
            sigs = by_id.get((can_id, bool(flags & F_EXT)))
            if sigs is None:
                continue
            for s in sigs:
                if s.channel is not None and s.channel != ch:
                    continue
                v = s.extract(data)
                if v is not None:
                    publish(s.key, ts, v, unit=s.unit, minimum=s.minimum, maximum=s.maximum, channel=ch,
                            can_id=can_id)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception:
                log.exception("quick signal definitions could not be loaded")
            first = self.tap.get(0.2)
            if first is None:
                continue
            try:
                self.decode([first] + self.tap.drain(5000))
            except Exception:
                log.exception("quick signal decoding failed")


def preview(svc, defs: list) -> list[dict]:
    """Decode each definition from the newest matching frame in the receive tables (for the editor)."""
    out = []
    tables = {ch.index: ch.rows_snapshot()[1] for ch in list(svc.engine.channels.values())}   # one lock each
    for d in defs:
        try:
            s = RawSignal(normalize(d))
        except (ServiceError, ValueError, TypeError) as exc:
            out.append({"value": None, "note": str(exc)})
            continue
        best = None
        for index, rows in tables.items():
            if s.channel is not None and index != s.channel:
                continue
            for r in rows:
                cid, flags, data, t_last = r[1], r[2], r[4], r[8]
                if cid == s.can_id and bool(flags & F_EXT) == s.extended and not flags & (F_RTR | F_ERR) \
                        and (best is None or t_last > best[0]):
                    best = (t_last, index, data)
        if best is None:
            out.append({"value": None, "note": "no frame with this id received yet"})
            continue
        v = s.extract(best[2])
        out.append({"value": v, "data": best[2].hex(" ").upper(), "channel": best[1], "ts": best[0],
                    **({} if v is not None else {"note": "frame too short for this signal"})})
    return out
