"""Adapter bench: check the adapter table's claims on a real bus, one adapter at a time.

Two modes:

Paired (an isolated bench bus): a REFERENCE adapter and the adapter under test (DUT) on the same two-wire bus,
terminated (120 ohm at each end; one is enough for a short bench below 1 Mbit/s). Nothing else powered on the bus:
the listen-only check relies on the DUT being the only other node that could acknowledge. Every frame is checked
from both sides, so a failure names what was lost, changed or refused.

    python tests/hardware/bench_adapters.py --ref kvaser:0 --dut pcan:PCAN_USBBUS1
    python tests/hardware/bench_adapters.py --ref kvaser:0 --dut slcan:COM5 --fd
    python tests/hardware/bench_adapters.py --ref pcan:PCAN_USBBUS1 --dut kvaser:0
    python tests/hardware/bench_adapters.py --ref kvaser:0 --dut obdlink:COM7 --rates 500k --bitrate 500k

Live (no reference, e.g. an OBDLink in a vehicle): listen-only receive, statistics, close/reopen. Sends one OBD-II
request only with --obd-request (and a typed yes).

    python tests/hardware/bench_adapters.py --ref none --dut obdlink:COM7 --bitrate 500k --seconds 60

A report (markdown + JSON) goes to --out (default bench-results/). Exit code 1 when any check failed.
`--ref virtual:x --dut virtual:x` runs everything on the virtual bus (the self-test does that).
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import can

import canopener
from canopener.core import adapters
from canopener.core.adapters import BACKENDS, BITRATES, ChannelConfig, backend_bitrate
from canopener.core.channel import F_BRS, F_ERR, F_EXT, F_FD, F_RTR, Channel, Tap
from canopener.core.engine import Engine
from canopener.core.transmit import FD_LENGTHS, TxEntry

REF, DUT = 0, 1
# Frame kinds a backend refuses by design (the adapter table says so): not counted as failures.
DOCUMENTED_REFUSALS = {"obdlink": {"rtr"}}
SLOW_TX = {"obdlink"}                      # each send stops receiving: small counts, slow cycles


@dataclass
class Result:
    check: str
    status: str                            # PASS | FAIL | SKIP | INFO
    detail: str = ""
    data: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------------------------- helpers

def parse_spec(text: str) -> Optional[tuple[str, Optional[str]]]:
    if text.lower() == "none":
        return None
    iface, _, chan = text.partition(":")     # first colon only: socket://host:port stays whole
    if iface not in BACKENDS:
        raise SystemExit(f"unknown interface {iface!r}; choose one of {', '.join(BACKENDS)}")
    return iface, (chan or None)


def parse_rate(text: str) -> int:
    t = text.strip().lower()
    if t in ("83.3k", "83.333k", "83k3"):
        return 83_333
    mult = 1_000_000 if t.endswith("m") else 1_000 if t.endswith("k") else 1
    return int(float(t.rstrip("mk")) * mult)


def kind(flags: int, dlc: int, data: bytes) -> tuple:
    rtr = bool(flags & F_RTR)
    return (bool(flags & F_EXT), rtr, bool(flags & F_FD), bool(flags & F_BRS), dlc if rtr else len(data),
            b"" if rtr else bytes(data))


def frame_key(f) -> tuple:
    _, can_id, flags, dlc, data = f
    return (can_id,) + kind(flags, dlc, data)


def msg_key(m: can.Message) -> tuple:
    return (m.arbitration_id, m.is_extended_id, m.is_remote_frame, m.is_fd, m.bitrate_switch,
            m.dlc if m.is_remote_frame else len(m.data), b"" if m.is_remote_frame else bytes(m.data))


def show(key: tuple) -> str:
    can_id, ext, rtr, fd, brs, n, data = key
    ident = f"{can_id:08X}" if ext else f"{can_id:03X}"
    tag = "RTR" if rtr else ("FD BRS" if brs else "FD") if fd else ""
    return f"{ident} {tag} [{n}] {data.hex(' ').upper()}".replace("  ", " ")


def classic_set() -> list[can.Message]:
    """Every DLC, both ID lengths, boundary IDs, remote frames and bit patterns."""
    m = can.Message
    out = [m(arbitration_id=0x100 + n, is_extended_id=False, data=bytes(range(0x10 * n, 0x10 * n + n)))
           for n in range(9)]
    out += [m(arbitration_id=0x18FF0000 + n, is_extended_id=True, data=bytes(0xF0 - i for i in range(n)))
            for n in range(9)]
    out += [m(arbitration_id=0x000, is_extended_id=False, data=b"\x00" * 8),
            m(arbitration_id=0x7FF, is_extended_id=False, data=b"\xFF" * 8),
            m(arbitration_id=0x00000000, is_extended_id=True, data=b"\x55" * 8),
            m(arbitration_id=0x1FFFFFFF, is_extended_id=True, data=b"\xAA" * 8),
            m(arbitration_id=0x701, is_extended_id=False, is_remote_frame=True, dlc=0),
            m(arbitration_id=0x702, is_extended_id=False, is_remote_frame=True, dlc=8),
            m(arbitration_id=0x18EA00FF, is_extended_id=True, is_remote_frame=True, dlc=3)]
    return out


def fd_set() -> list[can.Message]:
    out = []
    for i, n in enumerate(FD_LENGTHS):
        data = bytes((i * 7 + j) & 0xFF for j in range(n))
        out.append(can.Message(arbitration_id=0x200 + i, is_extended_id=False, is_fd=True,
                               bitrate_switch=bool(i % 2), data=data))
        out.append(can.Message(arbitration_id=0x19000000 + i, is_extended_id=True, is_fd=True,
                               bitrate_switch=not i % 2, data=data))
    out.append(can.Message(arbitration_id=0x2FF, is_extended_id=False, data=b"\x01\x02\x03"))   # classic on FD
    return out


def msg_kind(m: can.Message) -> str:
    return "rtr" if m.is_remote_frame else "fd" if m.is_fd else "data"


def send_all(ch: Channel, msgs: list[can.Message], retry_s: float = 0.25) -> tuple[list, list]:
    """Send each frame, retrying while the adapter's queue is full. Returns (sent, refused[(msg, error)])."""
    sent, refused = [], []
    for m in msgs:
        deadline = time.monotonic() + retry_s
        while True:
            try:
                ch.send(m)
                sent.append(m)
                break
            except (can.CanError, ValueError) as exc:
                if time.monotonic() > deadline:
                    refused.append((m, f"{type(exc).__name__}: {exc}"))
                    break
                time.sleep(0.001)
    return sent, refused


def collect(tap: Tap, expected: int, timeout: float, settle: float = 0.4) -> list:
    """Frames from a tap until `expected` arrived and nothing more came for `settle` s, or `timeout` s."""
    got, end, last = [], time.monotonic() + timeout, time.monotonic()
    while time.monotonic() < end:
        new = tap.drain()
        if new:
            got += [f for f in new if not f[2] & F_ERR]
            last = time.monotonic()
        elif len(got) >= expected and time.monotonic() - last > settle:
            break
        else:
            time.sleep(0.01)
    return got


def compare(sent: list[can.Message], got: list) -> tuple[bool, str, dict]:
    want = [msg_key(m) for m in sent]
    ids = {k[0] for k in want}
    have = [frame_key(f) for f in got if f[1] in ids]          # ignore foreign traffic
    if have == want:
        return True, f"{len(want)} frames, all exact and in order", {}
    missing = list(want)
    extra = []
    for k in have:
        if k in missing:
            missing.remove(k)
        else:
            extra.append(k)
    in_order = [k for k in have if k in want] == [k for k in want if k in have]
    detail = f"sent {len(want)}, got {len(have)}: {len(missing)} missing, {len(extra)} changed/extra"
    if not in_order:
        detail += ", order differs"
    return False, detail, {"missing": [show(k) for k in missing[:20]], "extra": [show(k) for k in extra[:20]]}


def new_tap(ch: Channel) -> Tap:
    return ch.add_tap(Tap(maxlen=None, include_tx=False))


# ---------------------------------------------------------------------------------------------- bench

class Bench:
    def __init__(self, args):
        self.a = args
        self.eng = Engine()
        self.results: list[Result] = []
        self.ref_spec = parse_spec(args.ref)
        self.dut_spec = parse_spec(args.dut)
        if self.dut_spec is None:
            raise SystemExit("--dut is required")
        self.dut_iface = self.dut_spec[0]
        self.virtual = self.dut_iface == "virtual"
        self.foreign: set = set()

    # ---------------------------------------------------------------- plumbing
    def add(self, check, status, detail="", **data) -> Result:
        r = Result(check, status, detail, data)
        self.results.append(r)
        print(f"  [{status:4}] {check}: {detail}", flush=True)
        return r

    def cfg(self, spec, bitrate, *, listen_only=False, fd=False) -> ChannelConfig:
        iface, chan = spec
        return ChannelConfig(iface, chan, bitrate=bitrate, fd=fd, data_bitrate=self.a.data_bitrate,
                             listen_only=listen_only)

    def open(self, index, spec, bitrate, **kw) -> Channel:
        return self.eng.connect(self.cfg(spec, bitrate, **kw), index)

    def ch(self, index) -> Channel:
        return self.eng.channel(index)

    def both_accept(self, rate) -> Optional[str]:
        for spec in (self.ref_spec, self.dut_spec):
            try:
                backend_bitrate(spec[0], rate)
            except ValueError as exc:
                return str(exc)
        return None

    def adapter_info(self, index) -> str:
        bus = self.ch(index)._bus
        info = getattr(bus, "channel_info", "") or ""
        dev = getattr(bus, "device", "")
        return f"{info} {dev}".strip()

    # ---------------------------------------------------------------- checks
    def check_detect(self) -> None:
        iface, chan = self.dut_spec
        if iface in ("obdlink", "gvret", "virtual"):
            self.add("detect", "SKIP", f"{iface} is not in the device list by design (type the channel)")
            return
        want = str(adapters.DEFAULT_CHANNELS[iface] if chan is None else chan).upper()
        found = [d for d in adapters.detect_devices(include_virtual=False) if d.interface == iface]
        hit = [d for d in found if str(d.channel).upper() == want]
        if hit:
            self.add("detect", "PASS", f"listed as {hit[0].label!r} {hit[0].detail}".strip(),
                     label=hit[0].label, info=hit[0].detail)
        else:
            self.add("detect", "FAIL", f"channel {want} not in the device list; {iface} entries: "
                     f"{[d.label for d in found] or 'none'}")

    def check_open_close(self, rate) -> bool:
        try:
            for _ in range(3):
                self.open(DUT, self.dut_spec, rate)
                self.eng.disconnect(DUT)
            self.open(DUT, self.dut_spec, rate)
        except Exception as exc:
            self.add("open / close x3", "FAIL", str(exc))
            return False
        self.add("open / close x3", "PASS", self.adapter_info(DUT) or "opened")
        return True

    def check_quiet(self) -> None:
        taps = (new_tap(self.ch(REF)), new_tap(self.ch(DUT)))
        time.sleep(1.0)
        seen = {f[1] for t in taps for f in t.drain() if not f[2] & F_ERR}
        for t, i in zip(taps, (REF, DUT)):
            self.ch(i).remove_tap(t)
        self.foreign = seen
        if seen:
            self.add("bus quiet", "INFO", f"other traffic on the bench bus: {sorted(hex(x) for x in seen)[:10]}"
                     "; the listen-only check will be skipped")
        else:
            self.add("bus quiet", "PASS", "no other traffic in 1 s")

    def check_direction(self, name, src, dst, msgs) -> None:
        """src sends msgs, dst must receive them exactly. Documented refusals are allowed."""
        tap = new_tap(self.ch(dst))
        try:
            sent, refused = send_all(self.ch(src), msgs)
            got = collect(tap, len(sent), timeout=3.0 + len(sent) * (0.25 if self.dut_iface in SLOW_TX else 0.01))
        finally:
            self.ch(dst).remove_tap(tap)
        allowed = DOCUMENTED_REFUSALS.get(self.dut_iface, set()) if src == DUT else set()
        bad_refusals = [(m, e) for m, e in refused if msg_kind(m) not in allowed]
        ok, detail, extra = compare(sent, got)
        if refused:
            detail += f"; {len(refused)} refused by the sender" + (
                f" ({len(refused) - len(bad_refusals)} documented)" if allowed else "")
            extra["refused"] = [f"{show(msg_key(m))}: {e}" for m, e in refused[:10]]
        self.add(name, "PASS" if ok and not bad_refusals else "FAIL", detail, **extra)

    def check_rates(self, rates) -> None:
        for rate in rates:
            label = f"{rate / 1000:g} kbit/s"
            why = self.both_accept(rate)
            if why:
                self.add(f"{label}", "SKIP", why)
                continue
            try:
                self.ch(REF).reopen(bitrate=rate)
                self.ch(DUT).reopen(bitrate=rate)
            except Exception as exc:
                self.add(f"{label} open", "FAIL", f"{type(exc).__name__}: {exc}")
                continue
            self.check_direction(f"{label} REF -> DUT (receive)", REF, DUT, classic_set())
            self.check_direction(f"{label} DUT -> REF (send)", DUT, REF, classic_set())

    def flood(self, name, src, dst, *, seconds=None, count=None) -> None:
        """src sends numbered frames as fast as it can; dst must get every one, in order."""
        tap = new_tap(self.ch(dst))
        sch = self.ch(src)
        n, refused, t0 = 0, 0, time.perf_counter()
        try:
            while (count is not None and n < count) or (seconds is not None and time.perf_counter() - t0 < seconds):
                m = can.Message(arbitration_id=0x123, is_extended_id=False, data=n.to_bytes(4, "little") + b"\xA5" * 4)
                s, r = send_all(sch, [m], retry_s=1.0)
                if r:
                    refused += 1
                    break
                n += 1
            elapsed = time.perf_counter() - t0
            got = collect(tap, n, timeout=5.0 + (n * 0.3 if self.dut_iface in SLOW_TX else 0))
        finally:
            self.ch(dst).remove_tap(tap)
        seqs = [int.from_bytes(f[4][:4], "little") for f in got if f[1] == 0x123 and len(f[4]) == 8]
        unique = set(seqs)
        missing = n - len(unique & set(range(n)))
        disorder = sum(1 for a, b in zip(seqs, seqs[1:]) if b <= a)
        fps = n / elapsed if elapsed else 0.0
        rate = self.ch(src).cfg.bitrate
        load = 100.0 * fps * 114 / rate          # 8-byte standard frame without stuff bits, + 3 bit interframe
        data = {"sent": n, "received": len(seqs), "missing": missing, "duplicates": len(seqs) - len(unique),
                "out_of_order": disorder, "fps": round(fps), "approx_load_pct": round(load, 1),
                "dst_stats": self.ch(dst).stats()}
        detail = (f"{n} frames at {fps:.0f}/s (~{load:.0f}% load at {rate // 1000}k): {missing} missing, "
                  f"{data['duplicates']} duplicated, {disorder} out of order")
        if refused:
            detail += "; the sender stopped accepting frames"
        ok = not missing and not disorder and not data["duplicates"] and not refused
        overruns = getattr(self.ch(dst)._bus, "overruns", 0)
        if not ok and dst == DUT and self.dut_iface == "obdlink" and overruns:
            # documented: a busy bus overflows the dongle, CANopener restarts it and counts it
            self.add(name, "INFO", detail + f"; {overruns} BUFFER FULL restarts counted (documented)", **data)
            return
        self.add(name, "PASS" if ok else "FAIL", detail, **data)

    def check_load(self, rates) -> None:
        rate = max(r for r in rates if not self.both_accept(r)) if self.dut_iface not in SLOW_TX else self.a.bitrate
        self.ch(REF).reopen(bitrate=rate)
        self.ch(DUT).reopen(bitrate=rate)
        slow = self.dut_iface in SLOW_TX
        self.flood("burst REF -> DUT (1000 back to back)", REF, DUT, count=1000)
        self.flood(f"sustained REF -> DUT ({self.a.load_seconds:g} s flat out)", REF, DUT,
                   seconds=self.a.load_seconds)
        self.flood(f"burst DUT -> REF ({20 if slow else 1000} back to back)", DUT, REF, count=20 if slow else 1000)
        if not slow:
            self.flood(f"sustained DUT -> REF ({self.a.load_seconds:g} s flat out)", DUT, REF,
                       seconds=self.a.load_seconds)
        self.ch(REF).reopen(bitrate=self.a.bitrate)
        self.ch(DUT).reopen(bitrate=self.a.bitrate)

    def check_transmit_list(self) -> None:
        period = 250.0 if self.dut_iface in SLOW_TX else 10.0
        run_s = max(3.0, 20 * period / 1000)
        tap = new_tap(self.ch(REF))
        try:
            once = self.eng.tx.add(TxEntry(can_id=0x322, data="01 02", period_ms=0, channel=DUT))
            self.eng.tx.send_once(once.id)
            single = [f for f in collect(tap, 1, timeout=2.0) if f[1] == 0x322]
            self.add("transmit list: single frame", "PASS" if len(single) == 1 else "FAIL",
                     f"REF received {len(single)} (want 1)")
            self.eng.tx.remove(once.id)

            cyc = self.eng.tx.add(TxEntry(can_id=0x321, data="DE AD BE EF", period_ms=period, enabled=True,
                                          channel=DUT))
            time.sleep(run_s)
            self.eng.tx.update(cyc.id, {"enabled": False})
            t_stop = time.time()
            frames = [f for f in collect(tap, 1, timeout=1.0, settle=0.5) if f[1] == 0x321]
            self.eng.tx.remove(cyc.id)
        finally:
            self.ch(REF).remove_tap(tap)
        late = [f for f in frames if f[0] > t_stop + 0.05 + 2 * period / 1000]
        ts = [f[0] for f in frames]
        gaps = [(b - a) * 1000 for a, b in zip(ts, ts[1:])]
        if len(gaps) < 5:
            self.add(f"transmit list: cyclic {period:g} ms", "FAIL", f"only {len(frames)} frames received")
            return
        median = statistics.median(gaps)
        want = run_s * 1000 / period
        data = {"frames": len(frames), "expected": round(want), "median_ms": round(median, 3),
                "mean_ms": round(statistics.mean(gaps), 3), "stdev_ms": round(statistics.pstdev(gaps), 3),
                "min_ms": round(min(gaps), 3), "max_ms": round(max(gaps), 3), "after_stop": len(late)}
        # median and count, not mean: one OS stall must not fail the check (the worst gap is reported)
        ok = abs(median - period) <= 0.02 * period and len(frames) >= 0.95 * want and not late
        self.add(f"transmit list: cyclic {period:g} ms", "PASS" if ok else "FAIL",
                 f"{len(frames)} frames (expected ~{want:.0f}), median {median:.2f} ms (min {min(gaps):.2f}, "
                 f"max {max(gaps):.2f}, stdev {data['stdev_ms']:.2f}), {len(late)} after stop", **data)

    def check_listen_only(self) -> None:
        name = "listen-only sends nothing (no ACK)"
        if self.virtual or "virtual" in self.adapter_info(DUT).lower():
            self.add(name, "SKIP", "a virtual bus or driver has no ACK")
            return
        if not BACKENDS[self.dut_iface].listen_only:
            try:
                adapters.bus_kwargs(self.cfg(self.dut_spec, self.a.bitrate, listen_only=True))
                self.add(name, "FAIL", "the table says no listen-only, but opening listen-only was accepted")
            except ValueError as exc:
                self.add(name, "PASS", f"refused as documented: {exc}")
            return
        if self.foreign:
            self.add(name, "SKIP", "other traffic on the bus: another node may acknowledge")
            return

        def copies(listen_only: bool) -> tuple[int, dict]:
            dut = self.ch(DUT)
            dut.cfg.listen_only = listen_only
            dut.reopen()
            tap = new_tap(dut)
            try:
                self.ch(REF).send(can.Message(arbitration_id=0x555, is_extended_id=False, data=b"\xAC\x4B"))
                time.sleep(0.3)
                ref_stats = self.ch(REF).stats()
                n = sum(1 for f in tap.drain() if f[1] == 0x555)
            finally:
                dut.remove_tap(tap)
                self.ch(REF).reopen()               # aborts a frame the reference is still repeating
            return n, ref_stats

        silent, ref_silent = copies(True)
        refused = False
        try:
            self.ch(DUT).send(can.Message(arbitration_id=0x556, data=b"\x00"))
        except can.CanError:
            refused = True
        acked, ref_acked = copies(False)
        data = {"copies_listen_only": silent, "copies_normal": acked,
                "ref_state_listen_only": ref_silent.get("bus_state"), "ref_err_frames": ref_silent.get("err_frames"),
                "send_refused_while_listen_only": refused}
        if acked != 1:
            self.add(name, "FAIL", f"control failed: in normal mode the DUT saw {acked} copies (want 1); "
                     "is another node acknowledging, or the bus unterminated?", **data)
        elif silent == 0:
            self.add(name, "FAIL", "listen-only received nothing", **data)
        elif silent == 1:
            self.add(name, "FAIL", "the frame was acknowledged once: the DUT ACKs in listen-only", **data)
        else:
            self.add(name, "PASS" if refused else "FAIL",
                     f"REF repeated an unacknowledged frame {silent}x (DUT silent); normal mode: 1 copy"
                     + ("" if refused else "; but sending on the listen-only channel was NOT refused"), **data)

    def check_fd(self) -> None:
        for spec in (self.ref_spec, self.dut_spec):
            if not BACKENDS[spec[0]].fd:
                self.add("CAN FD", "SKIP", f"{BACKENDS[spec[0]].title} has no CAN FD in CANopener")
                return
        label = f"FD {self.a.bitrate // 1000}k / {self.a.data_bitrate / 1e6:g}M"
        try:
            self.open(REF, self.ref_spec, self.a.bitrate, fd=True)
            self.open(DUT, self.dut_spec, self.a.bitrate, fd=True)
        except Exception as exc:
            self.add(f"{label} open", "FAIL", str(exc))
            return
        self.add(f"{label} open", "PASS", f"{self.adapter_info(REF)} / {self.adapter_info(DUT)}")
        self.check_direction(f"{label} REF -> DUT", REF, DUT, fd_set())
        self.check_direction(f"{label} DUT -> REF", DUT, REF, fd_set())

    # ---------------------------------------------------------------- live mode (no reference)
    def run_live(self) -> None:
        a = self.a
        try:
            ch = self.open(DUT, self.dut_spec, a.bitrate, listen_only=BACKENDS[self.dut_iface].listen_only)
        except Exception as exc:
            self.add("open", "FAIL", str(exc))
            return
        self.add("open", "PASS", f"{self.adapter_info(DUT)} listen-only={ch.cfg.listen_only}")
        tap = new_tap(ch)
        t0 = time.time()
        while time.time() - t0 < a.seconds:
            time.sleep(1.0)
            st = ch.stats()
            print(f"    {time.time() - t0:5.0f} s  rx {st['rx']:7}  {st['fps']:6.0f}/s  ids {st['ids']:3}  "
                  f"err {st['err_frames']}  rx_errors {st['rx_errors']}  "
                  f"overruns {getattr(ch._bus, 'overruns', '-')}", flush=True)
        frames = tap.drain()
        ch.remove_tap(tap)
        st = ch.stats()
        ids = sorted({f[1] for f in frames})
        data = {"frames": len(frames), "ids": [hex(i) for i in ids[:50]], "stats": st,
                "overruns": getattr(ch._bus, "overruns", None), "bus_rx_errors": getattr(ch._bus, "rx_errors", None)}
        if frames:
            self.add(f"receive {a.seconds:g} s", "PASS",
                     f"{len(frames)} frames, {len(ids)} IDs, {st['err_frames']} error frames, "
                     f"{st['rx_errors']} receive errors, overruns {data['overruns']}", **data)
        else:
            self.add(f"receive {a.seconds:g} s", "FAIL", "nothing received (wrong bit rate, pins or no traffic?)",
                     **data)
        ok = 0
        for i in range(5):
            ch.reopen()
            tap = new_tap(ch)
            ok += bool(collect(tap, 1, timeout=2.0, settle=0.0)) if frames else 1
            ch.remove_tap(tap)
        self.add("close / reopen x5 while traffic", "PASS" if ok == 5 else "FAIL",
                 f"receiving resumed {ok}/5 times" if frames else "reopened 5x (no traffic to check)")
        if a.obd_request:
            self.obd_request()

    def obd_request(self) -> None:
        if not self.a.yes and input("  Send ONE OBD-II request 7DF 02 01 0C (engine rpm) onto this bus? "
                                    "type yes: ").strip().lower() != "yes":
            self.add("OBD request 7DF -> 7E8", "SKIP", "not confirmed")
            return
        ch = self.ch(DUT)
        ch.cfg.listen_only = False
        ch.reopen()
        tap = new_tap(ch)
        try:
            ch.send(can.Message(arbitration_id=0x7DF, is_extended_id=False, data=b"\x02\x01\x0C" + b"\x00" * 5))
            time.sleep(1.0)
            replies = [f for f in tap.drain() if 0x7E8 <= f[1] <= 0x7EF and f[4][1:3] == b"\x41\x0C"]
        except can.CanError as exc:
            self.add("OBD request 7DF -> 7E8", "FAIL", f"send failed: {exc}")
            return
        finally:
            ch.remove_tap(tap)
        if replies:
            f = replies[0]
            rpm = int.from_bytes(f[4][3:5], "big") / 4
            self.add("OBD request 7DF -> 7E8", "PASS", f"{len(replies)} replies, first from {f[1]:03X}: {rpm:.0f} rpm")
        else:
            self.add("OBD request 7DF -> 7E8", "FAIL", "no 41 0C reply within 1 s")

    # ---------------------------------------------------------------- top level
    def run_paired(self) -> None:
        a = self.a
        rates = list(BITRATES) if a.rates == "all" else [parse_rate(x) for x in a.rates.split(",")]
        if a.bitrate not in rates:
            rates.insert(0, a.bitrate)
        self.check_detect()
        try:
            self.open(REF, self.ref_spec, a.bitrate)
        except Exception as exc:
            self.add("reference open", "FAIL", str(exc))
            return
        self.add("reference open", "INFO", self.adapter_info(REF) or a.ref)
        if not self.check_open_close(a.bitrate):
            return
        self.check_quiet()
        self.check_rates(rates)
        self.check_load(rates)
        self.check_transmit_list()
        self.check_listen_only()
        if a.fd:
            self.check_fd()

    def run(self) -> list[Result]:
        try:
            if self.ref_spec is None:
                self.run_live()
            else:
                self.run_paired()
        finally:
            self.eng.shutdown()
        return self.results


# ---------------------------------------------------------------------------------------------- report

def write_report(args, results: list[Result], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    safe = lambda s: "".join(c if c.isalnum() else "-" for c in s)
    base = out_dir / f"{stamp}_{safe(args.dut)}_vs_{safe(args.ref)}"
    meta = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "dut": args.dut, "ref": args.ref,
            "bitrate": args.bitrate, "rates": args.rates, "fd": args.fd, "data_bitrate": args.data_bitrate,
            "canopener": canopener.__version__, "python_can": can.__version__,
            "python": platform.python_version(), "os": platform.platform(), "note": args.note}
    base.with_suffix(".json").write_text(json.dumps({"meta": meta, "results": [asdict(r) for r in results]},
                                                    indent=2, default=str), encoding="utf-8")
    counts = {s: sum(r.status == s for r in results) for s in ("PASS", "FAIL", "SKIP", "INFO")}
    lines = [f"# Adapter bench: {args.dut} (reference {args.ref})", "",
             *(f"- **{k}**: {v}" for k, v in meta.items() if v not in (None, "")), "",
             f"**{counts['PASS']} pass, {counts['FAIL']} fail, {counts['SKIP']} skipped, {counts['INFO']} info**",
             "", "| Check | Result | Detail |", "|---|---|---|"]
    lines += [f"| {r.check} | {r.status} | {r.detail.replace('|', '/')} |" for r in results]
    fails = [r for r in results if r.status == "FAIL" and r.data]
    if fails:
        lines += ["", "## Failure details", ""]
        for r in fails:
            lines += [f"### {r.check}", "", "```", json.dumps(r.data, indent=2, default=str), "```", ""]
    base.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return base.with_suffix(".md")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    p.add_argument("--dut", required=True, help="adapter under test, interface:channel (e.g. pcan:PCAN_USBBUS1)")
    p.add_argument("--ref", required=True, help="reference adapter interface:channel, or 'none' for live mode")
    p.add_argument("--bitrate", type=parse_rate, default=250_000, help="base bit rate (default 250k)")
    p.add_argument("--rates", default="125k,250k,500k,1M", help="rates to check both ways, or 'all'")
    p.add_argument("--fd", action="store_true", help="also check CAN FD (both adapters must have FD hardware)")
    p.add_argument("--data-bitrate", type=parse_rate, default=2_000_000, help="CAN FD data rate (default 2M)")
    p.add_argument("--load-seconds", type=float, default=5.0, help="length of each flat-out flood (default 5)")
    p.add_argument("--seconds", type=float, default=30.0, help="live mode: how long to listen (default 30)")
    p.add_argument("--obd-request", action="store_true", help="live mode: send one OBD-II rpm request (asks first)")
    p.add_argument("--note", default="", help="free text for the report (firmware, cable, termination, ...)")
    p.add_argument("--out", type=Path, default=Path("bench-results"), help="report folder (default bench-results)")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    a = p.parse_args(argv)
    if a.ref.lower() != "none" and not a.yes:
        print("Paired mode SENDS frames (floods up to 100% bus load). Use an isolated bench bus with only the\n"
              "reference and the adapter under test on it, terminated.")
        if input("Is this an isolated bench bus? [y/N] ").strip().lower() not in ("y", "yes"):
            return 2
    print(f"CANopener {canopener.__version__} adapter bench: DUT {a.dut}, reference {a.ref}")
    results = Bench(a).run()
    path = write_report(a, results, a.out)
    fails = [r for r in results if r.status == "FAIL"]
    print(f"\n{len(results) - len(fails)} ok/skip/info, {len(fails)} FAIL. Report: {path}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
