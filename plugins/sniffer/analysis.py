"""What the sniffer knows about each CAN id: which bits change, when, how often, and what the bytes look like.

Bits are numbered the DBC little-endian way: bit i is bit (i % 8) of byte (i // 8), so bit 0 is the least
significant bit of byte 0. Everything here runs on the plugin's own thread (or in tests); nothing touches the bus.

Notch: "hide what is changing now". After notch(), bits that had changed before are hidden, and only bits that
change afterwards count as new, so pressing a pedal shows just the bits that follow it. Counter and checksum bytes
(once recognised) never count as new: they change all the time.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Iterable, Optional

from canopener.core.channel import F_EXT

HEAT = ((0.15, 9), (0.3, 8), (0.6, 7), (1.0, 6), (1.5, 5), (2.0, 4), (3.0, 3), (5.0, 2), (10.0, 1))
HISTORY = 200                  # frames kept per id whose data changed (checksum test)
SIG_HISTORY = 1000             # frames kept per id where a bit other than a counter/checksum changed (ranges seen)
MAX_IDS = 4096
TAGS_EVERY = 1.0               # seconds between guess updates of an id whose data changed


def heat(age: Optional[float]) -> int:
    """0-9: how recently a bit changed (9 = just now, 0 = more than 10 s ago or never)."""
    if age is None:
        return 0
    for limit, level in HEAT:
        if age < limit:
            return level
    return 0


def bits_of(mask: int) -> Iterable[int]:
    while mask:
        low = mask & -mask
        yield low.bit_length() - 1
        mask ^= low


def wire_pos(bit: int) -> int:
    """Position of a DBC-numbered bit in the order the bits are sent (bit 7 of byte 0 first): Motorola order."""
    return (bit // 8) * 8 + 7 - bit % 8


def runs(positions: list[int]) -> list[tuple[int, int]]:
    """Sorted positions -> (first, length) of each run of consecutive positions."""
    out, start, prev = [], None, None
    for p in positions:
        if start is None or p != prev + 1:
            if start is not None:
                out.append((start, prev - start + 1))
            start = p
        prev = p
    if start is not None:
        out.append((start, prev - start + 1))
    return out


def extract(data: bytes, start: int, length: int, order: str) -> Optional[int]:
    """The raw value of a DBC-numbered bit range (same numbering as the quick signals), or None if too short."""
    nbits = len(data) * 8
    if order == "little":
        if start + length > nbits:
            return None
        return (int.from_bytes(data, "little") >> start) & ((1 << length) - 1)
    last = wire_pos(start) + length - 1
    if last >= nbits:
        return None
    return (int.from_bytes(data, "big") >> (nbits - 1 - last)) & ((1 << length) - 1)


class IdState:
    __slots__ = ("channel", "can_id", "ext", "count", "data", "value", "first", "last", "period", "changes",
                 "last_change", "change_count", "ever", "notch", "new", "bmin", "bmax", "seen", "deltas",
                 "ndeltas", "history", "sig_history", "byte_changes", "tags", "tags_at", "tags_changes", "armed",
                 "new_id")

    def __init__(self, channel: int, can_id: int, ext: bool):
        self.channel, self.can_id, self.ext = channel, can_id, ext
        self.count = self.changes = 0
        self.data = b""
        self.value = 0
        self.first = self.last = 0.0
        self.period = 0.0
        self.last_change: dict[int, float] = {}     # bit -> time of its last change
        self.change_count: dict[int, int] = {}
        self.ever = self.notch = self.new = 0          # bit masks: changed since reset / hidden / new since notch
        self.armed = False                             # a notch is set: changes count as new
        self.new_id = False                            # first seen after the notch
        self.bmin: list[int] = []
        self.bmax: list[int] = []
        self.seen: list[int] = []                      # per byte, a 256-bit set of the values seen
        self.deltas: list[dict[int, int]] = []         # per byte: (value - previous) mod 256 -> count
        self.ndeltas: list[dict[int, int]] = []        # the same for the low nibble (4-bit counters)
        self.byte_changes: list[int] = []
        self.history: deque = deque(maxlen=HISTORY)
        self.sig_history: deque = deque(maxlen=SIG_HISTORY)
        self.tags: list[str] = []                      # guesses per byte, see update_tags()
        self.tags_at = -1e9
        self.tags_changes = -1

    @property
    def key(self) -> str:
        return f"{self.channel}:{self.can_id:X}:{int(self.ext)}"

    def _grow(self, n: int) -> None:
        while len(self.bmin) < n:
            self.bmin.append(255)
            self.bmax.append(0)
            self.seen.append(0)
            self.deltas.append({})
            self.ndeltas.append({})
            self.byte_changes.append(0)

    def add(self, ts: float, data: bytes) -> None:
        n = len(data)
        self._grow(n)
        value = int.from_bytes(data, "little")
        if self.count:
            dt = ts - self.last
            if dt > 0:
                self.period = dt if not self.period else self.period * 0.9 + dt * 0.1
            diff = value ^ self.value
            if len(data) != len(self.data):            # a length change: the new or lost bytes count as changed
                diff |= ((1 << (8 * max(n, len(self.data)))) - 1) ^ ((1 << (8 * min(n, len(self.data)))) - 1)
            if diff:
                self.changes += 1
                for b in bits_of(diff):
                    self.last_change[b] = ts
                    self.change_count[b] = self.change_count.get(b, 0) + 1
                self.ever |= diff
                if self.armed:
                    self.new |= diff & ~self.notch
                old = self.data
                for i in range(n):
                    if i < len(old) and data[i] != old[i]:
                        self.byte_changes[i] += 1
                        d = self.deltas[i]
                        k = (data[i] - old[i]) & 0xFF
                        if k in d or len(d) < 16:
                            d[k] = d.get(k, 0) + 1
                        nd = self.ndeltas[i]
                        k = ((data[i] & 0xF) - (old[i] & 0xF)) & 0xF
                        nd[k] = nd.get(k, 0) + 1
                self.history.append(data)
                if diff & ~self.noise():
                    self.sig_history.append(data)
        else:
            self.first = ts
            self.history.append(data)
            self.sig_history.append(data)
        for i, b in enumerate(data):
            if b < self.bmin[i]:
                self.bmin[i] = b
            if b > self.bmax[i]:
                self.bmax[i] = b
            self.seen[i] |= 1 << b
        self.count += 1
        self.last = ts
        self.data = data
        self.value = value

    # ------------------------------------------------------------------ guesses
    def update_tags(self, now: float, force: bool = False) -> list[str]:
        """Recompute the guesses per byte (at most every TAGS_EVERY seconds, and only when something changed)."""
        if force or len(self.tags) != len(self.data) or (
                self.changes != self.tags_changes and now - self.tags_at >= TAGS_EVERY):
            self.tags = self._compute_tags()
            self.tags_at, self.tags_changes = now, self.changes
        return self.tags

    def _compute_tags(self) -> list[str]:
        n = len(self.data)
        tags = [self._counter_tag(i) for i in range(n)]
        kinds = self._checksum_kinds([i for i in range(n) if not tags[i]])
        xors = [i for i, k in kinds.items() if k == "xor"]
        if len(xors) > 1:
            # with an XOR checksum in the frame every byte is the XOR of the others: the checksum is the byte
            # that changes whenever anything changes
            keep = max(xors, key=lambda i: (self.byte_changes[i], i))
            kinds = {i: k for i, k in kinds.items() if k != "xor" or i == keep}
        for i in kinds:
            tags[i] = "chk"
        return tags

    def _counter_tag(self, i: int) -> str:
        """cnt (8-bit counter), cnt4 (4-bit counter in the low nibble), const, or ""."""
        changes = self.byte_changes[i]
        if changes == 0:
            return "const" if self.count >= 10 else ""
        frames = max(self.count - 1, 1)
        if changes < 8 or changes / frames < 0.8:
            return ""
        # an 8-bit counter has the same step modulo 256 in every frame, even across its wrap; a counter in the low
        # nibble does not (its wrap, or a signal in the high nibble, breaks the full-byte step)
        if max(self.deltas[i].values()) / changes >= 0.96:
            return "cnt"
        nd = self.ndeltas[i]
        top = max(nd, key=nd.get) if nd else 0
        if top and nd[top] / changes >= 0.9:
            return "cnt4"
        return ""

    def _checksum_kinds(self, candidates: list[int]) -> dict[int, str]:
        """{byte: "xor" | "sum"} for bytes that are the XOR, or the sum plus a constant, of the other bytes in at
        least 90 % of 12+ frames. A checksum changes whenever anything else changes, and needs at least two other
        changing bytes (a byte that copies one other byte is not a checksum)."""
        frames = list(self.history)
        if len(frames) < 12 or not self.changes:
            return {}
        out = {}
        changing = [j for j in range(len(self.data)) if self.byte_changes[j]]
        for i in candidates:
            if self.byte_changes[i] < max(6, 0.8 * self.changes) or len([j for j in changing if j != i]) < 2:
                continue
            ok_x, ks, total = 0, {}, 0
            for d in frames:
                if len(d) <= i:
                    continue
                x = s = 0
                for b in d:
                    x ^= b
                    s += b
                total += 1
                ok_x += (x ^ d[i]) == d[i]            # the XOR of the others equals the byte
                k = (d[i] - (s - d[i])) & 0xFF
                ks[k] = ks.get(k, 0) + 1
            if total < 12:
                continue
            if ok_x >= 0.9 * total:
                out[i] = "xor"
            elif max(ks.values()) >= 0.9 * total:
                out[i] = "sum"
        return out

    def byte_tag(self, i: int) -> str:
        tags = self.update_tags(time.time())
        return tags[i] if i < len(tags) else ""

    def noise(self) -> int:
        """The bits of counters and checksums: they change all the time, so they never count as new."""
        mask = 0
        for i, tag in enumerate(self.tags):
            if tag in ("cnt", "chk"):
                mask |= 0xFF << (8 * i)
            elif tag == "cnt4":
                mask |= 0x0F << (8 * i)
        return mask

    def new_bits(self) -> int:
        return self.new & ~self.noise()

    def candidates(self, mask: Optional[int] = None) -> list[dict]:
        """Signal candidates from the bits that changed (the new bits after a notch, else every changed bit, minus
        counters and checksums): each run of changed bits in Intel order and in Motorola (wire) order, and each
        span of neighbouring changed bytes byte-aligned in both byte orders. At most 64 bits each."""
        if mask is None:
            mask = self.new_bits() if self.armed else self.ever & ~self.noise()
        bits = sorted(bits_of(mask))
        options: list[tuple[int, int, str]] = []
        for s, n in runs(bits):                                  # Intel runs
            for off in range(0, n, 64):
                options.append((s + off, min(64, n - off), "little"))
        for p, n in runs(sorted(wire_pos(b) for b in bits)):     # Motorola runs; DBC start bit = the MSB
            for off in range(0, n, 64):
                options.append((wire_pos(p + off), min(64, n - off), "big"))
        for b0, nb in runs(sorted({b // 8 for b in bits})):     # byte spans, byte-aligned
            for off in range(0, nb, 8):
                first, count = b0 + off, min(8, nb - off)
                options.append((first * 8, count * 8, "little"))
                if count > 1:
                    options.append((first * 8 + 7, count * 8, "big"))
        out, done = [], set()
        for o in options:
            if o not in done:
                done.add(o)
                out.append(self._candidate(*o))
        return out

    def _candidate(self, start: int, length: int, order: str) -> dict:
        vals = [v for v in (extract(d, start, length, order) for d in self.sig_history) if v is not None]
        if order == "little":
            first_byte, last_byte = start // 8, (start + length - 1) // 8
        else:
            first_byte, last_byte = start // 8, (wire_pos(start) + length - 1) // 8
        kinds = sorted({self.tags[i] for i in range(first_byte, min(last_byte + 1, len(self.tags))) if self.tags[i]})
        return {"start_bit": start, "length": length, "byte_order": order,
                "value": extract(self.data, start, length, order), "min": min(vals) if vals else None,
                "max": max(vals) if vals else None, "distinct": len(set(vals)), "frames": len(vals), "tags": kinds}

    def summary(self, now: float) -> dict:
        nbits = len(self.data) * 8
        lc = self.last_change
        heats = "".join(str(heat(now - lc[b])) if b in lc else "0" for b in range(nbits))
        return {"key": self.key, "channel": self.channel, "id": self.can_id, "ext": self.ext, "count": self.count,
                "period_ms": round(self.period * 1000, 1), "data": self.data.hex().upper(), "heat": heats,
                "notch": f"{self.notch:X}", "new": f"{self.new_bits():X}", "ever": f"{self.ever:X}",
                "tags": list(self.tags), "new_id": self.new_id,
                "age_ms": round((now - max(lc.values())) * 1000) if lc else None}

    def detail(self, all_changes: bool = False) -> dict:
        n = len(self.data)
        self.update_tags(time.time())
        return {"key": self.key, "channel": self.channel, "id": self.can_id, "ext": self.ext, "dlc": n,
                "count": self.count, "changes": self.changes, "armed": self.armed,
                "bytes": [{"byte": i, "value": self.data[i], "min": self.bmin[i], "max": self.bmax[i],
                           "distinct": bin(self.seen[i]).count("1"), "changes": self.byte_changes[i],
                           "tag": self.tags[i] if i < len(self.tags) else ""} for i in range(n)],
                "bit_changes": [self.change_count.get(b, 0) for b in range(n * 8)],
                "candidates": self.candidates(self.ever & ~self.noise() if all_changes else None)}


def _parse_key(key: str) -> Optional[tuple]:
    try:
        ch, cid, ext = key.split(":")
        return int(ch), int(cid, 16), ext == "1"
    except ValueError:
        return None


class Sniffer:
    """All ids, thread-safe. feed() and update_tags() from the plugin thread; snapshot()/detail()/notch() from
    routes."""

    def __init__(self):
        self.ids: dict[tuple, IdState] = {}
        self.lock = threading.Lock()
        self.frames = 0
        self.capped = 0                # frames of new ids ignored because MAX_IDS was reached
        self.notched_at: Optional[float] = None
        self.started = time.time()

    def feed(self, items) -> None:
        with self.lock:
            for ch, (ts, cid, flags, dlc, data) in items:
                key = (ch, cid, bool(flags & F_EXT))
                st = self.ids.get(key)
                if st is None:
                    if len(self.ids) >= MAX_IDS:
                        self.capped += 1
                        continue
                    st = self.ids[key] = IdState(ch, cid, key[2])
                    st.armed = st.new_id = self.notched_at is not None
                st.add(ts, bytes(data))
                self.frames += 1

    def update_tags(self, now: Optional[float] = None) -> None:
        """Refresh the guesses of ids that changed (called by the plugin thread about once a second)."""
        now = time.time() if now is None else now
        with self.lock:
            states = list(self.ids.values())
        for st in states:                                        # each id briefly, so feed() is never starved
            with self.lock:
                st.update_tags(now)

    def notch(self) -> int:
        """Hide every bit that has changed so far; returns how many bits are hidden."""
        with self.lock:
            self.notched_at = time.time()
            hidden = 0
            for st in self.ids.values():
                st.notch |= st.ever
                st.new = 0
                st.armed = True
                st.new_id = False
                hidden += bin(st.notch).count("1")
            return hidden

    def clear_notch(self) -> None:
        with self.lock:
            self.notched_at = None
            for st in self.ids.values():
                st.notch = st.new = 0
                st.armed = st.new_id = False

    def reset(self) -> None:
        with self.lock:
            self.ids.clear()
            self.frames = self.capped = 0
            self.notched_at = None
            self.started = time.time()

    def snapshot(self, channel: Optional[int] = None, ids: Optional[list] = None, only_new: bool = False,
                 hide_static: bool = False, now: Optional[float] = None) -> list[dict]:
        """ids: [(low, high), ...] ranges to show (None = all)."""
        now = time.time() if now is None else now
        only_new = only_new and self.notched_at is not None
        with self.lock:
            out = []
            for st in self.ids.values():
                if channel is not None and st.channel != channel:
                    continue
                if ids is not None and not any(lo <= st.can_id <= hi for lo, hi in ids):
                    continue
                if not st.tags and st.data:
                    st.update_tags(now)
                if only_new and not (st.new_id or st.new_bits()):
                    continue
                if hide_static and not st.ever:
                    continue
                out.append(st.summary(now))
            out.sort(key=lambda r: (r["channel"], r["ext"], r["id"]))
            return out

    def detail(self, key: str, all_changes: bool = False) -> Optional[dict]:
        k = _parse_key(key)
        if k is None:
            raise ValueError(f"bad key {key!r}")
        with self.lock:
            st = self.ids.get(k)
            return st.detail(all_changes) if st else None
