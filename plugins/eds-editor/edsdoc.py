"""EDS / DCF documents (CiA 306) for the editor: read, check and write them without losing what we don't model.

A document is plain JSON (the UI edits it directly):

  {"format": "eds" | "dcf", "file_name": str, "encoding": str,
   "sections": [[name, {key: value}], ...],   FileInfo, DeviceInfo, DummyUsage, DeviceComissioning and any
                                              section we don't know, in file order, kept as text
   "comments": [line, ...],
   "objects": [object, ...],                  sorted by index
   "notes": [{level, index, sub, text}]}      what was odd while reading (shown with the problems)

  object = {"index": int, "name": str, "object_type": 7 VAR | 8 ARRAY | 9 RECORD | 2 DOMAIN | 5 DEFTYPE | 6 DEFSTRUCT,
            VAR fields (also on every sub): "data_type": int, "access": str, "default": str, "low": str,
            "high": str, "pdo": bool, "value": str (DCF ParameterValue), "denotation": str,
            "extra": {key: value} (every other key, written back unchanged),
            "guess": [str] (fields guessed by "create from device"; never written),
            ARRAY/RECORD: "subs": [sub, ...] with "sub": int}

Values stay text as in the file ("0x180", "$NODEID+0x180", "Device"). CompactSubObj objects are expanded into
explicit sub-indices when read (CiA 306 allows both).
"""

from __future__ import annotations

import configparser
import datetime as _dt
import re
import struct
from typing import Any, Optional

VAR, ARRAY, RECORD, DOMAIN, DEFTYPE, DEFSTRUCT = 7, 8, 9, 2, 5, 6
OBJECT_TYPES = {VAR: "VAR", ARRAY: "ARRAY", RECORD: "RECORD", DOMAIN: "DOMAIN", DEFTYPE: "DEFTYPE",
                DEFSTRUCT: "DEFSTRUCT", 0: "NULL"}
ACCESS = ("ro", "wo", "rw", "rwr", "rww", "const")
READABLE = ("ro", "rw", "rwr", "rww", "const")
WRITABLE = ("rw", "wo", "rww", "rwr")

# code: (name, kind, bits, sequence step type). kind: bool | int | uint | real | str | bytes
DATA_TYPES: dict[int, tuple[str, str, int, str]] = {
    0x0001: ("BOOLEAN", "bool", 1, "u8"),
    0x0002: ("INTEGER8", "int", 8, "i8"),
    0x0003: ("INTEGER16", "int", 16, "i16"),
    0x0004: ("INTEGER32", "int", 32, "i32"),
    0x0005: ("UNSIGNED8", "uint", 8, "u8"),
    0x0006: ("UNSIGNED16", "uint", 16, "u16"),
    0x0007: ("UNSIGNED32", "uint", 32, "u32"),
    0x0008: ("REAL32", "real", 32, "f32"),
    0x0009: ("VISIBLE_STRING", "str", 0, "str"),
    0x000A: ("OCTET_STRING", "bytes", 0, "bytes"),
    0x000B: ("UNICODE_STRING", "bytes", 0, "bytes"),
    0x000C: ("TIME_OF_DAY", "bytes", 48, "bytes"),
    0x000D: ("TIME_DIFFERENCE", "bytes", 48, "bytes"),
    0x000F: ("DOMAIN", "bytes", 0, "bytes"),
    0x0010: ("INTEGER24", "int", 24, "bytes"),
    0x0011: ("REAL64", "real", 64, "bytes"),
    0x0012: ("INTEGER40", "int", 40, "bytes"),
    0x0013: ("INTEGER48", "int", 48, "bytes"),
    0x0014: ("INTEGER56", "int", 56, "bytes"),
    0x0015: ("INTEGER64", "int", 64, "i64"),
    0x0016: ("UNSIGNED24", "uint", 24, "bytes"),
    0x0018: ("UNSIGNED40", "uint", 40, "bytes"),
    0x0019: ("UNSIGNED48", "uint", 48, "bytes"),
    0x001A: ("UNSIGNED56", "uint", 56, "bytes"),
    0x001B: ("UNSIGNED64", "uint", 64, "u64"),
}

MANDATORY = (0x1000, 0x1001, 0x1018)
# keys we model, by their lower-case spelling (files use any case)
_KEYS = {"parametername": "name", "objecttype": "object_type", "datatype": "data_type", "accesstype": "access",
         "defaultvalue": "default", "lowlimit": "low", "highlimit": "high", "pdomapping": "pdo",
         "parametervalue": "value", "denotation": "denotation", "subnumber": None, "compactsubobj": None}
_VAR_FIELDS = ("data_type", "access", "default", "low", "high", "pdo", "value", "denotation")
_RE_OBJ = re.compile(r"^([0-9A-Fa-f]{4})$")
_RE_SUB = re.compile(r"^([0-9A-Fa-f]{4})sub([0-9A-Fa-f]{1,2})$", re.IGNORECASE)
_RE_COMPACT = re.compile(r"^([0-9A-Fa-f]{4})(Name|Value|Denotation)$", re.IGNORECASE)
_LISTS = ("mandatoryobjects", "optionalobjects", "manufacturerobjects")


class EdsError(Exception):
    pass


# ------------------------------------------------------------------------------------------- values

def type_name(code: int) -> str:
    return DATA_TYPES.get(code, (f"0x{code:04X}",))[0]


def uses_nodeid(text: str) -> bool:
    return "$nodeid" in (text or "").lower()


def parse_int(text: Any, node_id: Optional[int] = None) -> int:
    """An EDS integer: decimal, 0x hex, leading-0 octal (CiA 306), with an optional $NODEID term
    ("$NODEID+0x180", "0x180+$NODEID"). With node_id None, $NODEID counts as 0."""
    if isinstance(text, bool):
        return int(text)
    if isinstance(text, int):
        return text
    s = str(text or "").strip().replace(" ", "")
    if not s:
        raise ValueError("empty")
    total, found = 0, False
    for term in re.split(r"(?=[+-])", s):
        if not term:
            continue
        sign = -1 if term[0] == "-" else 1
        t = term.lstrip("+-")
        if t.lower() == "$nodeid":
            total += sign * (node_id or 0)
            found = True
            continue
        if re.fullmatch(r"0[xX][0-9A-Fa-f]+", t):
            v = int(t, 16)
        elif re.fullmatch(r"0[0-7]+", t):
            v = int(t, 8)
        elif re.fullmatch(r"\d+", t):
            v = int(t)
        else:
            raise ValueError(f"{text!r} is not a number")
        total += sign * v
        found = True
    if not found:
        raise ValueError(f"{text!r} is not a number")
    return total


def int_range(code: int) -> Optional[tuple[int, int]]:
    name, kind, bits, _ = DATA_TYPES.get(code, ("", "", 0, ""))
    if kind == "bool":
        return 0, 1
    if kind == "uint":
        return 0, (1 << bits) - 1
    if kind == "int":
        return -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return None


def int_value(text: Any, code: int, node_id: Optional[int] = None) -> int:
    """parse_int for a value of data type `code`: a signed type also takes its two's complement in hex
    (INTEGER16 0xFFFF = -1), as vendor tools write negative numbers."""
    v = parse_int(text, node_id)
    name, kind, bits, _ = DATA_TYPES.get(code, ("", "", 0, ""))
    if kind == "int" and (1 << (bits - 1)) <= v < (1 << bits) and "0x" in str(text).lower():
        v -= 1 << bits
    return v


def value_error(text: str, code: int) -> Optional[str]:
    """Why `text` is not a valid value of data type `code` (None when it is, or when it can't be checked)."""
    if text is None or str(text).strip() == "":
        return None
    kind = DATA_TYPES.get(code, ("", "", 0, ""))[1]
    if kind in ("int", "uint", "bool"):
        try:
            lo_hi = int_range(code)
            nodes = (1, 127) if uses_nodeid(text) else (None,)
            for n in nodes:
                v = int_value(text, code, n)
                if lo_hi and not lo_hi[0] <= v <= lo_hi[1]:
                    return f"{text} is outside {type_name(code)} ({lo_hi[0]}..{lo_hi[1]})" + \
                        (f" for node {n}" if n else "")
        except ValueError:
            return f"{text!r} is not a valid {type_name(code)}"
    elif kind == "real":
        try:
            float(str(text).strip())
        except ValueError:
            return f"{text!r} is not a valid {type_name(code)}"
    elif kind == "bytes" and code in (0x000A, 0x000F):
        t = str(text).strip()
        if t.lower().startswith("0x"):
            t = t[2:]
        if re.fullmatch(r"([0-9A-Fa-f]{2}\s*)*", t) is None:
            return f"{text!r} is not hex bytes"
    return None


def pack(text: str, code: int, node_id: Optional[int] = None) -> bytes:
    """The bytes a value is sent as (little endian), for comparing with a device and for SDO write steps."""
    name, kind, bits, _ = DATA_TYPES.get(code, ("", "bytes", 0, "bytes"))
    if kind in ("int", "uint", "bool"):
        v = int_value(text, code, node_id)
        n = max(1, bits // 8)
        return v.to_bytes(n, "little", signed=kind == "int")
    if kind == "real":
        return struct.pack("<f" if bits == 32 else "<d", float(text))
    if kind == "str":
        return str(text).encode("latin-1", errors="replace")
    t = str(text).strip()
    if t.lower().startswith("0x"):
        t = t[2:]
    return bytes.fromhex(t)


def format_value(data: bytes, code: int) -> str:
    """Bytes read from a device as EDS value text in the given data type."""
    name, kind, bits, _ = DATA_TYPES.get(code, ("", "bytes", 0, "bytes"))
    if kind in ("int", "uint", "bool") and data:
        n = max(1, bits // 8)
        v = int.from_bytes(data[:n].ljust(n, b"\x00"), "little", signed=kind == "int")
        if kind == "uint" and bits >= 16:
            return f"0x{v:0{n * 2}X}"
        return str(v)
    if kind == "real" and len(data) >= bits // 8:
        v = struct.unpack("<f" if bits == 32 else "<d", data[:bits // 8])[0]
        return repr(v)
    if kind == "str":
        return data.decode("latin-1").rstrip("\x00")
    return data.hex().upper()


def same_value(a: str, b: str, code: int, node_id: Optional[int] = None) -> bool:
    try:
        kind = DATA_TYPES.get(code, ("", "bytes"))[1]
        if kind == "str":
            return str(a).rstrip("\x00") == str(b).rstrip("\x00")
        if kind == "real":
            return abs(float(a) - float(b)) <= 1e-6 * max(1.0, abs(float(a)))
        return pack(a, code, node_id) == pack(b, code, node_id)
    except (ValueError, OverflowError, struct.error):
        return str(a).strip() == str(b).strip()


# ------------------------------------------------------------------------------------------- objects

def new_var(name: str, data_type: int, access: str = "rw", default: str = "", pdo: bool = False, **kw) -> dict:
    v = {"name": name, "data_type": data_type, "access": access, "default": default,
         "low": "", "high": "", "pdo": pdo, "value": "", "denotation": "", "extra": {}}
    v.update(kw)
    return v


def new_object(index: int, name: str, object_type: int = VAR, **kw) -> dict:
    if object_type in (ARRAY, RECORD):
        return {"index": index, "name": name, "object_type": object_type, "extra": {},
                "subs": kw.pop("subs", None) or [dict(new_var("Highest sub-index supported", 0x0005, "ro", "0"),
                                                      sub=0)]}
    o = new_var(name, kw.pop("data_type", 0x0007), **kw)
    o["object_type"] = object_type
    o["index"] = index
    return o


def find(doc: dict, index: int) -> Optional[dict]:
    for o in doc["objects"]:
        if o["index"] == index:
            return o
    return None


def find_var(doc: dict, index: int, sub: int) -> Optional[dict]:
    o = find(doc, index)
    if o is None:
        return None
    if "subs" not in o:
        return o if sub == 0 else None
    for s in o["subs"]:
        if s["sub"] == sub:
            return s
    return None


def variables(doc: dict):
    """(object, var, index, sub) for every variable (a VAR object, or each sub of an ARRAY/RECORD)."""
    for o in doc["objects"]:
        if "subs" in o:
            for s in o["subs"]:
                yield o, s, o["index"], s["sub"]
        elif o["object_type"] in (VAR, DOMAIN):
            yield o, o, o["index"], 0


def ref_value(var: dict, fmt: str) -> str:
    """The value that applies: the DCF ParameterValue when there is one, else the default."""
    if fmt == "dcf" and str(var.get("value", "")).strip():
        return var["value"]
    return var.get("default", "")


def section(doc: dict, name: str, create: bool = False) -> Optional[dict]:
    for n, d in doc["sections"]:
        if n.lower() == name.lower():
            return d
    if create:
        d: dict = {}
        doc["sections"].append([name, d])
        return d
    return None


# ------------------------------------------------------------------------------------------- read

def _decode(data: bytes) -> tuple[str, str]:
    if data.isascii():                       # CiA 306 files are ASCII; non-ASCII typed later is saved as ANSI
        return data.decode("ascii"), "cp1252"
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc), "utf-8" if enc == "utf-8-sig" else enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), "latin-1"


def _var_from(sec: dict, notes: list, where: tuple) -> dict:
    v = new_var("", 0x0007, "", "")
    v["extra"] = {}
    v["data_type"] = None
    for key, val in sec.items():
        k = _KEYS.get(key.lower(), "?")
        if k is None:
            continue
        if k == "?":
            v["extra"][key] = val
        elif k == "name":
            v["name"] = val
        elif k == "object_type":
            try:
                v["object_type"] = parse_int(val)
            except ValueError:
                notes.append(_note("error", where, f"ObjectType {val!r} is not a number"))
        elif k == "data_type":
            try:
                v["data_type"] = parse_int(val)
            except ValueError:
                notes.append(_note("error", where, f"DataType {val!r} is not a number"))
        elif k == "access":
            v["access"] = val.strip().lower()
        elif k == "pdo":
            try:
                v["pdo"] = bool(parse_int(val))
            except ValueError:
                v["pdo"] = val.strip().lower() in ("true", "yes")
        else:
            v[k] = val
    return v


def _note(level: str, where: tuple, text: str) -> dict:
    return {"level": level, "index": where[0], "sub": where[1] if len(where) > 1 else None, "text": text}


def read(text_or_bytes, file_name: str = "") -> dict:
    """Parse EDS/DCF text (or bytes) into a document."""
    if isinstance(text_or_bytes, bytes):
        text, enc = _decode(text_or_bytes)
    else:
        text, enc = text_or_bytes, "utf-8"
    cp = configparser.RawConfigParser(strict=False, interpolation=None, comment_prefixes=(";", "#"),
                                      inline_comment_prefixes=None, empty_lines_in_values=False)
    cp.optionxform = str
    try:
        cp.read_string(text)
    except configparser.Error as exc:
        raise EdsError(f"not an EDS/DCF file: {exc}") from exc
    if not cp.sections():
        raise EdsError("not an EDS/DCF file: no [sections]")

    notes: list = []
    doc = {"format": "dcf" if file_name.lower().endswith(".dcf") else "eds", "file_name": file_name,
           "encoding": enc, "sections": [], "comments": [], "objects": [], "notes": notes}
    objs: dict[int, dict] = {}
    subs: dict[int, dict[int, dict]] = {}
    compact: dict[int, dict[str, dict]] = {}
    listed: set[int] = set()
    for name in cp.sections():
        sec = dict(cp.items(name))
        if m := _RE_OBJ.match(name):
            idx = int(m.group(1), 16)
            o = _var_from(sec, notes, (idx,))
            o["index"] = idx
            o["_compact"] = next((v for k, v in sec.items() if k.lower() == "compactsubobj"), None)
            o["_raw"] = sec
            objs[idx] = o
        elif m := _RE_SUB.match(name):
            idx, sub = int(m.group(1), 16), int(m.group(2), 16)
            s = _var_from(sec, notes, (idx, sub))
            s["sub"] = sub
            s.pop("object_type", None)
            subs.setdefault(idx, {})[sub] = s
        elif m := _RE_COMPACT.match(name):
            compact.setdefault(int(m.group(1), 16), {})[m.group(2).lower()] = (name, sec)
        elif name.lower() in _LISTS:
            for k, v in sec.items():
                if k.lower() != "supportedobjects":
                    try:
                        listed.add(parse_int(v))
                    except ValueError:
                        notes.append(_note("warn", (None,), f"[{name}] {k}={v} is not an index"))
        elif name.lower() == "comments":
            lines = sorted(((parse_int(k[4:]) if k.lower().startswith("line") and k[4:].isdigit() else 0, v)
                            for k, v in sec.items() if k.lower() != "lines"))
            doc["comments"] = [v for n, v in lines if n]
        else:
            doc["sections"].append([name, sec])

    for idx in sorted(objs):
        o = objs[idx]
        ot = o.get("object_type") or VAR
        o["object_type"] = ot
        cmp = o.pop("_compact", None)
        raw = o.pop("_raw")
        if ot in (ARRAY, RECORD, DEFSTRUCT):
            members = subs.pop(idx, {})
            expanded = bool(cmp and not members)
            if expanded:
                members = _expand_compact(o, cmp, {k: v[1] for k, v in compact.pop(idx, {}).items()}, notes)
            # every key of the object section except the ones written anyway (and, when expanded, the ones the
            # sub-indices took over) is written back unchanged
            skip = {"parametername", "objecttype", "subnumber", "compactsubobj"}
            if expanded:
                skip |= {"datatype", "accesstype", "defaultvalue", "pdomapping", "lowlimit", "highlimit"}
            o2 = {"index": idx, "name": o["name"], "object_type": ot,
                  "extra": {k: v for k, v in raw.items() if k.lower() not in skip},
                  "subs": [members[k] for k in sorted(members)]}
            for s in o2["subs"]:
                if s["data_type"] is None:
                    notes.append(_note("error", (idx, s["sub"]), "no DataType"))
                    s["data_type"] = 0x0007
            doc["objects"].append(o2)
        else:
            if subs.pop(idx, None):
                notes.append(_note("warn", (idx,), f"sub-index sections of a {OBJECT_TYPES.get(ot, ot)} were ignored"))
            if o["data_type"] is None and ot == DOMAIN:
                o["data_type"] = 0x000F
            elif o["data_type"] is None:
                notes.append(_note("error", (idx,), "no DataType"))
                o["data_type"] = 0x0007
            o.pop("sub", None)
            doc["objects"].append(o)
    for idx in sorted(subs):
        notes.append(_note("error", (idx,), "sub-index sections without an object section were ignored"))
    for idx in sorted(compact):                    # Name/Value sections of an object that is not compact: keep them
        for name, sec in compact[idx].values():
            doc["sections"].append([name, sec])
    present = {o["index"] for o in doc["objects"]}
    for idx in sorted(listed - present):
        notes.append(_note("warn", (idx,), "listed in the object lists but has no section"))
    for idx in sorted(present - listed):
        if listed and idx >= 0x1000:
            notes.append(_note("info", (idx,), "has a section but was missing from the object lists (now listed)"))
    return doc


def _expand_compact(o: dict, cmp: str, extra: dict, notes: list) -> dict[int, dict]:
    try:
        n = parse_int(cmp)
    except ValueError:
        n = -1
    if not 0 <= n <= 254:
        notes.append(_note("error", (o["index"],), f"CompactSubObj={cmp} is not a number 0..254: not expanded"))
        return {}
    names, values, dens = extra.get("name", {}), extra.get("value", {}), extra.get("denotation", {})
    lookup = lambda d, i: next((v for k, v in d.items() if k.isdigit() and int(k) == i), "")  # noqa: E731
    out = {0: dict(new_var("Highest sub-index supported", 0x0005, "ro", str(n)), sub=0)}
    for i in range(1, n + 1):
        out[i] = dict(new_var(lookup(names, i) or f"{o['name']}{i}", o["data_type"] or 0x0007, o["access"] or "rw",
                              o.get("default", ""), o.get("pdo", False), low=o.get("low", ""), high=o.get("high", ""),
                              value=lookup(values, i), denotation=lookup(dens, i)), sub=i)
    notes.append(_note("info", (o["index"],), f"CompactSubObj={n}: expanded into sub-indices 1..{n}"))
    return out


def read_xdd(data: bytes, file_name: str = "") -> dict:
    """A document from a CiA 311 XDD/XDC file: the object list and the device identity (values kept as text)."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise EdsError(f"not an XDD file: {exc}") from exc
    tag = lambda el: el.tag.rsplit("}", 1)[-1]  # noqa: E731
    doc = blank((file_name.rsplit(".", 1)[0] or "device") + ".eds")
    doc["notes"].append(_note("info", (None,), f"read from {file_name or 'an XDD file'}; it is saved as EDS/DCF"))
    di = section(doc, "DeviceInfo")
    for el in root.iter():
        t = tag(el)
        if t in ("vendorName", "vendorID", "productName", "productID") and (el.text or "").strip():
            key = {"vendorName": "VendorName", "vendorID": "VendorNumber", "productName": "ProductName",
                   "productID": "ProductNumber"}[t]
            v = el.text.strip()
            di[key] = f"0x{v}" if t in ("vendorID", "productID") and not v.lower().startswith("0x") else v

    def var(el) -> dict:
        pdo = (el.get("PDOmapping") or "no").lower() != "no"
        v = new_var(el.get("name", ""), int(el.get("dataType") or "0007", 16), (el.get("accessType") or "rw").lower(),
                    el.get("defaultValue", ""), pdo, low=el.get("lowLimit", ""), high=el.get("highLimit", ""),
                    value=el.get("actualValue", ""))
        return v

    for el in root.iter():
        if tag(el) != "CANopenObject":
            continue
        try:
            idx = int(el.get("index"), 16)
        except (TypeError, ValueError):
            doc["notes"].append(_note("error", (None,), f"object with index {el.get('index')!r} skipped"))
            continue
        ot = int(el.get("objectType") or "7")
        members = [s for s in el if tag(s) == "CANopenSubObject"]
        if ot in (ARRAY, RECORD) and members:
            subs = [dict(var(s), sub=int(s.get("subIndex", "0"), 16)) for s in members]
            doc["objects"].append({"index": idx, "name": el.get("name", ""), "object_type": ot, "extra": {},
                                   "subs": sorted(subs, key=lambda s: s["sub"])})
        else:
            o = var(el)
            o["index"] = idx
            doc["objects"].append(o)
    if not doc["objects"]:
        raise EdsError("no CANopenObject in this XDD file")
    doc["objects"].sort(key=lambda o: o["index"])
    return doc


# ------------------------------------------------------------------------------------------- write

def _now() -> tuple[str, str]:
    t = _dt.datetime.now()
    return t.strftime("%m-%d-%Y"), t.strftime("%I:%M%p")


def write(doc: dict, fmt: Optional[str] = None, file_name: Optional[str] = None) -> str:
    """The document as EDS or DCF text (CRLF). ParameterValue, Denotation and [DeviceComissioning] only in a DCF."""
    fmt = (fmt or doc.get("format") or "eds").lower()
    dcf = fmt == "dcf"
    out: list[str] = []

    def sec(name: str, items) -> None:
        out.append(f"[{name}]")
        out.extend(f"{k}={v}" for k, v in items)
        out.append("")

    secs = [[n, dict(d)] for n, d in doc["sections"]]
    get = lambda nm: next((d for n, d in secs if n.lower() == nm.lower()), None)  # noqa: E731
    fi = get("FileInfo")
    if fi is None:
        fi = {}
        secs.insert(0, ["FileInfo", fi])
    date, time_ = _now()
    if file_name or doc.get("file_name"):
        fi["FileName"] = file_name or doc["file_name"]
    fi["ModificationDate"], fi["ModificationTime"] = date, time_
    fi.setdefault("CreationDate", date)
    fi.setdefault("CreationTime", time_)
    fi.setdefault("EDSVersion", "4.0")
    di = get("DeviceInfo")
    if di is not None:                                     # always matches the PDO objects in the file
        di["NrOfRXPDO"] = str(sum(1 for o in doc["objects"] if 0x1400 <= o["index"] <= 0x15FF))
        di["NrOfTXPDO"] = str(sum(1 for o in doc["objects"] if 0x1800 <= o["index"] <= 0x19FF))

    first = ("fileinfo", "deviceinfo", "dummyusage")
    for n, d in sorted((s for s in secs if s[0].lower() in first), key=lambda s: first.index(s[0].lower())):
        sec(n, d.items())
    sec("Comments", [("Lines", len(doc["comments"]))] + [(f"Line{i}", l) for i, l in enumerate(doc["comments"], 1)])
    if dcf:
        dc = get("DeviceComissioning") or {}
        sec("DeviceComissioning", dc.items())

    objs = sorted(doc["objects"], key=lambda o: o["index"])
    groups = [("MandatoryObjects", [o for o in objs if o["index"] in MANDATORY]),
              ("OptionalObjects", [o for o in objs if o["index"] not in MANDATORY
                                   and not 0x2000 <= o["index"] <= 0x5FFF]),
              ("ManufacturerObjects", [o for o in objs if 0x2000 <= o["index"] <= 0x5FFF])]
    for name, group in groups:
        sec(name, [("SupportedObjects", len(group))] + [(str(i), f"0x{o['index']:04X}") for i, o in enumerate(group, 1)])
        for o in group:
            _write_object(o, dcf, sec)
    for n, d in secs:
        if n.lower() not in first and n.lower() != "devicecomissioning":
            sec(n, d.items())
    return "\r\n".join(out)


def _var_items(v: dict, dcf: bool, object_type: int) -> list:
    items = [("ParameterName", v.get("name", "")), ("ObjectType", f"0x{object_type:X}"),
             ("DataType", f"0x{int(v.get('data_type') or 7):04X}"), ("AccessType", v.get("access", "rw") or "rw")]
    for key, field in (("DefaultValue", "default"), ("LowLimit", "low"), ("HighLimit", "high")):
        if str(v.get(field, "")).strip() != "":
            items.append((key, v[field]))
    items.append(("PDOMapping", "1" if v.get("pdo") else "0"))
    if dcf:
        if str(v.get("value", "")).strip() != "":
            items.append(("ParameterValue", v["value"]))
        if str(v.get("denotation", "")).strip() != "":
            items.append(("Denotation", v["denotation"]))
    items.extend((v.get("extra") or {}).items())
    return items


def _write_object(o: dict, dcf: bool, sec) -> None:
    idx = o["index"]
    if "subs" in o:
        extra = dict(o.get("extra") or {})
        if not dcf:
            extra.pop("ParameterValue", None)
            extra.pop("Denotation", None)
        sec(f"{idx:04X}", [("ParameterName", o["name"]), ("ObjectType", f"0x{o['object_type']:X}"),
                           ("SubNumber", f"0x{len(o['subs']):X}")] + list(extra.items()))
        for s in sorted(o["subs"], key=lambda s: s["sub"]):
            sec(f"{idx:04X}sub{s['sub']:X}", _var_items(s, dcf, VAR))
    else:
        sec(f"{idx:04X}", _var_items(o, dcf, o.get("object_type") or VAR))


# ------------------------------------------------------------------------------------------- templates

def blank(file_name: str = "device.eds") -> dict:
    date, time_ = _now()
    return {"format": "eds", "file_name": file_name, "encoding": "cp1252", "comments": [], "objects": [], "notes": [],
            "sections": [
                ["FileInfo", {"FileName": file_name, "FileVersion": "1", "FileRevision": "0", "EDSVersion": "4.0",
                              "Description": "", "CreationTime": time_, "CreationDate": date,
                              "CreatedBy": "", "ModificationTime": time_, "ModificationDate": date,
                              "ModifiedBy": ""}],
                ["DeviceInfo", {"VendorName": "", "VendorNumber": "0x00000000", "ProductName": "",
                                "ProductNumber": "0x00000000", "RevisionNumber": "0x00000000", "OrderCode": "",
                                **{f"BaudRate_{b}": "1" for b in (10, 20, 50, 125, 250, 500, 800, 1000)},
                                "SimpleBootUpMaster": "0", "SimpleBootUpSlave": "1", "Granularity": "8",
                                "DynamicChannelsSupported": "0", "GroupMessaging": "0", "NrOfRXPDO": "0",
                                "NrOfTXPDO": "0", "LSS_Supported": "0"}],
                ["DummyUsage", {f"Dummy000{i}": "0" for i in range(1, 8)}]]}


def _rec(index: int, name: str, members: list, object_type: int = RECORD) -> dict:
    """members: [(name, data_type, access, default, pdo)] for sub 1..; sub 0 is the highest sub-index."""
    subs = [dict(new_var("Highest sub-index supported", 0x0005, "ro", str(len(members))), sub=0)]
    for i, (nm, dt, acc, dflt, pdo) in enumerate(members, 1):
        if nm is not None:
            subs.append(dict(new_var(nm, dt, acc, dflt, pdo), sub=i))
    return {"index": index, "name": name, "object_type": object_type, "extra": {}, "subs": subs}


def _pdo_pair(doc: dict, kind: str, n: int, mapping: list[int]) -> None:
    rx = kind == "rpdo"
    comm, mapi = (0x1400 if rx else 0x1800) + n, (0x1600 if rx else 0x1A00) + n
    # CiA 301 predefines the COB-IDs of PDO 1-4 only; later ones start disabled (bit 31) until a COB-ID is chosen
    cob = f"$NODEID+0x{(0x200 if rx else 0x180) + 0x100 * n:X}" if n < 4 else "0x80000000"
    if rx:
        doc["objects"].append(_rec(comm, f"RPDO {n + 1} communication parameter", [
            ("COB-ID used by RPDO", 0x0007, "rw", cob, False),
            ("Transmission type", 0x0005, "rw", "0xFF", False)]))
    else:
        c = _rec(comm, f"TPDO {n + 1} communication parameter", [
            ("COB-ID used by TPDO", 0x0007, "rw", cob, False),
            ("Transmission type", 0x0005, "rw", "0xFE", False),
            ("Inhibit time", 0x0006, "rw", "0", False),
            (None, 0, "", "", False),
            ("Event timer", 0x0006, "rw", "0", False)])
        doc["objects"].append(c)
    m = _rec(mapi, f"{'RPDO' if rx else 'TPDO'} {n + 1} mapping parameter",
             [(f"Application object {i}", 0x0007, "rw", f"0x{mapping[i - 1]:08X}" if i <= len(mapping) else "0x00000000",
               False) for i in range(1, 9)])
    m["subs"][0].update(access="rw", default=str(len(mapping)))
    doc["objects"].append(m)


def template(profile: str = "301", vendor_name: str = "", vendor_number: str = "0x00000000",
             product_name: str = "", product_number: str = "0x00000000", file_name: str = "device.eds") -> dict:
    """A new device: the CiA 301 communication objects, 4 RPDOs and 4 TPDOs, plus CiA 401 (generic I/O) or
    CiA 402 (drive) objects with their usual default mapping."""
    doc = blank(file_name)
    di = section(doc, "DeviceInfo")
    di.update(VendorName=vendor_name, VendorNumber=vendor_number, ProductName=product_name,
              ProductNumber=product_number)
    device_type = {"401": "0x00000191", "402": "0x00020192"}.get(profile, "0x00000000")
    O = doc["objects"]
    O.append(new_object(0x1000, "Device type", data_type=0x0007, access="ro", default=device_type))
    O.append(new_object(0x1001, "Error register", data_type=0x0005, access="ro", default="0", pdo=True))
    e = _rec(0x1003, "Pre-defined error field", [("Standard error field", 0x0007, "ro", "0", False)], ARRAY)
    e["subs"][0].update(name="Number of errors", access="rw", default="0")
    O.append(e)
    O.append(new_object(0x1005, "COB-ID SYNC", data_type=0x0007, access="rw", default="0x00000080"))
    O.append(new_object(0x1008, "Manufacturer device name", data_type=0x0009, access="const", default=product_name))
    O.append(new_object(0x1009, "Manufacturer hardware version", data_type=0x0009, access="const", default=""))
    O.append(new_object(0x100A, "Manufacturer software version", data_type=0x0009, access="const", default=""))
    O.append(_rec(0x1010, "Store parameters", [("Save all parameters", 0x0007, "rw", "0x00000001", False)], ARRAY))
    O.append(_rec(0x1011, "Restore default parameters", [("Restore all default parameters", 0x0007, "rw", "0x00000001",
                                                          False)], ARRAY))
    O.append(new_object(0x1014, "COB-ID EMCY", data_type=0x0007, access="rw", default="$NODEID+0x80"))
    O.append(new_object(0x1017, "Producer heartbeat time", data_type=0x0006, access="rw", default="0"))
    O.append(_rec(0x1018, "Identity object", [("Vendor-ID", 0x0007, "ro", vendor_number, False),
                                              ("Product code", 0x0007, "ro", product_number, False),
                                              ("Revision number", 0x0007, "ro", "0x00000000", False),
                                              ("Serial number", 0x0007, "ro", "0x00000000", False)]))
    O.append(_rec(0x1200, "SDO server parameter", [("COB-ID client to server", 0x0007, "ro", "$NODEID+0x600", False),
                                                   ("COB-ID server to client", 0x0007, "ro", "$NODEID+0x580", False)]))
    rpdo: list[list[int]] = [[], [], [], []]
    tpdo: list[list[int]] = [[], [], [], []]
    if profile == "401":
        O.append(_rec(0x6000, "Read input 8-bit", [("Read input 1h-8h", 0x0005, "ro", "0", True)], ARRAY))
        O.append(_rec(0x6200, "Write output 8-bit", [("Write output 1h-8h", 0x0005, "rww", "0", True)], ARRAY))
        O.append(_rec(0x6401, "Read analog input 16-bit", [(f"Analog input {i}", 0x0003, "ro", "0", True)
                                                           for i in (1, 2)], ARRAY))
        O.append(_rec(0x6411, "Write analog output 16-bit", [(f"Analog output {i}", 0x0003, "rww", "0", True)
                                                             for i in (1, 2)], ARRAY))
        tpdo = [[0x60000108], [0x64010110, 0x64010210], [], []]
        rpdo = [[0x62000108], [0x64110110, 0x64110210], [], []]
    elif profile == "402":
        for idx, nm, dt, acc, pdo in (
                (0x603F, "Error code", 0x0006, "ro", True), (0x6040, "Controlword", 0x0006, "rww", True),
                (0x6041, "Statusword", 0x0006, "ro", True), (0x605A, "Quick stop option code", 0x0003, "rw", False),
                (0x6060, "Modes of operation", 0x0002, "rww", True),
                (0x6061, "Modes of operation display", 0x0002, "ro", True),
                (0x6064, "Position actual value", 0x0004, "ro", True),
                (0x606C, "Velocity actual value", 0x0004, "ro", True),
                (0x6071, "Target torque", 0x0003, "rww", True), (0x6077, "Torque actual value", 0x0003, "ro", True),
                (0x607A, "Target position", 0x0004, "rww", True), (0x6081, "Profile velocity", 0x0007, "rw", True),
                (0x6083, "Profile acceleration", 0x0007, "rw", False),
                (0x6084, "Profile deceleration", 0x0007, "rw", False),
                (0x60FF, "Target velocity", 0x0004, "rww", True), (0x6502, "Supported drive modes", 0x0007, "ro", False)):
            O.append(new_object(idx, nm, data_type=dt, access=acc, default="0", pdo=pdo))
        rpdo = [[0x60400010], [0x60400010, 0x60600008], [0x60400010, 0x607A0020], [0x60400010, 0x60FF0020]]
        tpdo = [[0x60410010], [0x60410010, 0x60610008], [0x60410010, 0x60640020], [0x60410010, 0x606C0020]]
    for n in range(4):
        _pdo_pair(doc, "rpdo", n, rpdo[n])
        _pdo_pair(doc, "tpdo", n, tpdo[n])
    doc["objects"].sort(key=lambda o: o["index"])
    di["NrOfRXPDO"], di["NrOfTXPDO"] = "4", "4"
    return doc


# ------------------------------------------------------------------------------------------- check

def _p(level: str, text: str, index: Optional[int] = None, sub: Optional[int] = None) -> dict:
    return {"level": level, "index": index, "sub": sub, "text": text}


def _check_var(v: dict, index: int, sub: Optional[int], fmt: str, deftypes: set, out: list) -> None:
    where = (index, sub)
    if not str(v.get("name", "")).strip():
        out.append(_p("error", "no name (ParameterName)", *where))
    dt = v.get("data_type")
    if dt not in DATA_TYPES and dt not in deftypes:
        out.append(_p("error", f"unknown data type 0x{int(dt or 0):04X}", *where))
    if v.get("access") not in ACCESS:
        out.append(_p("error", f"access type {v.get('access')!r} is not one of {', '.join(ACCESS)}", *where))
    for field, label in (("default", "default value"), ("low", "low limit"), ("high", "high limit")):
        msg = value_error(v.get(field, ""), dt)
        if msg:
            out.append(_p("error", f"{label}: {msg}", *where))
    if fmt == "dcf":
        msg = value_error(v.get("value", ""), dt)
        if msg:
            out.append(_p("error", f"DCF value: {msg}", *where))
    rng = int_range(dt)
    if rng and (v.get("low") or v.get("high")):
        try:
            lo = int_value(v["low"], dt) if str(v.get("low", "")).strip() else None
            hi = int_value(v["high"], dt) if str(v.get("high", "")).strip() else None
            if lo is not None and hi is not None and lo > hi:
                out.append(_p("error", f"low limit {v['low']} is above the high limit {v['high']}", *where))
            for field in ("default", "value"):
                t = str(v.get(field, "")).strip()
                if t and not uses_nodeid(t):
                    x = int_value(t, dt)
                    if (lo is not None and x < lo) or (hi is not None and x > hi):
                        out.append(_p("warn", f"{'default' if field == 'default' else 'DCF value'} {t} is outside "
                                              f"the limits", *where))
        except ValueError:
            pass
    if v.get("pdo") and DATA_TYPES.get(dt, ("", ""))[1] == "bytes" and dt in (0x000F,):
        out.append(_p("warn", "a DOMAIN cannot be mapped into a PDO", *where))
    if v.get("access") == "const" and not str(v.get("default", "")).strip() and \
            DATA_TYPES.get(dt, ("", ""))[1] not in ("str", "bytes"):
        out.append(_p("warn", "a const object without a default value", *where))
    for g in v.get("guess") or []:
        out.append(_p("warn", {"type": "data type guessed from the reply size: check it",
                               "access": "access type unknown (CANopener only read it): check it",
                               "structure": "ARRAY/RECORD guessed: check it",
                               "default": "default is the value the device had"}.get(g, g), *where))


def _cob(text: str) -> Optional[tuple[int, bool]]:
    """(id part, uses $NODEID) of a COB-ID value, None when it can't be read. Bit 31 set = not valid."""
    try:
        v = parse_int(text, 0)
    except ValueError:
        return None
    return v, uses_nodeid(text)


def check(doc: dict) -> list[dict]:
    """Problems with the document: [{level: error|warn|info, index, sub, text}]."""
    out: list[dict] = [dict(n) for n in doc.get("notes", [])]
    fmt = doc.get("format", "eds")
    deftypes = {o["index"] for o in doc["objects"] if o["index"] < 0x1000}
    seen: dict[int, int] = {}
    for o in doc["objects"]:
        idx = o["index"]
        seen[idx] = seen.get(idx, 0) + 1
        if not 0 < idx <= 0xFFFF:
            out.append(_p("error", f"index 0x{idx:X} is out of range", idx))
        elif 0x0260 <= idx <= 0x0FFF:
            out.append(_p("warn", "index is in the reserved range 0x0260-0x0FFF", idx))
        if "subs" in o:
            if not str(o.get("name", "")).strip():
                out.append(_p("error", "no name (ParameterName)", idx))
            subs = {s["sub"]: s for s in o["subs"]}
            if len(subs) != len(o["subs"]):
                out.append(_p("error", "the same sub-index twice", idx))
            if 0 not in subs:
                out.append(_p("error", "no sub-index 0 (highest sub-index supported)", idx))
            else:
                s0 = subs[0]
                if s0.get("data_type") != 0x0005:
                    out.append(_p("error", "sub-index 0 must be UNSIGNED8", idx, 0))
                try:
                    n = parse_int(ref_value(s0, fmt) or "0")
                    top = max(subs)
                    # in these, sub 0 counts the entries in use (errors in the list, mapped objects)
                    in_use = idx == 0x1003 or 0x1600 <= idx <= 0x17FF or 0x1A00 <= idx <= 0x1BFF
                    if n > top or (top > n and not in_use):
                        out.append(_p("error", f"sub-index 0 says {n} but the highest sub-index is {top}", idx, 0))
                except ValueError:
                    pass
            if o["object_type"] == ARRAY:
                types = {s.get("data_type") for s in o["subs"] if s["sub"] > 0}
                if len(types) > 1:
                    out.append(_p("error", "an ARRAY needs the same data type on every sub-index (use RECORD)", idx))
            for s in o["subs"]:
                _check_var(s, idx, s["sub"], fmt, deftypes, out)
            for g in o.get("guess") or []:
                out.append(_p("warn", "ARRAY/RECORD guessed: check it" if g == "structure" else g, idx))
        else:
            _check_var(o, idx, None, fmt, deftypes, out)
    for idx, n in seen.items():
        if n > 1:
            out.append(_p("error", "the same index twice", idx))
    for idx in MANDATORY:
        if find(doc, idx) is None:
            out.append(_p("error", f"mandatory object 0x{idx:04X} is missing", idx))
    ident = find(doc, 0x1018)
    if ident is not None and find_var(doc, 0x1018, 1) is None:
        out.append(_p("error", "0x1018 needs sub-index 1 (vendor-ID)", 0x1018))
    _check_device_info(doc, out)
    _check_pdos(doc, out)
    order = {"error": 0, "warn": 1, "info": 2}
    out.sort(key=lambda p: (order.get(p["level"], 3), p["index"] if p["index"] is not None else -1,
                            p["sub"] if p["sub"] is not None else -1))
    return out


def _check_device_info(doc: dict, out: list) -> None:
    di = section(doc, "DeviceInfo")
    if di is None:
        out.append(_p("error", "no [DeviceInfo] section"))
        return
    low = {k.lower(): v for k, v in di.items()}
    if not any(str(low.get(f"baudrate_{b}", "0")).strip() == "1" for b in (10, 20, 50, 125, 250, 500, 800, 1000)):
        out.append(_p("warn", "DeviceInfo: no supported bit rate (BaudRate_xxx=1)"))
    for key, label, sub in (("vendornumber", "VendorNumber", 1), ("productnumber", "ProductNumber", 2),
                            ("revisionnumber", "RevisionNumber", 3)):
        v = find_var(doc, 0x1018, sub)
        if v is None or not str(low.get(key, "")).strip() or not str(v.get("default", "")).strip():
            continue
        try:
            if parse_int(low[key]) != parse_int(v["default"]):
                out.append(_p("warn", f"DeviceInfo {label} {low[key]} differs from the default of 0x1018:{sub:02X} "
                                      f"({v['default']})", 0x1018, sub))
        except ValueError:
            pass


_RESTRICTED = ((0x000, 0x000, "NMT"), (0x001, 0x07F, "reserved"), (0x080, 0x080, "SYNC"), (0x081, 0x0FF, "EMCY"),
               (0x100, 0x100, "TIME"), (0x101, 0x180, "reserved"), (0x581, 0x5FF, "SDO (server to client)"),
               (0x601, 0x67F, "SDO (client to server)"), (0x6E0, 0x6FF, "reserved"), (0x701, 0x77F, "heartbeat"),
               (0x780, 0x7FF, "reserved (LSS)"))


def _restricted(cid: int) -> str:
    """Why an 11-bit CAN id must not be used for a PDO (CiA 301 restricted ids), or ""."""
    for lo, hi, what in _RESTRICTED:
        if lo <= cid <= hi:
            return f"restricted by CiA 301 ({what})"
    return ""


def _check_pdos(doc: dict, out: list) -> None:
    fmt = doc.get("format", "eds")
    node_id = None                                     # a DCF for one node: compare the ids on the bus
    if fmt == "dcf":
        try:
            node_id = parse_int((section(doc, "DeviceComissioning") or {}).get("NodeID", "")) or None
        except ValueError:
            pass
    cobs: dict[tuple, tuple[str, str]] = {}
    for kind, cbase, mbase in (("RPDO", 0x1400, 0x1600), ("TPDO", 0x1800, 0x1A00)):
        for n in range(0x200):
            comm, mp = find(doc, cbase + n), find(doc, mbase + n)
            if comm is None and mp is None:
                continue
            label = f"{kind} {n + 1}"
            if comm is None:
                out.append(_p("warn", f"{label}: a mapping object without its communication object "
                                      f"0x{cbase + n:04X}", mbase + n))
            if mp is None:
                out.append(_p("warn", f"{label}: a communication object without its mapping object "
                                      f"0x{mbase + n:04X}", cbase + n))
            valid = True
            if comm is not None:
                s1 = find_var(doc, cbase + n, 1)
                if s1 is None:
                    out.append(_p("error", f"{label}: no COB-ID (sub-index 1)", cbase + n))
                else:
                    c = _cob(ref_value(s1, fmt))
                    if c is not None:
                        valid = not c[0] & 0x80000000
                        bits = c[0] & 0x3FFFFFFF                # id and the 29-bit flag (bit 29)
                        key = (bits + node_id, False) if node_id and c[1] else (bits, c[1])
                        if valid and not bits & 0x20000000:
                            for nid in ([node_id] if node_id else [1, 127]) if c[1] else [0]:
                                cid = (bits & 0x7FF) + nid
                                why = "beyond 0x7FF, the last 11-bit id" if cid > 0x7FF else _restricted(cid)
                                if why:
                                    out.append(_p("error", f"{label}: CAN id 0x{cid:03X}" + (f" (node {nid})" if nid
                                                  else "") + f" is {why}", cbase + n, 1))
                                    break
                        if valid and key in cobs:
                            other, okind = cobs[key]
                            # two RPDOs may listen to the same frame; a TPDO must not share its id
                            level = "warn" if kind == okind == "RPDO" else "error"
                            out.append(_p(level, f"{label} uses the same COB-ID as {other}", cbase + n, 1))
                        elif valid:
                            cobs[key] = (label, kind)
            if mp is not None:
                _check_mapping(doc, mp, kind, label, fmt, out)


def _check_mapping(doc: dict, mp: dict, kind: str, label: str, fmt: str, out: list) -> None:
    idx = mp["index"]
    s0 = find_var(doc, idx, 0)
    if s0 is None:
        return
    try:
        count = parse_int(ref_value(s0, fmt) or "0")
    except ValueError:
        return
    if count > 64:
        out.append(_p("error", f"{label}: {count} mapped objects (at most 64)", idx, 0))
        return
    bits = 0
    for i in range(1, count + 1):
        e = find_var(doc, idx, i)
        if e is None:
            out.append(_p("error", f"{label}: sub-index 0 says {count} entries but sub-index {i} is missing", idx, i))
            continue
        try:
            entry = parse_int(ref_value(e, fmt) or "0")
        except ValueError:
            continue
        t_idx, t_sub, t_bits = entry >> 16, (entry >> 8) & 0xFF, entry & 0xFF
        if entry == 0:
            out.append(_p("error", f"{label}: entry {i} is empty but sub-index 0 says {count} entries", idx, i))
            continue
        bits += t_bits
        if 0x0001 <= t_idx <= 0x0007:                       # dummy entry: just a gap of that type's size
            size = DATA_TYPES.get(t_idx, ("", "", 0))[2]
            if kind == "TPDO":
                out.append(_p("error", f"{label}: dummy entries are only allowed in RPDOs", idx, i))
            if size and t_bits != size:
                out.append(_p("error", f"{label}: dummy {type_name(t_idx)} mapped with {t_bits} bits", idx, i))
            usage = {k.lower(): v for k, v in (section(doc, "DummyUsage") or {}).items()}
            if str(usage.get(f"dummy{t_idx:04d}", "0")).strip() != "1":
                out.append(_p("warn", f"{label}: maps dummy {type_name(t_idx)} but [DummyUsage] "
                                      f"Dummy{t_idx:04d} is not 1", idx, i))
            continue
        target = find_var(doc, t_idx, t_sub)
        where = f"0x{t_idx:04X}:{t_sub:02X}"
        if target is None:
            out.append(_p("error", f"{label}: maps {where}, which does not exist", idx, i))
            continue
        if not target.get("pdo"):
            out.append(_p("error", f"{label}: maps {where} ({target.get('name', '')}), which is not PDO-mappable", idx, i))
        size = DATA_TYPES.get(target.get("data_type"), ("", "", 0))[2]
        if size and t_bits != size:
            out.append(_p("error", f"{label}: maps {where} with {t_bits} bits, but it is {type_name(target['data_type'])}"
                                   f" ({size} bits)", idx, i))
        acc = target.get("access")
        if kind == "TPDO" and acc not in READABLE:
            out.append(_p("error", f"{label}: maps {where}, which is {acc} (a TPDO needs a readable object)", idx, i))
        if kind == "RPDO" and acc not in WRITABLE:
            out.append(_p("error", f"{label}: maps {where}, which is {acc} (an RPDO needs a writable object)", idx, i))
        if (kind, acc) in (("TPDO", "rww"), ("RPDO", "rwr")):
            out.append(_p("warn", f"{label}: maps {where}, which is {acc} (meant for "
                                  f"{'RPDOs' if acc == 'rww' else 'TPDOs'})", idx, i))
    if bits > 64:
        out.append(_p("error", f"{label}: {bits} bits mapped, more than the 64 of a classic CAN frame", idx, 0))


# ------------------------------------------------------------------------------------------- PDOs

PDO_KINDS = {"rpdo": (0x1400, 0x1600), "tpdo": (0x1800, 0x1A00)}


def transmission_text(t: Optional[int]) -> str:
    """CiA 301 transmission type, in words."""
    if t is None:
        return ""
    if t == 0:
        return "synchronous, acyclic"
    if 1 <= t <= 240:
        return f"synchronous, every {t} SYNC" if t > 1 else "synchronous, every SYNC"
    return {252: "RTR only, synchronous", 253: "RTR only, event-driven", 254: "event-driven (manufacturer)",
            255: "event-driven (device profile)"}.get(t, f"reserved ({t})")


def encode_entry(index: int, sub: int, bits: int) -> int:
    """A PDO mapping entry: index (16 bits), sub-index (8), length in bits (8)."""
    return ((index & 0xFFFF) << 16) | ((sub & 0xFF) << 8) | (bits & 0xFF)


def decode_entry(entry: int) -> tuple[int, int, int]:
    return (entry >> 16) & 0xFFFF, (entry >> 8) & 0xFF, entry & 0xFF


def decode_cob(text: str, node_id: Optional[int] = None) -> dict:
    """A PDO COB-ID value: the CAN id, valid (bit 31 clear), RTR allowed (bit 30 clear), 29-bit (bit 29),
    whether it follows the node id, and the id for node_id."""
    try:
        raw = parse_int(text, 0)
    except ValueError:
        return {"text": text, "error": f"{text!r} is not a number"}
    ext = bool(raw & 0x20000000)
    rel = uses_nodeid(text)
    base = raw & (0x1FFFFFFF if ext else 0x7FF)
    return {"text": text, "valid": not raw & 0x80000000, "rtr_allowed": not raw & 0x40000000, "extended": ext,
            "node_relative": rel, "id": base, "id_for_node": (base + node_id) if rel and node_id else base}


def pdos(doc: dict, node_id: Optional[int] = None) -> list[dict]:
    """Every PDO of the document, decoded: communication parameters and the mapped objects with their place
    in the frame. Uses the DCF values where a DCF has them."""
    fmt = doc.get("format", "eds")

    def val(index: int, sub: int) -> Optional[int]:
        v = find_var(doc, index, sub)
        try:
            return parse_int(ref_value(v, fmt), node_id) if v is not None and str(ref_value(v, fmt)).strip() else None
        except ValueError:
            return None

    out = []
    for kind, (cbase, mbase) in PDO_KINDS.items():
        for n in range(0x200):
            comm, mp = find(doc, cbase + n), find(doc, mbase + n)
            if comm is None and mp is None:
                continue
            cob_var = find_var(doc, cbase + n, 1)
            p = {"kind": kind, "number": n + 1, "comm": cbase + n if comm else None, "mapping": mbase + n if mp else None,
                 "cob": decode_cob(ref_value(cob_var, fmt), node_id) if cob_var else None,
                 "transmission": val(cbase + n, 2), "inhibit_100us": val(cbase + n, 3), "event_ms": val(cbase + n, 5),
                 "sync_start": val(cbase + n, 6), "entries": [], "bits": 0}
            p["transmission_text"] = transmission_text(p["transmission"])
            count = val(mbase + n, 0) or 0
            pos = 0
            for i in range(1, min(count, 64) + 1):
                entry = val(mbase + n, i) or 0
                idx, sub, bits = decode_entry(entry)
                t = find_var(doc, idx, sub)
                name = (t or {}).get("name") or (f"dummy {type_name(idx)}" if 1 <= idx <= 7 else "")
                p["entries"].append({"sub": i, "entry": f"0x{entry:08X}", "index": idx, "target_sub": sub, "bits": bits,
                                     "name": name, "start_bit": pos, "exists": t is not None or 1 <= idx <= 7})
                pos += bits
            p["bits"] = pos
            out.append(p)
    return out


def add_pdo(doc: dict, kind: str) -> int:
    """Add the next free RPDO or TPDO (communication and mapping object); returns its number (1-based)."""
    cbase, mbase = PDO_KINDS[kind]
    for n in range(0x200):
        if find(doc, cbase + n) is None and find(doc, mbase + n) is None:
            _pdo_pair(doc, kind, n, [])
            doc["objects"].sort(key=lambda o: o["index"])
            return n + 1
    raise EdsError(f"all 512 {kind.upper()}s are in use")
