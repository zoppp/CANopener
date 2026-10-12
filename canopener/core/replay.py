"""Replay a recorded file onto a channel with the original timing (or faster / slower)."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from .channel import F_ERR, F_TX, frame_to_message
from .recorder import read_frames

log = logging.getLogger(__name__)


class Player:
    """Plays a file in a thread. send(channel_index, message) is supplied by the engine.

    bus_map: {bus number in the file: channel index}; frames of other buses are skipped.
    only: "all" | "rx" | "tx" - which recorded direction to send."""

    def __init__(self, path, send: Callable, bus_map: dict, *, speed: float = 1.0, loop: bool = False,
                 only: str = "all"):
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(f"{self.path} not found")
        if speed <= 0:
            raise ValueError("speed must be > 0")
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
        while not self._stop.is_set():
            if not frames:
                break
            t_file0 = frames[0][1][0]
            t_wall0 = time.perf_counter()
            paused_for = 0.0
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
