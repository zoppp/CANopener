"""Replay a recorded file onto a channel with the original timing (or faster / slower)."""

from __future__ import annotations

import logging
import math
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .channel import F_ERR, F_TX, frame_to_message
from .recorder import read_frames

log = logging.getLogger(__name__)
MIN_LOOP_SPAN_S = 0.010          # a shorter file looped would be sent back to back (bus flood)
MAX_SPEED = 100.0                # the UI offers 0.5x..10x; API, MCP and CLI: finite and at most this


class Player:
    """Plays a file in a thread. send(channel_index, message) is supplied by the engine.

    bus_map: {bus number in the file: channel index}; frames of other buses are skipped.
    only: "all" | "rx" | "tx" - which recorded direction to send."""

    def __init__(self, path, send: Callable, bus_map: dict, *, speed: float = 1.0, loop: bool = False,
                 only: str = "all"):
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"{self.path} not found")
        if not (isinstance(speed, (int, float)) and math.isfinite(speed) and 0 < speed <= MAX_SPEED):
            raise ValueError(f"speed must be a number above 0 and at most {MAX_SPEED:g}")
        self._send = send
        self.bus_map = dict(bus_map)
        self.speed = speed
        self.loop = loop
        self.only = only
        self.sent = 0
        self.skipped = 0
        self.errors = 0
        self.last_error = ""
        self.loops = 0
        self.total = 0
        self.position_s = 0.0
        self.state = "playing"
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._thread = threading.Thread(target=self._run, name="replay", daemon=True)
        self._thread.start()

    def _frames(self) -> list:
        frames = [(b, f) for b, f in read_frames(self.path) if not f[2] & F_ERR]
        frames.sort(key=lambda bf: bf[1][0])
        return frames

    def _run(self) -> None:
        try:
            frames = self._frames()
        except Exception as exc:
            self.state, self.last_error = "error", f"{type(exc).__name__}: {exc}"
            log.exception("replay: cannot read %s", self.path)
            return
        self.total = len(frames)
        if not frames:
            self.state = "done"
            return
        t_file0 = frames[0][1][0]
        span = frames[-1][1][0] - t_file0
        if self.loop and span / self.speed < MIN_LOOP_SPAN_S:     # what one pass takes at this speed
            self.state = "error"
            self.last_error = (f"the file spans {span * 1000:.1f} ms ({span / self.speed * 1000:.1f} ms at "
                               f"{self.speed:g}x): looping it would send its frames back to back "
                               f"(minimum {MIN_LOOP_SPAN_S * 1000:.0f} ms); use the transmit list for a cyclic frame")
            return
        # a pass ends one typical frame gap after its last frame, as if the recording went on
        gaps = sorted(b[1][0] - a[1][0] for a, b in zip(frames, frames[1:]) if b[1][0] > a[1][0])
        gap = max(gaps[len(gaps) // 2], 0.001) if gaps else 0.001
        t_wall0 = time.perf_counter()
        paused_for = 0.0
        while not self._stop.is_set():
            for bus, fr in frames:
                if self._stop.is_set():
                    break
                if self._pause.is_set():
                    p0 = time.perf_counter()
                    self.state = "paused"
                    while self._pause.is_set() and not self._stop.is_set():
                        time.sleep(0.05)
                    paused_for += time.perf_counter() - p0
                    self.state = "playing"
                ts, can_id, flags, dlc, data = fr
                ch = self.bus_map.get(bus)
                is_tx = bool(flags & F_TX)
                if ch is None or (self.only == "rx" and is_tx) or (self.only == "tx" and not is_tx):
                    self.skipped += 1
                    continue
                due = t_wall0 + paused_for + (ts - t_file0) / self.speed
                delay = due - time.perf_counter()
                if delay > 0:
                    self._stop.wait(delay) if delay > 0.002 else time.sleep(delay)
                if self._stop.is_set():                 # stopped during the wait: never send this frame early
                    break
                self.position_s = ts - t_file0
                try:
                    self._send(ch, frame_to_message(can_id, flags & ~F_TX, data, dlc))
                    self.sent += 1
                except Exception as exc:
                    self.errors += 1
                    self.last_error = str(exc)
                    if "not connected" in self.last_error:
                        self._stop.set()
            if not self.loop:
                break
            self.loops += 1
            # the next pass starts one gap after this one's end, never sooner (no catch-up burst after a slow pass)
            t_wall0 = max(t_wall0 + (span + gap) / self.speed, time.perf_counter() - paused_for + gap / self.speed)
        if self.state != "error":
            self.state = "stopped" if self._stop.is_set() else "done"

    def pause(self, on: bool = True) -> None:
        (self._pause.set if on else self._pause.clear)()

    def stop(self) -> None:
        self._stop.set()
        self._pause.clear()
        self._thread.join(3)

    def status(self) -> dict:
        return {"path": str(self.path), "state": self.state, "sent": self.sent, "total": self.total,
                "skipped": self.skipped, "errors": self.errors, "last_error": self.last_error,
                "loops": self.loops, "loop": self.loop, "speed": self.speed, "position_s": round(self.position_s, 3),
                "bus_map": {str(k): v for k, v in self.bus_map.items()}, "only": self.only}
