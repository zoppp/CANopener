"""DBC files (cantools): decode frames into signals, encode signal values into frame data.

Several files can be loaded, each for one channel or for all. Messages are looked up by (extended, id); J1939
messages (VFrameFormat J1939PG) also by PGN, so the source address and priority of the frame do not matter.
"""

from __future__ import annotations

import math

from pathlib import Path
from typing import Optional

from .j1939 import split_id


def _default_value(sig):
    if sig.choices:
        raw = 0 if 0 in sig.choices else next(iter(sig.choices))
        return raw * sig.scale + sig.offset
    v = sig.offset
    if sig.minimum is not None:
        v = max(v, sig.minimum)
    if sig.maximum is not None:
        v = min(v, sig.maximum)
    return v


class DbcError(ValueError):
    pass


class DbcMessage:
    """One cantools message plus what decoding needs per frame, computed once."""
    __slots__ = ("msg", "name", "channel", "path", "j1939", "pgn", "meta")

    def __init__(self, msg, channel: Optional[int], path: str):
        self.msg, self.name, self.channel, self.path = msg, msg.name, channel, path
        self.j1939 = msg.is_extended_frame and msg.protocol == "j1939"
        self.pgn = split_id(msg.frame_id)[1] if msg.is_extended_frame else None
        self.meta = {s.name: (s.unit or "", s.minimum, s.maximum,
                              {int(k): str(v) for k, v in (s.choices or {}).items()}) for s in msg.signals}

    def decode(self, data: bytes) -> dict:
        try:
            return self.msg.decode(data, decode_choices=False, allow_truncated=True)
        except Exception:                          # a frame that does not fit the DBC: no values
            return {}

    def text(self, values: dict) -> str:
        parts = []
        for k, v in values.items():
            unit, _, _, choices = self.meta[k]
            if isinstance(v, float) and not math.isfinite(v):     # e.g. float32 FF FF FF FF ("not available")
                parts.append(f"{k}=n/a")
                continue
            if choices and int(v) == v and int(v) in choices:
                parts.append(f"{k}={choices[int(v)]}")
            else:
                parts.append(f"{k}={round(v, 4) if isinstance(v, float) else v}{' ' + unit if unit else ''}")
        return f"{self.name} " + " ".join(parts)

    def info(self) -> dict:
        m = self.msg
        sigs = []
        for s in m.signals:
            unit, lo, hi, choices = self.meta[s.name]
            sigs.append({"name": s.name, "unit": unit, "min": lo, "max": hi, "scale": s.scale, "offset": s.offset,
                         "choices": {str(k): v for k, v in choices.items()}, "mux": bool(s.is_multiplexer),
                         "mux_ids": list(s.multiplexer_ids or []), "comment": s.comment or ""})
        return {"name": m.name, "id": m.frame_id, "ext": m.is_extended_frame, "j1939": self.j1939, "pgn": self.pgn,
                "length": m.length, "senders": list(m.senders), "channel": self.channel, "path": self.path,
                "comment": m.comment or "", "signals": sigs}


class DbcSet:
    def __init__(self):
        self.files: list[dict] = []                    # {"path", "channel", "messages": [DbcMessage]}
        self.by_id: dict[int, list[DbcMessage]] = {}   # key: id | 1 << 31 for extended
        self.by_pgn: dict[int, list[DbcMessage]] = {}  # J1939 messages only

    def load(self, path: str, channel: Optional[int] = None) -> dict:
        try:
            import cantools
        except ImportError:
            raise DbcError("cantools is not installed (pip install cantools)") from None
        p = Path(path)
        if not p.is_file():
            raise DbcError(f"{p} not found")
        try:
            db = cantools.database.load_file(str(p), strict=False)
        except Exception as exc:
            raise DbcError(f"cannot read {p.name}: {exc}") from exc
        self.remove(str(p), channel)
        entry = {"path": str(p), "channel": channel,
                 "messages": [DbcMessage(m, channel, str(p)) for m in db.messages]}
        self.files.append(entry)
        self._index()
        return {"path": str(p), "channel": channel, "messages": len(entry["messages"])}

    def remove(self, path: str, channel: Optional[int] = None) -> None:
        self.files = [f for f in self.files if not (f["path"] == path and f["channel"] == channel)]
        self._index()

    def _index(self) -> None:
        by_id: dict[int, list] = {}
        by_pgn: dict[int, list] = {}
        for f in self.files:
            for m in f["messages"]:
                by_id.setdefault(m.msg.frame_id | (1 << 31 if m.msg.is_extended_frame else 0), []).append(m)
                if m.j1939:
                    by_pgn.setdefault(m.pgn, []).append(m)
        self.by_id, self.by_pgn = by_id, by_pgn

    @staticmethod
    def _pick(cands: Optional[list], ch: int) -> Optional[DbcMessage]:
        if cands:
            for m in cands:
                if m.channel is None or m.channel == ch:
                    return m
        return None

    def find(self, ch: int, can_id: int, ext: bool, by_pgn: bool = True) -> Optional[DbcMessage]:
        """The message for a frame: exact id first, then (by_pgn, J1939 channels) a J1939 message with the same PGN."""
        m = self._pick(self.by_id.get(can_id | (1 << 31) if ext else can_id), ch)
        if m is None and ext and by_pgn and self.by_pgn:
            m = self._pick(self.by_pgn.get(split_id(can_id)[1]), ch)
        return m

    def find_pgn(self, ch: int, pgn: int) -> Optional[DbcMessage]:
        return self._pick(self.by_pgn.get(pgn), ch)

    def message(self, name: str) -> DbcMessage:
        for f in self.files:
            for m in f["messages"]:
                if m.name == name:
                    return m
        raise DbcError(f"DBC message {name!r} is not loaded")

    def encode(self, name: str, signals: dict) -> tuple[int, bool, bytes]:
        """(frame id, extended, data). Signals not given get a value that encodes: their first choice, or the
        offset clamped into the signal's range. Choice names are accepted for enum signals."""
        m = self.message(name).msg
        values = {s.name: _default_value(s) for s in m.signals}
        for k, v in (signals or {}).items():
            if k not in values:
                raise DbcError(f"{name} has no signal {k!r}")
            if isinstance(v, str):
                try:
                    v = float(v) if any(c in v for c in ".eE") else int(v, 0)
                except ValueError:
                    pass                           # a choice name: cantools maps it
            values[k] = v
        try:
            data = m.encode(m.gather_signals(values), scaling=True, strict=True)    # only the selected mux branch
        except Exception as exc:
            raise DbcError(f"cannot encode {name}: {exc}") from exc
        return m.frame_id, m.is_extended_frame, data

    def messages_info(self) -> list[dict]:
        return [m.info() for f in self.files for m in f["messages"]]
