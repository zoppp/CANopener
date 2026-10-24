"""One open CAN channel: the receive thread, the per-ID receive table, the trace ring and bus statistics.

Throughput design (a full 1 Mbit/s bus is ~8k frames/s):
- one thread per channel does nothing but bus.recv() and _ingest();
- _ingest() updates the per-ID row (count, data, cycle time) and writes the frame into a preallocated ring
  (a list and a write counter; readers copy slices, no lock on the hot path for the trace);
- the recorder and the taps (plugins, the CANopen bridge) get the frame through a queue/deque append;
- the UI never sees single frames: the server takes snapshots at ~10 Hz.

A frame everywhere outside python-can is a tuple ``(ts, can_id, flags, dlc, data)`` where ts is wall-clock
seconds (time.time() base), flags is a bit set of the F_* constants below and data is bytes.
"""

from __future__ import annotations

import collections
import itertools
import logging
import threading
import time
from typing import Callable, Optional

import can

from .adapters import BACKENDS, ChannelConfig, device_present, open_bus

log = logging.getLogger(__name__)

F_EXT, F_RTR, F_ERR, F_FD, F_BRS, F_ESI, F_TX = 1, 2, 4, 8, 16, 32, 64

QUIET_PROBE_S = 1.5             # bus silent this long: check the adapter is still plugged in
RETRY_S = 1.0                   # between attempts to reopen a lost adapter
SHUTDOWN_WAIT_S = 3.0           # a dead driver handle must not hang disconnect/reconnect
DRIVER_STATS_S = 1.0            # driver bus state / hardware load is polled this often, off the stats() path
DEFAULT_RING = 262_144          # trace frames kept per channel (~2 min at 2k frames/s)
_serials = itertools.count(1)


def msg_flags(msg: can.Message) -> int:
    f = 0
    if msg.is_extended_id:
        f |= F_EXT
    if msg.is_remote_frame:
        f |= F_RTR
    if msg.is_error_frame:
        f |= F_ERR
    if msg.is_fd:
        f |= F_FD
        if msg.bitrate_switch:
            f |= F_BRS
        if msg.error_state_indicator:
            f |= F_ESI
    return f


def frame_to_message(can_id: int, flags: int, data: bytes, dlc: Optional[int] = None,
                     ts: float = 0.0, channel=None) -> can.Message:
    rtr = bool(flags & F_RTR)
    return can.Message(
        timestamp=ts, arbitration_id=can_id, is_extended_id=bool(flags & F_EXT),
        is_remote_frame=rtr, is_error_frame=bool(flags & F_ERR), is_fd=bool(flags & F_FD),
        bitrate_switch=bool(flags & F_BRS), error_state_indicator=bool(flags & F_ESI),
        dlc=dlc if (rtr and dlc is not None) else None, data=b"" if rtr else data,
        is_rx=not (flags & F_TX), channel=channel, check=False)


def frame_bits(flags: int, dlc: int, data_len: int, fd_ratio: float) -> float:
    """Approximate bus time of one frame in nominal bit times (incl. typical stuffing and the 3-bit IFS)."""
    n = 0 if flags & F_RTR else data_len
    if flags & F_FD:
        arb = 49 if flags & F_EXT else 29
        data_phase = 8 * n + (21 if n > 16 else 17) + 10
        return arb + 10 + (data_phase / fd_ratio if flags & F_BRS else data_phase)
    if flags & F_EXT:
        return 67 + 8 * n + (54 + 8 * n) // 5
    return 47 + 8 * n + (34 + 8 * n) // 5


class IdRow:
    """Receive-table row for one identifier on one channel."""
    __slots__ = ("can_id", "flags", "dlc", "data", "prev", "count", "t_first", "t_last",
                 "cycle", "cmin", "cmax", "seq")

    def __init__(self, can_id: int, flags: int):
        self.can_id = can_id
        self.flags = flags
        self.dlc = 0
        self.data = b""
        self.prev = b""
        self.count = 0
        self.t_first = 0.0
        self.t_last = 0.0
        self.cycle = 0.0
        self.cmin = 0.0
        self.cmax = 0.0
        self.seq = 0             # change counter, lets readers send only rows that changed


class Tap:
    """A bounded, non-blocking copy of the frame stream for one consumer (plugin, decoder, CANopen bridge).

    The receive thread never waits on a tap: when it is full the oldest frame is dropped and counted.
    Consumers call drain() (or get() with a timeout) from their own thread."""

    def __init__(self, maxlen: Optional[int] = 50_000, include_tx: bool = True,
                 accept: Optional[Callable[[int, int], bool]] = None):
        self.buf: collections.deque = collections.deque(maxlen=maxlen)
        self.maxlen = maxlen
        self.include_tx = include_tx
        self.accept = accept
        self.dropped = 0
        self._event = threading.Event()

    def push(self, item) -> None:
        buf = self.buf
        if self.maxlen is not None and len(buf) >= self.maxlen:
            self.dropped += 1
        buf.append(item)
        self._event.set()

    def drain(self, limit: int = 0) -> list:
        out = []
        pop = self.buf.popleft
        try:
            while not limit or len(out) < limit:
                out.append(pop())
        except IndexError:
            self._event.clear()
            if self.buf:                     # a frame arrived between the last pop and clear()
                self._event.set()
        return out

    def get(self, timeout: Optional[float] = None):
        """One frame, or None after `timeout` seconds."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            try:
                return self.buf.popleft()
            except IndexError:
                self._event.clear()
                if self.buf:
                    continue
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._event.wait(remaining)


class Channel:
    """An open adapter channel. Thread-safe: send() from any thread; snapshots from the server thread."""

    def __init__(self, index: int, cfg: ChannelConfig, *, ring_size: int = DEFAULT_RING,
                 bus: Optional[can.BusABC] = None, name: str = ""):
        self.index = index
        self.serial = next(_serials)              # unique per Channel object (id() is reused by CPython)
        self.cfg = cfg
        self.name = name or f"CAN{index + 1}"
        self._bus: Optional[can.BusABC] = bus
        self._given_bus = bus is not None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._poll_stop = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._hw: dict = {}                       # last driver stats, written only by the poller
        self._send_lock = threading.Lock()
        # receive table, ring and counters; re-entrant because send() holds it around bus.send() + _ingest(),
        # so a reply can never enter the trace before the request that caused it
        self._lock = threading.RLock()
        self._ring_size = ring_size
        self._ring: list = [None] * ring_size
        self.written = 0                          # total frames ever written into the ring
        self.rows: dict[int, IdRow] = {}
        self.taps: list[Tap] = []
        self.recorders: list = []                 # objects with put(ch_index, frame)
        self._ts_offset: Optional[float] = None
        self.t_open = 0.0
        self._row_seq = 0                         # only ever increases (incremental snapshots rely on it)
        self.generation = 0                       # +1 on clear(): readers drop what they have
        self.state = "closed"                     # closed | open | error
        self.error = ""
        self.reconnects = 0                       # times the adapter came back after being unplugged
        self._reset_counters()

    # ------------------------------------------------------------------ lifecycle
    def _reset_counters(self) -> None:
        self.rx_frames = 0
        self.tx_frames = 0
        self.err_frames = 0
        self.tx_errors = 0
        self.rx_errors = 0
        self.bits = 0.0
        self._samples: collections.deque = collections.deque(maxlen=64)   # (t, frames, bits, errs)
        self.history: collections.deque = collections.deque(maxlen=600)   # per-second (t, fps, load%)
        self.peak_load = 0.0
        self.peak_fps = 0.0
        self._last_hist_t = 0.0

    @property
    def fd_ratio(self) -> float:
        if self.cfg.fd and self.cfg.bitrate:
            return max(1.0, self.cfg.data_bitrate / self.cfg.bitrate)
        return 1.0

    def open(self) -> "Channel":
        if self._bus is None:
            self._bus = open_bus(self.cfg)
        self._stop.clear()
        self.t_open = time.time()
        self._ts_offset = None
        self.state, self.error = "open", ""
        self._thread = threading.Thread(target=self._run, name=f"rx-{self.name}", daemon=True)
        self._thread.start()
        self._poll_stop = threading.Event()       # fresh: an abandoned poller from a hung close() stays stopped
        self._poll_thread = threading.Thread(target=self._poll_driver, name=f"hw-{self.name}", daemon=True)
        self._poll_thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        self._poll_stop.set()
        if self._thread is not None:
            self._thread.join(2.0)
            self._thread = None
        if self._poll_thread is not None:
            self._poll_thread.join(0.2)           # may be inside a hung driver call: daemon, abandoned
            self._poll_thread = None
        self._hw = {}
        with self._send_lock:                     # never shut the bus down under a send in progress
            bus, self._bus = self._bus, None
        if bus is not None and not self._given_bus:
            self._shutdown_bus(bus)
        self.state = "closed"

    def _shutdown_bus(self, bus) -> None:
        """bus.shutdown() on a worker thread: on an unplugged adapter some drivers raise, some block."""
        def work():
            try:
                bus.shutdown()
            except Exception as exc:
                log.warning("%s: shutdown failed: %s", self.name, exc)
        t = threading.Thread(target=work, name=f"shutdown-{self.name}", daemon=True)
        t.start()
        t.join(SHUTDOWN_WAIT_S)
        if t.is_alive():
            log.warning("%s: shutdown did not finish in %.0f s; abandoned", self.name, SHUTDOWN_WAIT_S)

    def _lose_bus(self, why: str) -> None:
        """The adapter went away: drop the dead bus, keep the channel (the receive thread reopens it)."""
        with self._send_lock:
            bus, self._bus = self._bus, None
        self._hw = {}                             # do not show the dead bus's values as live
        self.state, self.error = "error", why
        log.warning("%s: %s", self.name, why)
        if bus is not None:
            self._shutdown_bus(bus)

    def _try_reopen(self) -> bool:
        if device_present(self.cfg) is False:
            return False
        try:
            bus = open_bus(self.cfg)
        except Exception as exc:
            self.error = f"adapter lost, reopen failed: {type(exc).__name__}: {exc}"
            return False
        with self._send_lock:
            if self._stop.is_set():               # closed while we were opening
                stale = bus
            else:
                stale, self._bus = None, bus
        if stale is not None:
            self._shutdown_bus(stale)
            return False
        self._ts_offset = None
        self.state, self.error = "open", ""
        self.reconnects += 1
        log.info("%s: adapter back, channel reopened", self.name)
        return True

    def reopen(self, bitrate: Optional[int] = None, data_bitrate: Optional[int] = None) -> None:
        """Close and open again, optionally at another bit rate (bit-rate change of a node)."""
        if self._given_bus:
            raise RuntimeError("this channel wraps a bus it did not open; it cannot reopen it")
        self.close()
        if bitrate:
            self.cfg.bitrate = int(bitrate)
        if data_bitrate:
            self.cfg.data_bitrate = int(data_bitrate)
        self.open()

    @property
    def is_open(self) -> bool:
        return self.state == "open" and self._bus is not None

    # ------------------------------------------------------------------ receive
    def _run(self) -> None:
        consecutive = 0
        host_clock = self.cfg.interface in ("gs_usb", "slcan")   # device counter wraps / no hw stamps
        win_min, win_start = float("inf"), time.time()
        last_rx = time.time()
        can_reopen = not self._given_bus
        while not self._stop.is_set():
            bus = self._bus
            if bus is None:                       # adapter lost: wait for it to come back
                if not can_reopen:
                    return
                if self._stop.wait(RETRY_S):
                    return
                if self._try_reopen():
                    consecutive, last_rx = 0, time.time()
                continue
            try:
                msg = bus.recv(0.05)
            except Exception as exc:              # driver errors (bus off, unplugged adapter, ...)
                if self._bus is not bus:
                    continue
                consecutive += 1
                self.rx_errors += 1
                self.error = f"{type(exc).__name__}: {exc}"
                if consecutive in (1, 20) or consecutive % 500 == 0:
                    log.warning("%s: receive error #%d: %s", self.name, consecutive, exc)
                if consecutive >= 20:
                    self.state = "error"
                    if can_reopen and consecutive == 20:
                        self._lose_bus(f"adapter lost ({self.error})")
                        continue
                self._stop.wait(0.5 if consecutive >= 20 else 0.02)
                continue
            if msg is None and can_reopen and time.time() - last_rx > QUIET_PROBE_S:
                last_rx = time.time() - QUIET_PROBE_S + 1.0   # probe again in ~1 s, not on every poll
                if device_present(self.cfg) is False:
                    self._lose_bus("adapter unplugged; waiting for it to come back")
                    continue
            elif msg is not None:
                last_rx = time.time()
            if consecutive:
                consecutive = 0
                if self.state == "error":
                    self.state, self.error = "open", ""
            if msg is None:
                continue
            now = time.time()
            ts = msg.timestamp
            if host_clock or not ts:
                ts = now
            else:
                # Hardware time + offset, so cycle times keep the adapter's precision while staying on the same
                # clock as Tx frames (stamped with time.time()). The offset follows the smallest observed
                # latency and resyncs on jumps (counter wrap, adapter reset).
                d = now - ts
                off = self._ts_offset
                if off is None or abs(d - off) > 2.0:
                    off = self._ts_offset = d
                elif d < off:
                    off = self._ts_offset = d
                win_min = min(win_min, d)
                if now - win_start > 10.0:            # let the offset rise again with clock drift
                    if win_min != float("inf"):
                        off = self._ts_offset = win_min
                    win_min, win_start = float("inf"), now
                ts = ts + off
            self._ingest(ts, msg.arbitration_id, msg_flags(msg), msg.dlc, bytes(msg.data))

    def _ingest(self, ts: float, can_id: int, flags: int, dlc: int, data: bytes) -> None:
        frame = (ts, can_id, flags, dlc, data)
        bits = frame_bits(flags, dlc, len(data), self.fd_ratio)
        with self._lock:
            w = self.written
            self._ring[w % self._ring_size] = frame
            self.written = w + 1
            self.bits += bits
            if flags & F_ERR:
                self.err_frames += 1
            else:
                if flags & F_TX:
                    self.tx_frames += 1
                else:
                    self.rx_frames += 1
                key = can_id | ((flags & (F_EXT | F_RTR | F_TX)) << 29)
                row = self.rows.get(key)
                if row is None:
                    row = self.rows[key] = IdRow(can_id, flags)
                    row.t_first = ts
                else:
                    c = ts - row.t_last
                    row.cycle = c
                    if row.count == 1 or c < row.cmin:
                        row.cmin = c
                    if c > row.cmax:
                        row.cmax = c
                row.count += 1
                row.t_last = ts
                row.flags = flags
                row.dlc = dlc
                if data != row.data:
                    row.prev = row.data
                    row.data = data
                self._row_seq += 1
                row.seq = self._row_seq
        for rec in self.recorders:
            rec.put(self.index, frame)
        for tap in self.taps:
            if (flags & F_TX) and not tap.include_tx:
                continue
            try:
                if tap.accept is not None and not tap.accept(can_id, flags):
                    continue
                tap.push(frame)
            except Exception:                     # a broken plugin filter must never stop the receive thread
                log.exception("%s: a tap failed and was removed", self.name)
                self.remove_tap(tap)
                self.error = "a plugin frame filter failed and was removed (see the log)"

    # ------------------------------------------------------------------ transmit
    def send(self, msg: can.Message, timeout: Optional[float] = None) -> float:
        """Send one frame; it is also entered into the trace / receive table / recorder as Tx.

        Returns the timestamp. Raises can.CanError (or ValueError) when the adapter refuses it."""
        if not self.is_open:
            raise can.CanOperationError(f"{self.name} is not connected")
        if self.cfg.listen_only:
            raise can.CanOperationError(f"{self.name} is listen-only; reconnect without listen-only to transmit")
        data = bytes(msg.data)
        flags = msg_flags(msg) | F_TX
        dlc = msg.dlc
        with self._send_lock:
            bus = self._bus
            if bus is None:
                raise can.CanOperationError(f"{self.name} is not connected")
            # timeout None: queue the frame (Kvaser would otherwise wait for an ACK while holding the lock)
            with self._lock:
                ts = time.time()                  # before the send: the frame cannot be on the bus earlier
                try:
                    bus.send(msg, timeout)
                except Exception:
                    self.tx_errors += 1
                    raise
                self._ingest(ts, msg.arbitration_id, flags, dlc, data)
        return ts

    # ------------------------------------------------------------------ taps
    def add_tap(self, tap: Tap) -> Tap:
        self.taps = self.taps + [tap]           # copy-on-write: the receive thread iterates without a lock
        return tap

    def remove_tap(self, tap: Tap) -> None:
        self.taps = [t for t in self.taps if t is not tap]

    def add_recorder(self, rec) -> None:
        self.recorders = self.recorders + [rec]

    def remove_recorder(self, rec) -> None:
        self.recorders = [r for r in self.recorders if r is not rec]

    # ------------------------------------------------------------------ readers
    def trace_since(self, pos: int, limit: int = 0) -> tuple[int, list, int]:
        """Frames written after position `pos`: (new_pos, frames, skipped).

        `skipped` counts frames that were already overwritten in the ring (reader too slow) or left out
        because of `limit` (the newest `limit` frames are returned)."""
        w = self.written
        start = max(pos, w - self._ring_size + 1024)    # keep clear of the slots being overwritten
        skipped = max(0, start - pos)
        if limit and w - start > limit:
            skipped += (w - limit) - start
            start = w - limit
        n = self._ring_size
        ring = self._ring
        frames = [ring[i % n] for i in range(start, w)]
        return w, frames, skipped

    def trace_tail(self, count: int) -> list:
        w = self.written
        start = max(0, w - min(count, self._ring_size - 1024))
        n = self._ring_size
        return [self._ring[i % n] for i in range(start, w)]

    def rows_snapshot(self, since_seq: int = 0) -> tuple[int, list]:
        """(seq, rows changed after since_seq) as compact lists for the UI."""
        with self._lock:
            seq = self._row_seq
            rows = [(k, r.can_id, r.flags, r.dlc, r.data, r.prev, r.count, r.t_first, r.t_last,
                     r.cycle, r.cmin, r.cmax) for k, r in self.rows.items() if r.seq > since_seq]
        return seq, rows

    def rows_where(self, keep: Callable[[int], bool]) -> dict[int, tuple]:
        """{key: (data, t_last, cycle, count)} of the receive-table rows whose key (id | flags << 29) passes keep."""
        with self._lock:
            return {k: (r.data, r.t_last, r.cycle, r.count) for k, r in self.rows.items() if keep(k)}

    def clear(self) -> None:
        """Clear the receive table and counters (the trace ring keeps running)."""
        with self._lock:
            self.rows.clear()
            self._reset_counters()
            self.generation += 1

    def stats(self) -> dict:
        now = time.time()
        with self._lock:
            frames = self.rx_frames + self.tx_frames
            sample = (now, frames, self.bits, self.err_frames)
        self._samples.append(sample)
        # rate over the last ~1 s of samples
        old = sample
        for s in self._samples:
            if now - s[0] <= 1.05:
                old = s
                break
        dt = now - old[0]
        fps = (frames - old[1]) / dt if dt > 0.2 else 0.0
        bps = (sample[2] - old[2]) / dt if dt > 0.2 else 0.0
        eps = (sample[3] - old[3]) / dt if dt > 0.2 else 0.0
        load = 100.0 * bps / self.cfg.bitrate if self.cfg.bitrate else 0.0
        if dt > 0.2:
            self.peak_load = max(self.peak_load, load)
            self.peak_fps = max(self.peak_fps, fps)
        if now - self._last_hist_t >= 1.0:
            self._last_hist_t = now
            self.history.append((round(now, 1), round(fps, 1), round(load, 2)))
        out = {
            "index": self.index, "name": self.name, "state": self.state, "error": self.error,
            "config": self.cfg.as_dict(), "backend": BACKENDS[self.cfg.interface].title,
            "rx": self.rx_frames, "tx": self.tx_frames, "err_frames": self.err_frames,
            "tx_errors": self.tx_errors, "rx_errors": self.rx_errors,
            "fps": round(fps, 1), "load": round(load, 2), "eps": round(eps, 1),
            "peak_load": round(self.peak_load, 2), "peak_fps": round(self.peak_fps, 1),
            "ids": len(self.rows), "written": self.written, "t_open": self.t_open,
            "taps_dropped": sum(t.dropped for t in self.taps), "reconnects": self.reconnects,
        }
        out.update(self._hw)
        return out

    def _poll_driver(self) -> None:
        """Poller thread: driver calls (bus.state, Kvaser get_stats) can take seconds, so they never run in stats()."""
        stop = self._poll_stop
        while not stop.is_set():
            bus = self._bus                       # read fresh: the receive thread drops and reopens it
            if bus is not None:
                out = self._driver_stats(bus)
                if self._bus is bus and not stop.is_set():
                    self._hw = out
            stop.wait(DRIVER_STATS_S)

    def _driver_stats(self, bus) -> dict:
        out = {}
        try:
            st = bus.state
            out["bus_state"] = st.name if hasattr(st, "name") else str(st)
        except Exception:
            pass
        if hasattr(bus, "get_stats"):              # Kvaser: hardware bus load and overruns
            try:
                s = bus.get_stats()
                out["hw_overruns"] = int(getattr(s, "overruns", 0))
                out["hw_load"] = round(getattr(s, "busLoad", 0) / 100.0, 2)
            except Exception:
                pass
        return out
