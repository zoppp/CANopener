"""Device work for the EDS editor. It only reads: SDO uploads to compare a file with a node, read its values, or
build a document by probing it. Writing a DCF goes through the Nodes tab as custom steps (dcf_steps), which
show every frame and ask before anything is sent.

The functions take `upload(index, sub) -> bytes`, which raises Abort(code) for an SDO abort and NoAnswer when
the node does not answer, so they run the same against a real node and in the tests.
"""

from __future__ import annotations

import struct
from typing import Callable, Optional

from . import edsdoc as E

NO_OBJECT = 0x06020000          # object does not exist
NO_SUB = 0x06090011             # sub-index does not exist
WRITE_ONLY = 0x06010001
ABORT_TEXT = {NO_OBJECT: "object does not exist", NO_SUB: "sub-index does not exist",                # CiA 301
              WRITE_ONLY: "write only", 0x06010002: "read only", 0x06010000: "access not supported",
              0x05030000: "toggle bit not alternated", 0x05040000: "SDO timeout", 0x05040001: "unknown SDO command",
              0x05040005: "out of memory", 0x06040041: "cannot be mapped", 0x06040042: "PDO too long",
              0x06040043: "parameter incompatibility", 0x06040047: "internal incompatibility",
              0x06060000: "hardware error", 0x06070010: "length does not match", 0x06070012: "too long",
              0x06070013: "too short", 0x06090030: "value range exceeded", 0x06090031: "value too high",
              0x06090032: "value too low", 0x060A0023: "resource not available", 0x08000000: "general error",
              0x08000020: "cannot be transferred", 0x08000021: "local control", 0x08000022: "not in this device state",
              0x08000024: "no data available"}

# where "create from device" looks (CiA 301): communication area, manufacturer area, standard profile area
FULL_RANGES = ((0x1000, 0x1FFF), (0x2000, 0x5FFF), (0x6000, 0x9FFF))


class Abort(Exception):
    def __init__(self, code: int):
        super().__init__(f"SDO abort 0x{code:08X} ({ABORT_TEXT.get(code, 'device error')})")
        self.code = code


class NoAnswer(Exception):
    pass


class Stopped(Exception):
    pass


def abort_text(code: int) -> str:
    return f"0x{code:08X} {ABORT_TEXT.get(code, '')}".strip()


# ------------------------------------------------------------------------------------------- replies

class Reply(bytes):
    """SDO upload data; `sized` is False for an expedited reply without a size (always 4 bytes)."""

    def __new__(cls, data: bytes, sized: bool = True):
        obj = super().__new__(cls, data)
        obj.sized = sized
        return obj


def fit(data: bytes, dt: int) -> bytes:
    """Cut a reply to the size of its data type when the device sent more than that without saying so (an
    expedited reply without a size has 4 bytes) or padded it with zeros, as canopen does with a known type."""
    size = E.DATA_TYPES.get(dt, ("", "", 0))[2] // 8
    if size and len(data) > size and (not getattr(data, "sized", True) or not any(data[size:])):
        return Reply(bytes(data[:size]), True)
    return data


# SDO aborts that mean "the object is there, but you can't read it now"; every other abort counts as missing
EXISTS = {WRITE_ONLY, 0x06010000, 0x08000020, 0x08000021, 0x08000022, 0x08000024}


def _reader(upload: Callable, where: str):
    """upload() that retries a timeout once and gives up after three timeouts in a row."""
    misses = 0

    def up(idx: int, sub: int):
        nonlocal misses
        for attempt in (1, 2):
            try:
                data = upload(idx, sub)
                misses = 0
                return data, None
            except Abort as exc:
                misses = 0
                return None, exc.code
            except NoAnswer:
                if attempt == 2:
                    misses += 1
        if misses >= 3:
            raise NoAnswer(f"{where} stopped answering (at 0x{idx:04X}:{sub:02X})")
        return None, None
    return up


# ------------------------------------------------------------------------------------------- compare / read

def compare(doc: dict, upload: Callable, node_id: int, progress: Callable = lambda done, total: None,
            stopped: Callable = lambda: False) -> dict:
    """Read every readable variable of the document from the node and compare it with the file's value
    (the DCF value when there is one, else the default). Row status: same | differs | missing | size | error |
    read (the file has no value to compare with). Returns {"rows", "stopped": why it ended early, or ""}."""
    fmt = doc.get("format", "eds")
    todo = [(o, v, i, s) for o, v, i, s in E.variables(doc) if v.get("access") in E.READABLE]
    rows, up, why = [], _reader(upload, f"node {node_id}"), ""
    for n, (o, v, idx, sub) in enumerate(todo):
        if stopped():
            why = f"stopped at 0x{idx:04X}:{sub:02X}"
            break
        progress(n, len(todo))
        dt = v.get("data_type") or 0x0007
        ref = str(E.ref_value(v, fmt) or "").strip()
        row = {"index": idx, "sub": sub, "name": v.get("name", ""), "type": E.type_name(dt), "file": ref,
               "device": "", "raw": "", "status": ""}
        try:
            data, code = up(idx, sub)
        except NoAnswer as exc:
            why = str(exc)
            break
        if data is None:
            row["status"] = "error" if code is None or code in EXISTS else "missing"
            row["device"] = "no answer" if code is None else abort_text(code)
            rows.append(row)
            continue
        data = fit(data, dt)
        row["raw"] = data.hex(" ").upper()
        row["device"] = E.format_value(data, dt)
        size = E.DATA_TYPES.get(dt, ("", "", 0))[2]
        if size and size >= 8 and len(data) != size // 8:
            row["status"] = "size"
            row["device"] += f" ({len(data)} bytes, {E.type_name(dt)} has {size // 8})"
        elif not ref:
            row["status"] = "read"
        else:
            row["status"] = "same" if E.same_value(ref, row["device"], dt, node_id) else "differs"
        rows.append(row)
    progress(len(todo), len(todo))
    return {"rows": rows, "stopped": why}


# ------------------------------------------------------------------------------------------- probe

def _guess_type(data: bytes) -> int:
    if len(data) in (1, 2, 3, 4, 8):
        return {1: 0x0005, 2: 0x0006, 3: 0x0016, 4: 0x0007, 8: 0x001B}[len(data)]
    text = data.rstrip(b"\x00")
    if text and all(32 <= b < 127 for b in text):
        return 0x0009
    return 0x000A


def _known_objects() -> dict[int, dict]:
    """Names, types and access of the standard objects (from the 401 and 402 templates)."""
    out = {}
    for prof in ("401", "402"):
        for o in E.template(prof)["objects"]:
            out.setdefault(o["index"], o)
    for n in range(4, 512):                        # PDOs past the 4 of the templates look like PDO 1
        for base in (0x1400, 0x1600, 0x1800, 0x1A00):
            if base + n not in out:
                o = dict(out[base], index=base + n)
                o["name"] = o["name"].replace(" 1 ", f" {n + 1} ")
                out[base + n] = o
    return out


def _var_from_reply(data: Optional[bytes], code: Optional[int], known: Optional[dict], name: str) -> dict:
    guess = []
    if known is not None:
        v = E.new_var(known["name"], known["data_type"], known["access"], "", known.get("pdo", False))
        if data is not None:
            data = fit(data, v["data_type"])
    else:
        dt = _guess_type(data) if data is not None else 0x0007
        v = E.new_var(name, dt, "wo" if code == WRITE_ONLY else "ro")
        guess = ["type", "access"]
    if data is not None:                          # an unreadable object (abort) keeps an empty default
        v["default"] = E.format_value(data, v["data_type"])
        if known is None:
            guess.append("default")
    v["guess"] = guess
    return v


def probe(upload: Callable, ranges=FULL_RANGES, progress: Callable = lambda done, total, found: None,
          stopped: Callable = lambda: False, node_id: Optional[int] = None) -> dict:
    """Build a document from what the node answers. Standard objects get their CiA name, type and access;
    the rest get a data type guessed from the reply size, flagged for checking. When it ends early (Stop,
    the node stops answering, or the node answers every index) the objects found so far are kept, with a note."""
    known = _known_objects()
    doc = E.blank("device.eds")
    total = sum(b - a + 1 for a, b in ranges)
    done = 0
    up = _reader(upload, "the node")
    there = lambda d, c: d is not None or c in EXISTS  # noqa: E731

    def note(text: str) -> None:
        doc["notes"].append({"level": "warn", "index": None, "sub": None, "text": text})

    try:
        for a, b in ranges:
            scanned = abort_only = 0
            first = len(doc["objects"])
            for idx in range(a, b + 1):
                if stopped():
                    note(f"stopped at 0x{idx:04X}: only part of the node was read")
                    raise Stopped()
                done += 1
                scanned += 1
                if done % 64 == 0:
                    progress(done, total, len(doc["objects"]))
                if scanned >= 256 and abort_only * 2 > scanned:
                    # a node that answers (almost) every index with an abort does not tell missing objects apart
                    del doc["objects"][first:]
                    note(f"0x{a:04X}-0x{b:04X}: the node answers almost every index with an SDO abort, so missing "
                         f"objects cannot be told apart; this range was left out")
                    break
                data, code = up(idx, 0)
                k = known.get(idx)
                if code == NO_SUB:                    # no sub 0: a record without it, if sub 1 is there
                    d1, c1 = up(idx, 1)
                    if not there(d1, c1):
                        continue
                    structured = True
                elif not there(data, code):
                    continue
                else:
                    structured = k is not None and "subs" in k
                    if k is None and data is not None and len(fit(data, 0x0005)) == 1:
                        d1, c1 = up(idx, 1)           # a VAR answers sub 1 with "sub-index does not exist"
                        structured = there(d1, c1)
                if data is None and not structured:
                    abort_only += 1
                if not structured:
                    o = _var_from_reply(data, code, k, f"Object {idx:04X}h")
                    o["index"], o["object_type"] = idx, E.VAR
                    doc["objects"].append(o)
                    continue
                # sub 0 is the highest sub-index; some objects count the entries in use there (PDO mappings,
                # 0x1003), so standard objects are read up to their template's highest sub-index too
                data = fit(data, 0x0005) if data is not None else None
                count = data[0] if data else 0
                ksubs = {s["sub"]: s for s in (k or {}).get("subs", [])}
                limit = max([count] + list(ksubs)) if k is not None else (count or 8)
                subs = [dict(_var_from_reply(data, code, ksubs.get(0) or {"name": "Highest sub-index supported",
                                                                           "data_type": 0x0005, "access": "ro"},
                                             "Highest sub-index supported"), sub=0)]
                for sub in range(1, min(limit, 0xFE) + 1):
                    if stopped():
                        note(f"stopped at 0x{idx:04X}:{sub:02X}: only part of the node was read")
                        raise Stopped()
                    d, c = up(idx, sub)
                    if there(d, c):
                        subs.append(dict(_var_from_reply(d, c, ksubs.get(sub), f"Sub {sub}"), sub=sub))
                if data is None:                      # sub 0 unreadable: the EDS still needs it
                    subs[0]["default"] = str(subs[-1]["sub"])
                    subs[0]["guess"] = ["default"]
                ot = k["object_type"] if k is not None else E.RECORD
                doc["objects"].append({"index": idx, "name": k["name"] if k is not None else f"Object {idx:04X}h",
                                       "object_type": ot, "extra": {}, "subs": subs,
                                       "guess": [] if k is not None else ["structure"]})
    except Stopped:
        pass
    except NoAnswer as exc:
        note(f"{exc}: only part of the node was read")
    progress(total, total, len(doc["objects"]))
    _fill_device_info(doc, node_id)
    return doc


def _fill_device_info(doc: dict, node_id: Optional[int]) -> None:
    di = E.section(doc, "DeviceInfo")
    for key, sub in (("VendorNumber", 1), ("ProductNumber", 2), ("RevisionNumber", 3)):
        v = E.find_var(doc, 0x1018, sub)
        if v is not None and v.get("default"):
            di[key] = v["default"]
    name = E.find(doc, 0x1008)
    if name is not None and name.get("default"):
        di["ProductName"] = name["default"]
    # values that depend on the node id become $NODEID expressions, so the EDS fits every node id
    if node_id:
        for o, v, idx, sub in E.variables(doc):
            if (0x1400 <= idx <= 0x15FF or 0x1800 <= idx <= 0x19FF) and sub == 1 or (idx, sub) in (
                    (0x1014, 0), (0x1200, 1), (0x1200, 2)):
                try:
                    val = E.parse_int(v["default"])
                except ValueError:
                    continue
                base = val & 0x7FF
                if not val & 0x80000000 and base - node_id in (0x80, 0x180, 0x200, 0x280, 0x300, 0x380, 0x400,
                                                               0x480, 0x500, 0x580, 0x600):
                    v["default"] = f"$NODEID+0x{val - node_id:X}"
    di["NrOfRXPDO"] = str(sum(1 for o in doc["objects"] if 0x1400 <= o["index"] <= 0x15FF))
    di["NrOfTXPDO"] = str(sum(1 for o in doc["objects"] if 0x1800 <= o["index"] <= 0x19FF))


# ------------------------------------------------------------------------------------------- DCF -> steps

def _step_value(text: str, dt: int, node_id: int) -> tuple[str, object]:
    """(sequence type, value) for an SDO write step. Strings go as bytes: the step engine reads a value that
    looks like a number as a number, which would change a string such as "0012"."""
    name, kind, bits, st = E.DATA_TYPES.get(dt, ("", "bytes", 0, "bytes"))
    if st in ("bytes", "str"):
        return "bytes", E.pack(text, dt, node_id).hex(" ").upper()
    if st == "f32":
        return "f32", float(text)
    return st, E.int_value(text, dt, node_id)


# COB-ID objects whose CAN id must not change while they are in use (CiA 301): the value that switches them off
# first. (index, sub ranges) -> (bits to set, bits to clear)
_SWITCH_OFF = {0x1005: (0, 0x40000000), 0x1012: (0, 0xC0000000), 0x1014: (0x80000000, 0)}


def _switch_off(idx: int, sub: int) -> Optional[tuple[int, int]]:
    if idx in _SWITCH_OFF and sub == 0:
        return _SWITCH_OFF[idx]
    if 0x1200 <= idx <= 0x12FF and sub in (1, 2):         # SDO server/client COB-IDs: bit 31 = not valid
        return 0x80000000, 0
    return None


def dcf_steps(doc: dict, node_id: int, only_changed: bool = True, preop: bool = True, store: bool = True,
              reset: bool = False) -> dict:
    """Nodes-tab custom steps that write the DCF values to a node. PDOs are written the CiA 301 way: disable
    (COB-ID bit 31), mapping count 0, entries, count, the other parameters, then the COB-ID again; SYNC, TIME,
    EMCY and SDO COB-IDs are switched off before their id changes. Returns {"steps", "vars", "skipped", "count"}."""
    fmt = "dcf"
    chosen: dict[tuple[int, int], tuple[dict, str]] = {}
    skipped = []
    for o, v, idx, sub in E.variables(doc):
        val = str(v.get("value", "")).strip()
        if not val:
            continue
        if only_changed and E.same_value(val, v.get("default", ""), v.get("data_type") or 7, node_id):
            continue
        if v.get("access") not in E.WRITABLE:
            skipped.append(f"0x{idx:04X}:{sub:02X} {v.get('name', '')}: {v.get('access')} (not writable)")
            continue
        try:
            _step_value(val, v.get("data_type") or 7, node_id)
        except (ValueError, OverflowError, struct.error):
            skipped.append(f"0x{idx:04X}:{sub:02X} {v.get('name', '')}: {val!r} is not a valid "
                           f"{E.type_name(v.get('data_type') or 7)}")
            continue
        chosen[(idx, sub)] = (v, val)

    def write(idx: int, sub: int, v: dict, text: str, note: str = "") -> dict:
        t, value = _step_value(text, v.get("data_type") or 7, node_id)
        s = {"op": "sdo_write", "node": "$OLD_ID", "index": f"0x{idx:04X}", "sub": sub, "type": t, "value": value}
        if note:
            s["note"] = note
        return s

    steps: list[dict] = []
    if preop:
        steps.append({"op": "nmt", "command": "preop", "node": "$OLD_ID", "note": "PDOs can only be changed in "
                                                                                    "pre-operational"})
    pdo_keys = set()
    for kind, cbase, mbase in (("RPDO", 0x1400, 0x1600), ("TPDO", 0x1800, 0x1A00)):
        for n in range(0x200):
            touched = [k for k in chosen if k[0] in (cbase + n, mbase + n)]
            if not touched:
                continue
            pdo_keys.update(touched)
            label = f"{kind} {n + 1}"
            cob_var = E.find_var(doc, cbase + n, 1)
            cob_text = E.ref_value(cob_var, fmt) if cob_var else ""
            try:
                cob = E.parse_int(cob_text, node_id) if cob_text and cob_var.get("access") in E.WRITABLE else None
            except ValueError:
                cob = None
            if cob is not None:
                steps.append(write(cbase + n, 1, cob_var, str(cob | 0x80000000), f"{label}: disable while it changes"))
            m0 = E.find_var(doc, mbase + n, 0)
            if any(k[0] == mbase + n for k in touched):
                try:
                    count = E.parse_int(E.ref_value(m0, fmt) or "0", node_id) if m0 else 0
                except ValueError:
                    count = -1
                if m0 is None or m0.get("access") not in E.WRITABLE:
                    skipped.append(f"{label}: the mapping is fixed (0x{mbase + n:04X}:00 is not writable)")
                elif cob is None:
                    skipped.append(f"{label}: the mapping is not written, the PDO cannot be disabled first "
                                   f"(0x{cbase + n:04X}:01 is missing or not writable)")
                elif not 0 <= count <= 64:
                    skipped.append(f"{label}: the mapping is not written, {count} entries (0..64)")
                else:
                    steps.append(write(mbase + n, 0, m0, "0", f"{label}: mapping count 0 before changing entries"))
                    for i in range(1, count + 1):
                        e = E.find_var(doc, mbase + n, i)
                        if e is not None:
                            steps.append(write(mbase + n, i, e, E.ref_value(e, fmt) or "0"))
                    steps.append(write(mbase + n, 0, m0, str(count), f"{label}: {count} mapped object(s)"))
            for (idx, sub) in sorted(touched):
                if idx == cbase + n and sub != 1:
                    v, val = chosen[(idx, sub)]
                    steps.append(write(idx, sub, v, val))
            if cob is not None and not cob & 0x80000000:
                steps.append(write(cbase + n, 1, cob_var, str(cob), f"{label}: enable"))
    for (idx, sub) in sorted(chosen):
        if (idx, sub) in pdo_keys:
            continue
        v, val = chosen[(idx, sub)]
        off = _switch_off(idx, sub)
        if off is not None:
            final = E.parse_int(val, node_id)
            first = (final | off[0]) & ~off[1] & 0xFFFFFFFF
            if first != final:
                steps.append(write(idx, sub, v, str(first), "switched off while its CAN id changes"))
        steps.append(write(idx, sub, v, val))
    count = sum(1 for s in steps if s["op"] == "sdo_write")
    if store and count:
        if E.find_var(doc, 0x1010, 1) is not None:
            steps.append({"op": "store", "node": "$OLD_ID", "target": "0x1010:01"})
        else:
            skipped.append("no 0x1010:01 (store parameters) in the file: the values are not stored")
    if reset and count:
        steps.append({"op": "nmt", "command": "reset_node", "node": "$OLD_ID"})
        steps.append({"op": "wait_node", "node": "$OLD_ID", "timeout_s": 5, "optional": True})
    return {"steps": steps if count else [], "vars": {"OLD_ID": node_id}, "skipped": skipped, "count": count}
