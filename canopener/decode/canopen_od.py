"""CANopen decoding: an EDS/XDD per node (PDO decode, the object dictionary for the SDO browser), EMCY and heartbeats.

PDOs are decoded from the mapping in the EDS (default values) or from the mapping read from the device over SDO.
Like the J1939 decoder this runs on the decode thread under the manager's lock.
"""

from __future__ import annotations

import collections
import re
import struct
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import canopen
from canopen.objectdictionary import ODArray, ODRecord, ODVariable, datatypes as dt

NMT_STATES = {0: "boot-up", 4: "stopped", 5: "operational", 127: "pre-operational"}
NMT_COMMANDS = ("start", "stop", "preop", "reset_node", "reset_comm")

# CiA 301 emergency error code classes, by the high byte (a sub-class) or its high nibble (the class)
EMCY_CLASSES = {
    0x00: "error reset / no error", 0x10: "generic error", 0x20: "current", 0x21: "current, device input side",
    0x22: "current inside the device", 0x23: "current, device output side", 0x30: "voltage", 0x31: "mains voltage",
    0x32: "voltage inside the device", 0x33: "output voltage", 0x40: "temperature", 0x41: "ambient temperature",
    0x42: "device temperature", 0x50: "device hardware", 0x60: "device software", 0x61: "internal software",
    0x62: "user software", 0x63: "data set", 0x70: "additional modules", 0x80: "monitoring",
    0x81: "communication", 0x82: "protocol error", 0x90: "external error", 0xF0: "additional functions",
    0xFF: "device specific",
}
EMCY_CODES = {
    0x8110: "CAN overrun (objects lost)", 0x8120: "CAN in error passive mode", 0x8130: "life guard / heartbeat error",
    0x8140: "recovered from bus off", 0x8150: "CAN-ID collision", 0x8210: "PDO not processed (length error)",
    0x8220: "PDO length exceeded", 0x8230: "DAM MPDO not processed", 0x8240: "unexpected SYNC data length",
    0x8250: "RPDO timeout",
}
ERROR_REGISTER_BITS = ("generic", "current", "voltage", "temperature", "communication", "device profile",
                       "reserved", "manufacturer")

# OD data type -> the value type of a node sequence step (nodes/sequence.py pack_value)
SEQ_TYPES = {dt.BOOLEAN: "u8", dt.INTEGER8: "i8", dt.INTEGER16: "i16", dt.INTEGER32: "i32", dt.UNSIGNED8: "u8",
             dt.UNSIGNED16: "u16", dt.UNSIGNED32: "u32", dt.REAL32: "f32", dt.VISIBLE_STRING: "str",
             dt.INTEGER64: "i64", dt.UNSIGNED64: "u64"}


def emcy_text(code: int) -> str:
    cls = EMCY_CLASSES.get(code >> 8) or EMCY_CLASSES.get((code >> 8) & 0xF0, "unknown class")
    return f"{cls}: {EMCY_CODES[code]}" if code in EMCY_CODES else cls


def decode_emcy(data: bytes) -> dict:
    code = data[0] | (data[1] << 8) if len(data) >= 2 else 0
    reg = data[2] if len(data) > 2 else 0
    return {"code": code, "code_hex": f"0x{code:04X}", "text": emcy_text(code), "register": reg,
            "register_bits": [n for i, n in enumerate(ERROR_REGISTER_BITS) if reg >> i & 1],
            "vendor": data[3:8].hex(" ").upper()}


# ------------------------------------------------------------------------------------------- object dictionary

def _xdd_value(text: Optional[str], data_type: int, node_id: int):
    if text is None or text == "":
        return None
    if data_type in (dt.VISIBLE_STRING, dt.UNICODE_STRING):
        return text
    if data_type in dt.INTEGER_TYPES or data_type == dt.BOOLEAN:
        s = text.replace(" ", "")
        if "$NODEID" in s.upper():
            s = re.sub(r"\+?\$NODEID\+?", "", s, flags=re.I) or "0"
            return int(s, 0) + node_id
        return int(s, 0)
    if data_type in dt.FLOAT_TYPES:
        return float(text)
    return None


def _xdd_variable(el, index: int, sub: int, node_id: int) -> ODVariable:
    var = ODVariable(el.get("name", f"0x{index:04X}"), index, sub)
    var.data_type = int(el.get("dataType", "0"), 16)
    var.access_type = el.get("accessType", "rw")
    var.default = _xdd_value(el.get("defaultValue"), var.data_type, node_id)
    var.min = _xdd_value(el.get("lowLimit"), var.data_type, node_id)
    var.max = _xdd_value(el.get("highLimit"), var.data_type, node_id)
    return var


def import_xdd(path: str, node_id: int) -> canopen.ObjectDictionary:
    """A CiA 311 XDD file (the CANopen object list only)."""
    od = canopen.ObjectDictionary()
    for el in ET.parse(path).getroot().iter():
        if not el.tag.endswith("CANopenObject"):
            continue
        index = int(el.get("index"), 16)
        subs = [s for s in el if s.tag.endswith("CANopenSubObject")]
        otype = int(el.get("objectType", "7"))
        if otype == 7 or not subs:
            od.add_object(_xdd_variable(el, index, 0, node_id))
            continue
        obj = ODArray(el.get("name", ""), index) if otype == 8 else ODRecord(el.get("name", ""), index)
        for s in subs:
            obj.add_member(_xdd_variable(s, index, int(s.get("subIndex"), 16), node_id))
        od.add_object(obj)
    return od


def load_od(path: str, node_id: int) -> canopen.ObjectDictionary:
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"{p} not found")
    try:
        if p.suffix.lower() == ".xdd":
            return import_xdd(str(p), node_id)
        return canopen.import_od(str(p), node_id)
    except Exception as exc:
        raise ValueError(f"cannot read {p.name}: {exc}") from exc


def od_entries(od) -> list[dict]:
    """Every variable of an object dictionary, for the SDO browser."""
    out = []
    for index in sorted(od.indices):
        obj = od.indices[index]
        members = [obj] if isinstance(obj, ODVariable) else [obj[s] for s in sorted(obj.subindices)]
        for v in members:
            out.append({"index": v.index, "sub": v.subindex, "name": v.qualname,
                        "type": SEQ_TYPES.get(v.data_type, "bytes"), "data_type": v.data_type, "access": v.access_type,
                        "unit": v.unit, "default": v.default if isinstance(v.default, (int, float, str)) else None})
    return out


def value_of(var: Optional[ODVariable], data: bytes):
    """Decode SDO / PDO bytes with the OD variable (factor applied, choices as text); raw hex without one."""
    if var is None:
        return data.hex(" ").upper()
    try:
        value = var.decode_raw(data)
    except Exception:
        return data.hex(" ").upper()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex(" ").upper()
    if var.data_type == dt.BOOLEAN:
        return bool(value)
    if isinstance(value, int) and var.factor != 1:
        value = value * var.factor
    return value


# ------------------------------------------------------------------------------------------- PDOs

class Pdo:
    """One PDO of one node: COB-ID and the mapped variables (name, OD variable or None, bit offset, bit length)."""
    __slots__ = ("node", "name", "cob_id", "entries", "channel", "prefix", "meta")

    def __init__(self, node: int, name: str, cob_id: int, mapping: list[int], od, channel: Optional[int]):
        self.node, self.name, self.cob_id, self.channel = node, name, cob_id, channel
        self.prefix = f"canopen/{node}/{name}."
        self.entries = []
        self.meta = {}
        offset = 0
        for m in mapping:
            index, sub, bits = m >> 16, (m >> 8) & 0xFF, m & 0xFF
            var = od.get_variable(index, sub) if index > 0x0FFF else None     # below 0x1000: dummy mapping
            if index > 0x0FFF:
                name_ = var.qualname if var is not None else f"0x{index:04X}:{sub:02X}"
                self.entries.append((name_, var, offset, bits))
                if var is not None:
                    self.meta[name_] = (var.unit or "", var.min, var.max, dict(var.value_descriptions))
            offset += bits

    def decode(self, data: bytes) -> list[tuple[str, object]]:
        raw = int.from_bytes(data, "little")
        nbits = len(data) * 8
        out = []
        for name, var, off, bits in self.entries:
            if off + bits > nbits:
                continue
            v = (raw >> off) & ((1 << bits) - 1)
            if var is not None and bits % 8 == 0:
                out.append((name, value_of(var, v.to_bytes(bits // 8, "little"))))
            else:
                out.append((name, bool(v) if bits == 1 else v))
        return out

    def info(self) -> dict:
        return {"name": self.name, "cob_id": self.cob_id,
                "entries": [{"name": n, "offset": o, "bits": b} for n, _, o, b in self.entries]}


def _default(od, index: int, sub: int) -> Optional[int]:
    """The configured value (a DCF's ParameterValue) if there is one, else the EDS default."""
    var = od.get_variable(index, sub)
    if var is None:
        return None
    value = getattr(var, "value", None)
    if isinstance(value, int):
        return value
    return var.default if isinstance(var.default, int) else None


def _pdo_slots(od):
    """(name, communication index, mapping index) for every PDO the OD describes."""
    for comm, mapping, kind in ((0x1800, 0x1A00, "TPDO"), (0x1400, 0x1600, "RPDO")):
        for i in range(512):
            if comm + i in od and mapping + i in od:
                yield f"{kind}{i + 1}", comm + i, mapping + i


def _valid_cob(cob: Optional[int]) -> Optional[int]:
    if cob is None or cob & 0x80000000 or cob & 0x20000000:      # disabled, or a 29-bit PDO (not decoded)
        return None
    return cob & 0x7FF


def pdos_from_od(od, node: int, channel: Optional[int]) -> list[Pdo]:
    out = []
    for name, comm, mapping in _pdo_slots(od):
        cob = _valid_cob(_default(od, comm, 1))
        if cob is None:
            continue
        count = _default(od, mapping, 0)
        if count is None:
            count = len(od[mapping]) - 1 if not isinstance(od[mapping], ODVariable) else 0
        values = [_default(od, mapping, s) or 0 for s in range(1, count + 1)]
        out.append(Pdo(node, name, cob, values, od, channel))
    return out


def pdos_from_device(net, od, node: int, channel: Optional[int]) -> list[Pdo]:
    """Read the PDO communication + mapping parameters over SDO (uploads only). Raises on the first failure."""
    remote = net.add_node(node, od)
    u32 = lambda b: struct.unpack_from("<I", b.ljust(4, b"\x00"))[0]   # noqa: E731
    out = []
    try:
        for name, comm, mapping in _pdo_slots(od):
            cob = _valid_cob(u32(remote.sdo.upload(comm, 1)))
            if cob is None:
                continue
            count = remote.sdo.upload(mapping, 0)[0]
            values = [u32(remote.sdo.upload(mapping, s)) for s in range(1, count + 1)]
            out.append(Pdo(node, name, cob, values, od, channel))
    finally:
        del net[node]
    return out


class NodeEds:
    def __init__(self, node: int, path: str, channel: Optional[int]):
        self.node, self.path, self.channel = node, path, channel
        self.od = load_od(path, node)
        self.pdos = pdos_from_od(self.od, node, channel)
        self.source = "eds"                        # eds | device (PDO mapping read over SDO)

    def info(self) -> dict:
        return {"node": self.node, "path": self.path, "channel": self.channel, "source": self.source,
                "pdos": [p.info() for p in self.pdos], "objects": len(self.od.indices)}


class CanopenDecoder:
    def __init__(self, store):
        self.store = store
        self.nodes: dict[tuple[Optional[int], int], NodeEds] = {}     # (channel or None = all, node id)
        self.by_cob: dict[int, list[Pdo]] = {}
        self.nmt: dict[tuple[int, int], dict] = {}                    # (channel, node) -> state from heartbeats
        self.emcy: collections.deque = collections.deque(maxlen=500)

    def assign(self, node: int, path: str, channel: Optional[int]) -> NodeEds:
        if not 1 <= node <= 127:
            raise ValueError("node id must be 1..127")
        n = NodeEds(node, path, channel)
        self.nodes[(channel, node)] = n
        self._index()
        return n

    def remove(self, node: int, channel: Optional[int]) -> None:
        self.nodes.pop((channel, node), None)
        self._index()

    def set_pdos(self, n: NodeEds, pdos: list[Pdo], source: str) -> None:
        n.pdos, n.source = pdos, source
        self._index()

    def _index(self) -> None:
        by_cob: dict[int, list] = {}
        for n in self.nodes.values():
            for p in n.pdos:
                by_cob.setdefault(p.cob_id, []).append(p)
        self.by_cob = by_cob

    def node_for(self, node: int, ch: int) -> Optional[NodeEds]:
        return self.nodes.get((ch, node)) or self.nodes.get((None, node))

    def clear(self) -> None:
        self.nmt.clear()
        self.emcy.clear()

    def handle(self, ch: int, ts: float, cid: int, data: bytes) -> Optional[str]:
        """Decode one 11-bit frame (PDO of an assigned node, heartbeat, EMCY); the receive-table text or None."""
        pdos = self.by_cob.get(cid)
        if pdos:
            for p in pdos:
                if p.channel is None or p.channel == ch:
                    return self._pdo(p, ch, ts, data)
        if 0x700 < cid < 0x780 and len(data) == 1:
            node, state = cid - 0x700, data[0] & 0x7F
            if state in NMT_STATES:
                self.nmt[(ch, node)] = {"channel": ch, "node": node, "state": NMT_STATES[state], "ts": ts}
                self.store.publish(f"canopen/{node}/NMT", ts, state, choices=NMT_STATES, channel=ch, can_id=cid)
                return f"Heartbeat node {node}: {NMT_STATES[state]}"
        elif 0x80 < cid < 0x100 and len(data) == 8:
            node = cid - 0x80
            if (ch, node) in self.nmt or self.node_for(node, ch) is not None:     # a known CANopen node only
                e = decode_emcy(data)
                self.emcy.append({"ts": ts, "channel": ch, "node": node, **e})
                self.store.publish(f"canopen/{node}/EMCY.code", ts, e["code"], channel=ch, can_id=cid)
                self.store.publish(f"canopen/{node}/EMCY.register", ts, e["register"], channel=ch, can_id=cid)
                return (f"EMCY node {node}: {e['code_hex']} {e['text']}, register 0x{e['register']:02X}"
                        + (f" ({', '.join(e['register_bits'])})" if e["register_bits"] else ""))
        return None

    def _pdo(self, p: Pdo, ch: int, ts: float, data: bytes) -> str:
        parts = []
        publish, prefix, meta = self.store.publish, p.prefix, p.meta
        for name, value in p.decode(data):
            m = meta.get(name)
            if m:
                publish(prefix + name, ts, value, unit=m[0], minimum=m[1], maximum=m[2], choices=m[3], channel=ch,
                        can_id=p.cob_id)
                if m[3] and value in m[3]:
                    parts.append(f"{name}={m[3][value]}")
                    continue
                shown = round(value, 4) if isinstance(value, float) else value
                parts.append(f"{name}={shown}{' ' + m[0] if m[0] else ''}")
            else:
                publish(prefix + name, ts, value, channel=ch, can_id=p.cob_id)
                parts.append(f"{name}={value}")
        return f"{p.name} node {p.node} " + " ".join(parts)

    # ------------------------------------------------------------------ readers
    def nodes_info(self) -> list[dict]:
        return [n.info() for n in sorted(self.nodes.values(), key=lambda n: (n.node, n.channel is not None))]

    def nmt_list(self) -> list[dict]:
        return sorted(self.nmt.values(), key=lambda n: (n["channel"], n["node"]))
