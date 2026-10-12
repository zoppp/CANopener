"""Node sequences: explicit steps that the node workspace previews, confirms, runs and logs.

Built-in recipes (recipes.json) are compiled into these steps, and the custom mode for unknown devices
edits them directly, so the preview always shows exactly what will be sent.

A step is a dict with an "op" and its fields. Values may use placeholders: $NEW_ID, $OLD_ID, $BAUD (kbit/s),
$BAUD_INDEX (CiA 305 table index), $VENDOR, $PRODUCT, $REVISION, $SERIAL, $NODE, plus any variable the caller
passes. Inside a raw frame's data, "$NEW_ID" is replaced with the value as one hex byte.

  sdo_write   node, index, sub, type, value         SDO download (expedited when it fits 4 bytes)
  sdo_read    node, index, sub, type, [expect]       SDO upload; with "expect" the value must match
  store       node, [target="0x1010:01"]             SDO 0x1010 'save'
  nmt         command (start|stop|preop|reset_node|reset_comm or a number), node (0 = all)
  raw         id, data, [ext], [fd], [brs]            one CAN frame
  lss         command, ...                            switch_global (state: waiting|configuration),
                                                      switch_selective (vendor, product, revision, serial),
                                                      configure_node_id (value), configure_bit_timing (kbit),
                                                      activate_bit_timing (delay_ms), store, inquire_node_id
  delay       ms
  reopen      kbit                                    reopen the adapter at another bit rate
  wait_node   node, timeout_s                         node answers (heartbeat/boot-up or SDO 0x1000)
  wait_traffic timeout_s                              any frame arrives

Common fields: "always": true runs the step even after an earlier step failed (cleanup, e.g. LSS back to
waiting); "optional": true reports a failure as a warning instead of failing the sequence; "note": text.
"""

from __future__ import annotations

import hashlib
import json
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import canopen
from canopen.sdo import SdoAbortedError, SdoCommunicationError

from . import wire

LSS_TX = 0x7E5
LSS_RX = 0x7E4
BITRATE_TABLE = {1000: 0, 800: 1, 500: 2, 250: 3, 125: 4, 100: 5, 50: 6, 20: 7, 10: 8}
NMT_COMMANDS = {"start": 0x01, "stop": 0x02, "preop": 0x80, "reset_node": 0x81, "reset_comm": 0x82}

OPS = ("sdo_write", "sdo_read", "store", "nmt", "raw", "lss", "delay", "reopen", "wait_node", "wait_traffic")
LSS_COMMANDS = ("switch_global", "switch_selective", "configure_node_id", "configure_bit_timing",
                "activate_bit_timing", "store", "inquire_node_id")

_NOOP = lambda *a, **k: None  # noqa: E731


class SequenceError(Exception):
    pass


# ------------------------------------------------------------------------------------------- values

def resolve(value: Any, vars: dict) -> Any:
    """Substitute placeholders; a string that is only a number or a placeholder becomes an int."""
    if isinstance(value, str):
        s = value.strip()
        if s.startswith("$"):
            key = s[1:].upper()
            if key not in vars or vars[key] is None:
                raise SequenceError(f"{s} has no value (set it before previewing)")
            return vars[key]
        try:
            return wire.parse_int(s)
        except ValueError:
            return value
    return value


def _int(value: Any, vars: dict, what: str) -> int:
    v = resolve(value, vars)
    if isinstance(v, bool) or not isinstance(v, int):
        raise SequenceError(f"{what}: {value!r} is not a number")
    return v


def _raw_data(text: Any, vars: dict) -> bytes:
    if isinstance(text, (list, tuple)):
        return bytes(_int(b, vars, "data byte") & 0xFF for b in text)
    s = str(text or "")
    for key, val in sorted(vars.items(), key=lambda kv: -len(kv[0])):
        if isinstance(val, int) and f"${key}" in s.upper():
            idx = s.upper().find(f"${key}")
            while idx >= 0:
                s = s[:idx] + f" {val & 0xFF:02X} " + s[idx + len(key) + 1:]
                idx = s.upper().find(f"${key}")
    if "$" in s:
        raise SequenceError(f"unresolved placeholder in data {text!r}")
    from ..core.transmit import parse_data
    try:
        return parse_data(s)
    except ValueError as exc:
        raise SequenceError(str(exc)) from exc


def pack_value(type_name: str, value: Any, vars: dict) -> bytes:
    t = (type_name or "u8").lower()
    if t == "bytes":
        return _raw_data(value, vars)
    if t in ("str", "string", "visible_string"):
        return str(resolve(value, vars)).encode("latin-1")
    if t in ("f32", "real32"):
        return struct.pack("<f", float(resolve(value, vars)))
    if t in ("u64", "i64"):
        return struct.pack("<Q" if t == "u64" else "<q", _int(value, vars, "value"))
    v = _int(value, vars, "value")
    try:
        return wire.pack(t, v)
    except struct.error as exc:
        raise SequenceError(f"value {v} does not fit {t}") from exc


def unpack_value(type_name: str, data: bytes) -> Any:
    t = (type_name or "bytes").lower()
    if t in ("str", "string", "visible_string"):
        return data.decode("latin-1", errors="replace").rstrip("\x00")
    if t == "bytes":
        return data.hex(" ").upper()
    if t in ("f32", "real32"):
        return struct.unpack_from("<f", data.ljust(4, b"\x00"))[0]
    fmt = {"u8": "<B", "u16": "<H", "u32": "<I", "i8": "<b", "i16": "<h", "i32": "<i", "u64": "<Q", "i64": "<q"}[t]
    return struct.unpack_from(fmt, data.ljust(struct.calcsize(fmt), b"\x00"))[0]


def _index_sub(step: dict, vars: dict) -> tuple[int, int]:
    index = _int(step.get("index"), vars, "index")
    sub = _int(step.get("sub", 0), vars, "sub")
    if not (0 <= index <= 0xFFFF and 0 <= sub <= 0xFF):
        raise SequenceError(f"object 0x{index:X}:{sub} out of range")
    return index, sub


def _node(step: dict, vars: dict, allow_zero: bool = False) -> int:
    n = _int(step.get("node", "$NODE" if "NODE" in vars else "$OLD_ID"), vars, "node")
    lo = 0 if allow_zero else 1
    if not (lo <= n <= 127):
        raise SequenceError(f"node id {n} out of range {lo}..127")
    return n


# ------------------------------------------------------------------------------------------- preview

def _hex(b: bytes) -> str:
    return b.hex(" ").upper()


def _lss_bytes(cmd: str, step: dict, vars: dict) -> list[tuple[bytes, str]]:
    z = b"\x00" * 7
    if cmd == "switch_global":
        state = str(step.get("state", "waiting")).lower()
        code = {"waiting": 0, "configuration": 1, "config": 1}.get(state)
        if code is None:
            raise SequenceError(f"LSS switch_global: unknown state {state!r}")
        return [(bytes([0x04, code]) + z[:6], f"switch global -> {state}")]
    if cmd == "switch_selective":
        out = []
        for cs, key, ph in ((0x40, "vendor", "$VENDOR"), (0x41, "product", "$PRODUCT"),
                            (0x42, "revision", "$REVISION"), (0x43, "serial", "$SERIAL")):
            v = _int(step.get(key, ph), vars, key) & 0xFFFFFFFF
            out.append((bytes([cs]) + struct.pack("<I", v) + b"\x00" * 3, f"switch selective {key} 0x{v:08X}"))
        return out
    if cmd == "configure_node_id":
        v = _int(step.get("value", "$NEW_ID"), vars, "node id")
        if not (1 <= v <= 127 or v == 255):
            raise SequenceError(f"LSS node id {v} out of range 1..127")
        return [(bytes([0x11, v]) + z[:6], f"configure node id {v}")]
    if cmd == "configure_bit_timing":
        kbit = _int(step.get("kbit", "$BAUD"), vars, "kbit")
        if kbit not in BITRATE_TABLE:
            raise SequenceError(f"{kbit} kbit/s is not in the CiA 305 table {sorted(BITRATE_TABLE)}")
        return [(bytes([0x13, 0, BITRATE_TABLE[kbit]]) + z[:5], f"configure bit timing {kbit} kbit/s")]
    if cmd == "activate_bit_timing":
        d = _int(step.get("delay_ms", 500), vars, "delay_ms")
        return [(bytes([0x15]) + struct.pack("<H", d) + b"\x00" * 5, f"activate bit timing, switch delay {d} ms")]
    if cmd == "store":
        return [(bytes([0x17]) + z, "store configuration")]
    if cmd == "inquire_node_id":
        return [(bytes([0x5E]) + z, "inquire node id")]
    raise SequenceError(f"unknown LSS command {cmd!r}; use one of {', '.join(LSS_COMMANDS)}")


def _sdo_download_frames(node: int, index: int, sub: int, data: bytes) -> list[dict]:
    if not data:
        raise SequenceError("empty value: an SDO write needs at least one byte")
    mux = struct.pack("<HB", index, sub)
    if len(data) <= 4:
        cs = 0x23 | ((4 - len(data)) << 2)
        return [{"id": 0x600 + node, "data": _hex(bytes([cs]) + mux + data.ljust(4, b"\x00")),
                 "note": f"expedited download, {len(data)} byte(s)"}]
    out = [{"id": 0x600 + node, "data": _hex(bytes([0x21]) + mux + struct.pack("<I", len(data))),
            "note": f"segmented download initiate, {len(data)} bytes"}]
    toggle = 0
    for i in range(0, len(data), 7):
        chunk = data[i:i + 7]
        last = i + 7 >= len(data)
        cs = (toggle << 4) | ((7 - len(chunk)) << 1) | (1 if last else 0)
        out.append({"id": 0x600 + node, "data": _hex(bytes([cs]) + chunk.ljust(7, b"\x00")), "note": "segment"})
        toggle ^= 1
    return out


def preview_step(step: dict, vars: dict) -> dict:
    """{"text", "frames": [{"id", "data", "note"}], "expect"} for one step. Raises SequenceError if invalid."""
    op = step.get("op")
    frames: list[dict] = []
    expect = ""
    if op == "sdo_write":
        node = _node(step, vars)
        index, sub = _index_sub(step, vars)
        data = pack_value(step.get("type", "u8"), step.get("value"), vars)
        frames = _sdo_download_frames(node, index, sub, data)
        text = f"SDO write node {node} 0x{index:04X}:{sub:02X} = {_hex(data)} ({step.get('type', 'u8')})"
        expect = (f"0x{0x580 + node:03X} 60 ... (download response); without an answer the SDO client sends an "
                  f"abort 0x{0x600 + node:03X} 80 ...")
    elif op == "sdo_read":
        node = _node(step, vars)
        index, sub = _index_sub(step, vars)
        frames = [{"id": 0x600 + node, "data": _hex(bytes([0x40]) + struct.pack("<HB", index, sub) + b"\x00" * 4),
                   "note": "upload request"}]
        text = f"SDO read node {node} 0x{index:04X}:{sub:02X} as {step.get('type', 'bytes')}"
        if "expect" in step:
            text += f", must equal {resolve(step['expect'], vars)!r}"
        expect = (f"0x{0x580 + node:03X} 4x ... (upload response); without an answer the SDO client sends an "
                  f"abort 0x{0x600 + node:03X} 80 ...")
    elif op == "store":
        node = _node(step, vars)
        index, sub = wire.parse_store_target(str(step.get("target", "0x1010:01")))
        frames = _sdo_download_frames(node, index, sub, wire.pack("u32", wire.STORE_SAVE_VALUE))
        text = f"Store parameters: node {node} 0x{index:04X}:{sub:02X} = 'save'"
        expect = f"0x{0x580 + node:03X} 60 ...; without an answer an abort 0x{0x600 + node:03X} 80 ..."
    elif op == "nmt":
        cmd = step.get("command", "reset_node")
        if isinstance(cmd, str) and cmd.strip().lower() in NMT_COMMANDS:
            code = NMT_COMMANDS[cmd.strip().lower()]
        else:
            try:
                code = _int(cmd, vars, "NMT command")
            except SequenceError:
                raise SequenceError(f"unknown NMT command {cmd!r}; use one of {', '.join(NMT_COMMANDS)}") from None
        node = _node(step, vars, allow_zero=True)
        frames = [{"id": 0x000, "data": _hex(bytes([code, node])), "note": "NMT"}]
        text = f"NMT {cmd} -> {'all nodes' if node == 0 else f'node {node}'}"
    elif op == "raw":
        can_id = _int(step.get("id"), vars, "id")
        data = _raw_data(step.get("data", ""), vars)
        ext = bool(step.get("ext", can_id > 0x7FF))
        fd, brs = bool(step.get("fd")), bool(step.get("fd") and step.get("brs"))
        from ..core.transmit import build_message
        try:                                       # exactly what goes out (CAN FD pads to a valid length)
            data = bytes(build_message(can_id, data, ext=ext, fd=fd, brs=brs).data)
        except ValueError as exc:
            raise SequenceError(str(exc)) from exc
        frames = [{"id": can_id, "data": _hex(data), "ext": ext, "fd": fd, "brs": brs, "note": "raw"}]
        text = (f"Raw frame 0x{can_id:X}{' (ext)' if ext else ''}{' FD' if fd else ''}{' BRS' if brs else ''}: "
                f"{_hex(data) or '(no data)'}")
    elif op == "lss":
        cmd = str(step.get("command", ""))
        parts = _lss_bytes(cmd, step, vars)
        frames = [{"id": LSS_TX, "data": _hex(b), "note": n} for b, n in parts]
        text = "LSS " + "; ".join(n for _, n in parts)
        if cmd in ("configure_node_id", "configure_bit_timing", "store", "inquire_node_id") or cmd == "switch_selective":
            expect = f"0x{LSS_RX:03X} reply"
    elif op == "delay":
        ms = _int(step.get("ms", 100), vars, "ms")
        if not (0 <= ms <= 60_000):
            raise SequenceError("delay must be 0..60000 ms")
        text = f"Wait {ms} ms"
    elif op == "reopen":
        kbit = _int(step.get("kbit", "$BAUD"), vars, "kbit")
        if kbit * 1000 not in (10_000, 20_000, 50_000, 83_333, 100_000, 125_000, 250_000, 500_000, 800_000,
                               1_000_000) and kbit not in BITRATE_TABLE:
            raise SequenceError(f"{kbit} kbit/s is not a supported bit rate")
        text = f"Reopen the adapter at {kbit} kbit/s"
    elif op == "wait_node":
        node = _node(step, vars)
        text = f"Check node {node} answers (heartbeat/boot-up or SDO 0x1000), up to {step.get('timeout_s', 5)} s"
        frames = [{"id": 0x600 + node, "data": _hex(bytes([0x40, 0x00, 0x10, 0x00]) + bytes(4)),
                   "note": "SDO read 0x1000, repeated until the node answers (an abort follows each timeout)"}]
    elif op == "wait_traffic":
        text = f"Check traffic arrives, up to {step.get('timeout_s', 5)} s"
    else:
        raise SequenceError(f"unknown step op {op!r}; use one of {', '.join(OPS)}")
    flags = [f for f in ("always", "optional") if step.get(f)]
    return {"op": op, "text": text, "frames": frames, "expect": expect, "flags": flags,
            "note": step.get("note", "")}


def preview(steps: list[dict], vars: dict) -> list[dict]:
    out = []
    for i, step in enumerate(steps, 1):
        try:
            out.append(preview_step(step, vars))
        except SequenceError as exc:
            raise SequenceError(f"step {i} ({step.get('op')}): {exc}") from exc
    return out


def plan_token(steps: list[dict], vars: dict, channel_index: int) -> str:
    blob = json.dumps({"steps": steps, "vars": vars, "ch": channel_index}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


# ------------------------------------------------------------------------------------------- execution

@dataclass
class Context:
    network: canopen.Network
    vars: dict
    send_raw: Callable                       # (can_id, data, ext, fd, brs) -> None
    reopen: Optional[Callable[[int], None]] = None      # kbit -> None
    traffic_seen: Optional[Callable[[float], bool]] = None   # timeout -> bool
    log: Callable[[str], None] = _NOOP
    sdo_timeout: float = 1.0
    stop: Optional[Callable[[], bool]] = None


@dataclass
class StepResult:
    index: int
    op: str
    text: str
    status: str                 # ok | failed | warn | skipped
    detail: str = ""
    value: Any = None

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class RunResult:
    ok: bool
    steps: list = field(default_factory=list)
    error: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "error": self.error, "steps": [s.as_dict() for s in self.steps]}

    def value_of(self, op: str):
        for s in reversed(self.steps):
            if s.op == op:
                return s
        return None


def _sdo_node(ctx: Context, node_id: int):
    net = ctx.network
    if node_id in net:
        del net[node_id]
    node = net.add_node(node_id, None)
    node.sdo.RESPONSE_TIMEOUT = ctx.sdo_timeout
    return node


def _wait_node(ctx: Context, node_id: int, timeout: float) -> bool:
    net = ctx.network
    net.scanner.reset()
    deadline = time.monotonic() + timeout
    node = _sdo_node(ctx, node_id)
    try:
        while time.monotonic() < deadline:
            if node_id in net.scanner.nodes:
                ctx.log(f"node {node_id}: heartbeat/boot-up/traffic seen")
                return True
            node.sdo.RESPONSE_TIMEOUT = max(0.05, min(0.5, deadline - time.monotonic()))
            try:
                node.sdo.upload(0x1000, 0)
                ctx.log(f"node {node_id}: answered SDO 0x1000")
                return True
            except SdoAbortedError:
                ctx.log(f"node {node_id}: SDO abort on 0x1000 (present)")
                return True
            except SdoCommunicationError:
                continue
        return False
    finally:
        del net[node_id]


def _run_lss(ctx: Context, cmd: str, step: dict, vars: dict) -> Any:
    lss = ctx.network.lss
    if cmd == "switch_global":
        state = str(step.get("state", "waiting")).lower()
        lss.send_switch_state_global(lss.CONFIGURATION_STATE if state.startswith("config") else lss.WAITING_STATE)
    elif cmd == "switch_selective":
        vals = [_int(step.get(k, ph), vars, k) for k, ph in (("vendor", "$VENDOR"), ("product", "$PRODUCT"),
                                                             ("revision", "$REVISION"), ("serial", "$SERIAL"))]
        if lss.send_switch_state_selective(*vals) is False:
            raise SequenceError("no device answered the selective switch with the expected reply")
    elif cmd == "configure_node_id":
        lss.configure_node_id(_int(step.get("value", "$NEW_ID"), vars, "node id"))
    elif cmd == "configure_bit_timing":
        lss.configure_bit_timing(BITRATE_TABLE[_int(step.get("kbit", "$BAUD"), vars, "kbit")])
    elif cmd == "activate_bit_timing":
        lss.activate_bit_timing(_int(step.get("delay_ms", 500), vars, "delay_ms"))
    elif cmd == "store":
        lss.store_configuration()
    elif cmd == "inquire_node_id":
        return lss.inquire_node_id()
    return None


def _run_step(ctx: Context, step: dict, vars: dict) -> Any:
    op = step["op"]
    if op == "sdo_write":
        node_id = _node(step, vars)
        index, sub = _index_sub(step, vars)
        data = pack_value(step.get("type", "u8"), step.get("value"), vars)
        node = _sdo_node(ctx, node_id)
        try:
            node.sdo.download(index, sub, data)
        finally:
            del ctx.network[node_id]
        return None
    if op == "sdo_read":
        node_id = _node(step, vars)
        index, sub = _index_sub(step, vars)
        node = _sdo_node(ctx, node_id)
        try:
            data = node.sdo.upload(index, sub)
        finally:
            del ctx.network[node_id]
        value = unpack_value(step.get("type", "bytes"), data)
        if "expect" in step:
            want = resolve(step["expect"], vars)
            if value != want:
                raise SequenceError(f"read {value!r}, expected {want!r}")
        return value
    if op == "store":
        node_id = _node(step, vars)
        index, sub = wire.parse_store_target(str(step.get("target", "0x1010:01")))
        node = _sdo_node(ctx, node_id)
        try:
            node.sdo.download(index, sub, wire.pack("u32", wire.STORE_SAVE_VALUE))
        finally:
            del ctx.network[node_id]
        return None
    if op in ("nmt", "raw"):
        for fr in preview_step(step, vars)["frames"]:
            ctx.send_raw(fr["id"], bytes.fromhex(fr["data"]), fr.get("ext", False), fr.get("fd", False),
                         fr.get("brs", False))
        return None
    if op == "lss":
        return _run_lss(ctx, str(step.get("command")), step, vars)
    if op == "delay":
        end = time.monotonic() + _int(step.get("ms", 100), vars, "ms") / 1000.0
        while time.monotonic() < end:
            if ctx.stop and ctx.stop() and not step.get("nostop"):
                raise SequenceError("stopped")
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))
        return None
    if op == "reopen":
        if ctx.reopen is None:
            raise SequenceError("this adapter cannot be reopened from here")
        ctx.reopen(_int(step.get("kbit", "$BAUD"), vars, "kbit"))
        return None
    if op == "wait_node":
        node_id = _node(step, vars)
        if not _wait_node(ctx, node_id, float(step.get("timeout_s", 5))):
            raise SequenceError(f"node {node_id} did not answer within {step.get('timeout_s', 5)} s")
        return True
    if op == "wait_traffic":
        if ctx.traffic_seen is None:
            raise SequenceError("no traffic check available")
        if not ctx.traffic_seen(float(step.get("timeout_s", 5))):
            raise SequenceError(f"no traffic within {step.get('timeout_s', 5)} s")
        return True
    raise SequenceError(f"unknown op {op!r}")


def execute(steps: list[dict], ctx: Context) -> RunResult:
    """Run the steps in order. After a failure, a stop request or Ctrl+C, only steps marked "always" still run
    (an interrupt is raised again after them). Steps marked "nostop" ignore a stop request: the part of a
    bit-rate change after activate, which must reach the reopen."""
    texts = preview(steps, ctx.vars)              # validates everything before the first frame is sent
    result = RunResult(ok=True)
    failed = False
    interrupt: Optional[BaseException] = None
    for i, (step, pv) in enumerate(zip(steps, texts), 1):
        if failed and not step.get("always"):
            result.steps.append(StepResult(i, step["op"], pv["text"], "skipped"))
            continue
        if not failed and ctx.stop and ctx.stop() and not step.get("always") and not step.get("nostop"):
            failed, result.ok, result.error = True, False, "stopped by the user"
            result.steps.append(StepResult(i, step["op"], pv["text"], "skipped"))
            continue
        ctx.log(f"step {i}: {pv['text']}")
        try:
            value = _run_step(ctx, step, ctx.vars)
            shown = "" if value is None else (f"= {value} (0x{value:X})" if isinstance(value, int)
                                              and not isinstance(value, bool) else f"= {value!r}")
            result.steps.append(StepResult(i, step["op"], pv["text"], "ok", value=value, detail=shown))
            if value is not None and step["op"] in ("sdo_read", "lss"):
                ctx.log(f"  -> {value!r}")
        except BaseException as exc:              # SDO abort/timeout, LSS no reply, adapter error, Ctrl+C
            if not isinstance(exc, Exception):
                interrupt = interrupt or exc
            detail = str(exc) if isinstance(exc, SequenceError) else f"{type(exc).__name__}: {exc}"
            if step.get("optional") and interrupt is None:
                ctx.log(f"  warning: {detail}")
                result.steps.append(StepResult(i, step["op"], pv["text"], "warn", detail))
                continue
            ctx.log(f"  FAILED: {detail}")
            result.steps.append(StepResult(i, step["op"], pv["text"], "failed", detail))
            if not failed:
                failed, result.ok, result.error = True, False, f"step {i} ({pv['text']}): {detail}"
    if interrupt is not None:
        raise interrupt
    return result
