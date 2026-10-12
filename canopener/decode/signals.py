"""The signal store: decoded values by name, shared by decoders (DBC, CANopen, J1939, plugins) and consumers
(receive-table decode column, dashboards, MCP).

A signal key is "<source>/<group>/<name>", e.g. "dbc/EEC1/EngineSpeed", "canopen/5/TPDO1.Speed",
"j1939/0/61444.EngineSpeed". Decoders call publish() from their own threads (never from a receive thread);
readers take snapshots. Each signal keeps the last value and a bounded history for plots.
"""

from __future__ import annotations

import collections
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

HISTORY = 20_000          # points kept per signal (~3 min at 100 Hz)


@dataclass
class Signal:
    key: str
    unit: str = ""
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: dict = field(default_factory=dict)     # raw value -> text (enums)
    channel: int = 0
    can_id: Optional[int] = None
    ts: float = 0.0
    value: Any = None
    seq: int = 0
    count: int = 0
    history: collections.deque = field(default_factory=lambda: collections.deque(maxlen=HISTORY))

    def info(self) -> dict:
        return {"key": self.key, "unit": self.unit, "min": self.minimum, "max": self.maximum,
                "choices": {str(k): v for k, v in self.choices.items()}, "channel": self.channel,
                "can_id": self.can_id, "ts": self.ts, "value": self.value, "count": self.count}


class SignalStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._signals: dict[str, Signal] = {}
        self._seq = 0
        self.generation = 0                     # +1 on clear(): readers drop what they have

    def publish(self, key: str, ts: float, value: Any, *, unit: str = "", minimum: Optional[float] = None,
                maximum: Optional[float] = None, choices: Optional[dict] = None, channel: int = 0,
                can_id: Optional[int] = None) -> None:
        """Record one value. Numbers and bools go into the history (for plots); text only into the last value."""
        if isinstance(value, float) and not math.isfinite(value):
            value = None                           # NaN/inf (e.g. a factor overflow) would break JSON for every client
        with self._lock:
            s = self._signals.get(key)
            if s is None:
                s = self._signals[key] = Signal(key, unit, minimum, maximum, dict(choices or {}), channel, can_id)
            self._seq += 1
            s.ts, s.value, s.seq = ts, value, self._seq
            s.count += 1
            if isinstance(value, bool):
                s.history.append((ts, 1.0 if value else 0.0))
            elif isinstance(value, (int, float)):
                s.history.append((ts, float(value)))

    def changed(self, since_seq: int) -> tuple[int, list[dict]]:
        """(seq, [{key, ts, value}]) for signals updated after since_seq."""
        with self._lock:
            out = [{"key": s.key, "ts": round(s.ts, 6), "value": s.value} for s in self._signals.values()
                   if s.seq > since_seq]
            return self._seq, out

    def list(self, prefix: str = "") -> list[dict]:
        with self._lock:
            return [s.info() for k, s in sorted(self._signals.items()) if k.startswith(prefix)]

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            s = self._signals.get(key)
            return s.info() if s else None

    def history(self, key: str, seconds: float = 60.0, max_points: int = 5000) -> list[tuple[float, float]]:
        cutoff = time.time() - seconds
        with self._lock:
            s = self._signals.get(key)
            pts = [p for p in s.history if p[0] >= cutoff] if s else []
        if len(pts) > max_points:                      # thin evenly, keep the newest point
            step = len(pts) / max_points
            pts = [pts[int(i * step)] for i in range(max_points - 1)] + [pts[-1]]
        return pts

    def clear(self, prefix: str = "") -> None:
        with self._lock:
            for k in [k for k in self._signals if k.startswith(prefix)]:
                del self._signals[k]
            self.generation += 1
