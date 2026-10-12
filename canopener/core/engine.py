"""The engine: every open channel plus the transmit list, the recorder and the player.

The server, the MCP tools, the node workspace and plugins all work through one Engine instance.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from pathlib import Path
from typing import Optional

import can

from .adapters import ChannelConfig
from .channel import Channel, Tap
from .recorder import Recorder, read_frames
from .replay import Player
from .transmit import TxEntry, TxScheduler, build_message

log = logging.getLogger(__name__)


class EngineError(Exception):
    """A request the engine refuses, with a message for the user."""


class _Bound:
    """Attaches one engine-level Tap to one channel: pushes (channel index, frame)."""
    __slots__ = ("tap", "index", "include_tx", "accept")

    def __init__(self, tap: Tap, index: int):
        self.tap, self.index = tap, index
        self.include_tx, self.accept = tap.include_tx, tap.accept

    @property
    def dropped(self) -> int:
        return self.tap.dropped                    # the engine-level tap's drops (shared by all channels)

    def push(self, frame) -> None:
        self.tap.push((self.index, frame))


class Engine:
    def __init__(self, ring_size: Optional[int] = None):
        self.channels: dict[int, Channel] = {}
        self._ring_size = ring_size
        self._lock = threading.RLock()
        self.tx = TxScheduler(self.send_message)
        self.tx.start()
        self.recorder: Optional[Recorder] = None
        self.last_recording: Optional[dict] = None
        self.player: Optional[Player] = None
        self.events: collections.deque = collections.deque(maxlen=500)   # (seq, ts, level, text)
        self._event_seq = 0
        self._listeners: list = []          # callables(kind, payload) for engine events (not frames)
        self.taps: list[Tap] = []           # engine-level taps: get (channel index, frame) from every channel

    # ------------------------------------------------------------------ events (user-visible log)
    def event(self, text: str, level: str = "info") -> None:
        self._event_seq += 1
        self.events.append((self._event_seq, time.time(), level, text))
        (log.warning if level in ("warn", "error") else log.info)(text)
        for fn in list(self._listeners):
            try:
                fn("event", {"level": level, "text": text})
            except Exception:
                log.exception("engine listener failed")

    def add_listener(self, fn) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    # ------------------------------------------------------------------ taps
    def add_tap(self, tap: Tap) -> Tap:
        """Frames of every live channel (also ones connected later) as (channel index, frame) tuples."""
        with self._lock:
            self.taps.append(tap)
            for ch in self.channels.values():
                ch.add_tap(_Bound(tap, ch.index))
        return tap

    def remove_tap(self, tap: Tap) -> None:
        with self._lock:
            if tap in self.taps:
                self.taps.remove(tap)
            for ch in self.channels.values():
                ch.taps = [t for t in ch.taps if not (isinstance(t, _Bound) and t.tap is tap)]

    # ------------------------------------------------------------------ channels
    def channel(self, index: int = 0) -> Channel:
        ch = self.channels.get(int(index))
        if ch is None:
            raise EngineError(f"channel {index + 1} is not connected")
        return ch

    def _free_index(self) -> int:
        i = 0
        while i in self.channels:
            i += 1
        return i

    def connect(self, cfg: ChannelConfig, index: Optional[int] = None, *, bus: Optional[can.BusABC] = None,
                name: str = "") -> Channel:
        with self._lock:
            if index is None:
                index = self._free_index()
            if index in self.channels:
                self.disconnect(index)
            kw = {"ring_size": self._ring_size} if self._ring_size else {}
            ch = Channel(index, cfg, bus=bus, name=name, **kw)
            try:
                ch.open()
            except Exception as exc:
                raise EngineError(f"could not open {cfg.interface} channel {cfg.channel!r}: "
                                  f"{type(exc).__name__}: {exc}") from exc
            for tap in self.taps:
                ch.add_tap(_Bound(tap, index))
            self.channels[index] = ch               # a running recording keeps the channels it started with
            shown = cfg.channel if cfg.channel not in (None, "") else "(default)"
            self.event(f"{ch.name}: connected {cfg.interface} {shown}"
                       f" at {cfg.bitrate // 1000} kbit/s" + (" FD" if cfg.fd else "")
                       + (" (listen-only)" if cfg.listen_only else ""))
            return ch

    def disconnect(self, index: int) -> None:
        with self._lock:
            ch = self.channels.pop(int(index), None)
            if ch is None:
                return
            for e in self.tx.entries:
                if e.channel == ch.index:
                    e.enabled = False
            if self.recorder is not None:
                ch.remove_recorder(self.recorder)
            ch.close()
            self.event(f"{ch.name}: disconnected")

    def disconnect_all(self) -> None:
        for i in list(self.channels):
            self.disconnect(i)

    def open_file_view(self, path, index: Optional[int] = None) -> list[Channel]:
        """Load a recorded file into offline channels (one per bus in the file) for browsing.

        The frames keep their recorded timestamps, so cycle times in the receive table are real."""
        p = Path(path)
        by_bus: dict[int, list] = {}
        for bus, fr in read_frames(p):
            by_bus.setdefault(bus, []).append(fr)
        if not by_bus:
            raise EngineError(f"{p.name} has no frames")
        made = []
        with self._lock:
            for bus in sorted(by_bus):
                frames = by_bus[bus]
                i = self._free_index() if index is None or made else index
                if i in self.channels:
                    self.disconnect(i)
                size = max(self._ring_size or 0, len(frames) + 2048, 4096)
                ch = Channel(i, ChannelConfig(interface="virtual", channel=p.name, bitrate=0), ring_size=size,
                             name=f"{p.stem}" + (f" bus {bus}" if len(by_bus) > 1 else ""))
                for fr in frames:
                    ch._ingest(*fr)
                ch.state = "file"
                ch.t_open = frames[0][0]
                self.channels[i] = ch
                made.append(ch)
        self.event(f"opened {p.name}: {sum(len(v) for v in by_bus.values())} frames on {len(made)} bus(es)")
        return made

    # ------------------------------------------------------------------ transmit
    def send_message(self, index: int, msg: can.Message) -> float:
        ch = self.channels.get(int(index))
        if ch is None or not ch.is_open:
            raise can.CanOperationError(f"channel {int(index) + 1} is not connected")
        return ch.send(msg)

    def send(self, index: int, can_id: int, data: bytes = b"", **kw) -> float:
        try:
            msg = build_message(can_id, data, **kw)
        except ValueError as exc:
            raise EngineError(str(exc)) from exc
        try:
            return self.send_message(index, msg)
        except can.CanError as exc:
            raise EngineError(str(exc)) from exc

    # ------------------------------------------------------------------ recording
    def start_recording(self, path, channels: Optional[list[int]] = None) -> Recorder:
        with self._lock:
            if self.recorder is not None:
                raise EngineError(f"already recording to {self.recorder.path.name}")
            idx = [i for i in (channels if channels else self.channels)
                   if i in self.channels and self.channels[i].state != "file"]
            if not idx:
                raise EngineError("no connected channel to record")
            bitrate = self.channels[idx[0]].cfg.bitrate
            try:
                rec = Recorder(path, {i: n + 1 for n, i in enumerate(sorted(idx))}, bitrate=bitrate)
            except (OSError, ValueError) as exc:
                raise EngineError(str(exc)) from exc
            for i in idx:
                self.channels[i].add_recorder(rec)
            self.recorder = rec
        self.event(f"recording to {rec.path}")
        return rec

    def stop_recording(self) -> Optional[dict]:
        with self._lock:
            rec, self.recorder = self.recorder, None
            if rec is None:
                return None
            for ch in self.channels.values():
                ch.remove_recorder(rec)
        rec.stop()
        st = rec.status()
        self.last_recording = st
        self.event(f"recording stopped: {st['written']} frames in {Path(st['path']).name}"
                   + (f" (error: {st['error']})" if st["error"] else ""), "warn" if st["error"] else "info")
        return st

    # ------------------------------------------------------------------ replay
    def start_replay(self, path, bus_map: Optional[dict] = None, *, speed: float = 1.0, loop: bool = False,
                     only: str = "all") -> Player:
        with self._lock:
            if self.player is not None and self.player.state in ("playing", "paused"):
                raise EngineError("a replay is already running; stop it first")
            live = sorted(i for i, c in self.channels.items() if c.is_open)
            if not live:
                raise EngineError("connect a channel to replay onto")
            if not bus_map:
                bus_map = {1: live[0]}
            bus_map = {int(k): int(v) for k, v in bus_map.items()}
            for v in bus_map.values():
                if v not in self.channels or not self.channels[v].is_open:
                    raise EngineError(f"channel {v + 1} is not connected")
            try:
                self.player = Player(path, self.send_message, bus_map, speed=speed, loop=loop, only=only)
            except (OSError, ValueError) as exc:
                raise EngineError(str(exc)) from exc
        self.event(f"replaying {Path(path).name} at {speed:g}x" + (" (loop)" if loop else ""))
        return self.player

    def stop_replay(self) -> Optional[dict]:
        p = self.player
        if p is None:
            return None
        p.stop()
        self.event(f"replay stopped after {p.sent} frames")
        return p.status()

    # ------------------------------------------------------------------ state
    def status(self) -> dict:
        return {
            "channels": [c.stats() for c in sorted(self.channels.values(), key=lambda c: c.index)],
            "recording": self.recorder.status() if self.recorder else None,
            "last_recording": self.last_recording,
            "replay": self.player.status() if self.player else None,
            "tx": [e.as_dict() for e in self.tx.entries],
        }

    def shutdown(self) -> None:
        try:
            self.tx.stop_all()
            self.tx.stop()
            if self.player is not None:
                self.player.stop()
            self.stop_recording()
        finally:
            self.disconnect_all()
