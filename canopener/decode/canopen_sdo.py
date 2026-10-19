"""SDO traffic seen on the bus (CiA 301): expedited and segmented uploads/downloads reassembled, aborts with their
text. Requests from this tool (frames flagged Tx) are told apart from other masters on the bus.

Only listens: one transfer per (channel, node, server) is followed from the client request on 0x600 + node to the
server response on 0x580 + node, or on an extra SDO server's COB-ID pair set for the node (`server` names it, for
example "0x627/0x5A7"; "" is the default server). Block transfers are logged as such and not decoded. Runs on the
decode thread under the manager's lock (like the other decoders).
"""

from __future__ import annotations

import collections
from typing import Optional

from canopen.sdo.exceptions import SdoAbortedError

BLOCK_IDLE_S = 1.0          # a block transfer we cannot follow ends after this much silence
MAX_VALUES = 20_000         # last value per object: a non-SDO sender on SDO ids must not grow it without end

# CiA 301 communication profile area (0x1000-0x1FFF): names for nodes without an EDS
CIA301_NAMES = {
    0x1000: "Device type", 0x1001: "Error register", 0x1002: "Manufacturer status register",
    0x1003: "Pre-defined error field", 0x1005: "COB-ID SYNC", 0x1006: "Communication cycle period",
    0x1007: "Synchronous window length", 0x1008: "Manufacturer device name", 0x1009: "Manufacturer hardware version",
    0x100A: "Manufacturer software version", 0x100C: "Guard time", 0x100D: "Life time factor",
    0x1010: "Store parameters", 0x1011: "Restore default parameters", 0x1012: "COB-ID time stamp",
    0x1013: "High resolution time stamp", 0x1014: "COB-ID EMCY", 0x1015: "Inhibit time EMCY",
    0x1016: "Consumer heartbeat time", 0x1017: "Producer heartbeat time", 0x1018: "Identity object",
    0x1019: "Synchronous counter overflow value", 0x1020: "Verify configuration", 0x1029: "Error behavior",
    0x1F50: "Program data", 0x1F51: "Program control", 0x1F56: "Program software identification",
    0x1F57: "Flash status identification", 0x1F80: "NMT startup",
}
CIA301_RANGES = ((0x1200, 0x127F, "SDO server parameter"), (0x1280, 0x12FF, "SDO client parameter"),
                 (0x1400, 0x15FF, "RPDO communication parameter"), (0x1600, 0x17FF, "RPDO mapping parameter"),
                 (0x1800, 0x19FF, "TPDO communication parameter"), (0x1A00, 0x1BFF, "TPDO mapping parameter"))
IDENTITY_SUBS = {1: "Vendor-ID", 2: "Product code", 3: "Revision number", 4: "Serial number"}


def cia301_name(index: int, sub: int) -> str:
    if index == 0x1018 and sub in IDENTITY_SUBS:
        return f"Identity object.{IDENTITY_SUBS[sub]}"
    if index in CIA301_NAMES:
        return CIA301_NAMES[index]
    for lo, hi, name in CIA301_RANGES:
        if lo <= index <= hi:
            return f"{name} {index - lo + 1}"
    return ""


def abort_text(code: int) -> str:
    return SdoAbortedError.CODES.get(code, "unknown abort code")


def _u32(b: bytes) -> int:
    return int.from_bytes(b[:4].ljust(4, b"\x00"), "little")


class SdoSniffer:
    """Follows SDO transfers per (channel, node, server). `describe(ch, node, index, sub, data)` gives (name, value,
    text, type) for a finished transfer (the node's EDS when it has one; type as in the object dictionary, or "")."""

    def __init__(self, describe, maxlen: int = 2000):
        self.describe = describe
        self.log: collections.deque = collections.deque(maxlen=maxlen)
        self.values: dict[tuple[int, int, int, int], dict] = {}     # (ch, node, index, sub) -> last value seen
        self.pending: dict[tuple[int, int, str], dict] = {}

    def clear(self) -> None:
        self.log.clear()
        self.values.clear()
        self.pending.clear()

    def active(self, ch: int, node: int, server: str = "") -> bool:
        return (ch, node, server) in self.pending

    # ------------------------------------------------------------------ frames
    def request(self, ch: int, ts: float, node: int, d: bytes, tx: bool, server: str = "") -> str:
        """A frame on 0x600 + node, or the request id of an extra server (client -> server)."""
        key = (ch, node, server)
        p = self.pending.get(key)
        if p is not None and p.get("block"):
            seen = self._block_frame(key, p, ts, d, "request")
            if seen is not None:
                return seen
            p = None                                  # the block transfer had ended unseen: a new request
        ccs = d[0] >> 5
        index, sub = d[1] | (d[2] << 8), d[3]
        source = "CANopener" if tx else "bus"
        if ccs in (1, 2, 5, 6):                       # a new transfer: one still open got no (complete) answer
            if p is not None:
                self._finish(key, p, ts, "no response" if not p.get("segmented") else "incomplete")
            t = {"ch": ch, "node": node, "server": server, "t0": ts, "source": source, "index": index, "sub": sub,
                 "op": "write" if ccs in (1, 6) else "read", "data": bytearray(), "size": None, "toggle": 0}
            if ccs == 1:
                e, s, n = d[0] >> 1 & 1, d[0] & 1, d[0] >> 2 & 3
                if e:
                    t["data"] = bytearray(d[4:8 - n] if s else d[4:8])
                else:
                    t["segmented"] = True
                    t["size"] = _u32(d[4:8]) if s else None
            elif ccs in (5, 6):
                t["block"] = True
                t["last"] = ts
                t["data_side"] = "request" if ccs == 6 else "response"     # download: the client sends the data
                self._finish(key, t, ts, "block transfer, not decoded", keep=True)
                return f"SDO block {'download' if ccs == 6 else 'upload'} node {node} 0x{index:04X}:{sub:02X}"
            self.pending[key] = t
            return f"SDO {t['op']} node {node} 0x{index:04X}:{sub:02X}"
        if ccs == 0 and p is not None and p.get("segmented") and p["op"] == "write":
            if (d[0] >> 4 & 1) != p["toggle"]:
                self._finish(key, p, ts, "toggle bit error")
                return f"SDO segment node {node} (toggle bit error)"
            p["data"] += d[1:8 - (d[0] >> 1 & 7)]
            p["last_segment"] = bool(d[0] & 1)
            return f"SDO download segment node {node}"
        if ccs == 3 and p is not None:
            return f"SDO upload segment request node {node}"
        if ccs == 4:
            code = _u32(d[4:8])
            t = p or {"ch": ch, "node": node, "server": server, "t0": ts, "op": "", "data": bytearray(), "size": None}
            t.update(index=index, sub=sub, source=source)
            self._finish(key, t, ts, "abort", code=code, by="client")
            return f"SDO abort node {node} 0x{index:04X}:{sub:02X} 0x{code:08X} {abort_text(code)}"
        return ""

    def response(self, ch: int, ts: float, node: int, d: bytes, server: str = "") -> str:
        """A frame on 0x580 + node, or the response id of an extra server (server -> client)."""
        key = (ch, node, server)
        p = self.pending.get(key)
        if p is not None and p.get("block"):
            seen = self._block_frame(key, p, ts, d, "response")
            if seen is not None:
                return seen
            p = None
        scs = d[0] >> 5
        index, sub = d[1] | (d[2] << 8), d[3]
        if scs == 4:
            code = _u32(d[4:8])
            t = p or {"ch": ch, "node": node, "server": server, "t0": ts, "op": "", "source": "bus",
                      "data": bytearray(), "size": None}
            t.update(index=index, sub=sub)
            self._finish(key, t, ts, "abort", code=code, by="server")
            return f"SDO abort node {node} 0x{index:04X}:{sub:02X} 0x{code:08X} {abort_text(code)}"
        if p is None:                                 # the request was not seen (on Kvaser: sent by another program
            t = {"ch": ch, "node": node, "server": server, "t0": ts, "source": "bus", "index": index, "sub": sub,
                 "data": bytearray(), "size": None}  # on the same channel), only what the reply says alone
            if scs == 2 and d[0] >> 1 & 1:           # an expedited upload: the value is in the reply
                n, s = d[0] >> 2 & 3, d[0] & 1
                t.update(op="read", data=bytearray(d[4:8 - n] if s else d[4:8]))
                self._finish(key, t, ts, "ok")
                return f"SDO read node {node} 0x{index:04X}:{sub:02X} (request not seen)"
            if scs == 3:                              # a download acknowledged: written, the value is unknown
                t["op"] = "write"
                self._finish(key, t, ts, "request not seen")
                return f"SDO write node {node} 0x{index:04X}:{sub:02X} acknowledged (request not seen)"
            return ""
        if p["op"] == "write":
            if scs == 3 and not p.get("segmented"):
                self._finish(key, p, ts, "ok")
                return f"SDO write node {node} 0x{p['index']:04X}:{p['sub']:02X} done"
            if scs == 3:
                return f"SDO download node {node} 0x{p['index']:04X}:{p['sub']:02X} accepted"
            if scs == 1:
                p["toggle"] ^= 1
                if p.get("last_segment"):
                    self._finish(key, p, ts, "ok")
                    return f"SDO write node {node} 0x{p['index']:04X}:{p['sub']:02X} done"
                return f"SDO download segment ack node {node}"
            return ""
        if scs == 2:                                  # initiate upload response
            if d[0] >> 1 & 1:
                n, s = d[0] >> 2 & 3, d[0] & 1
                p["data"] = bytearray(d[4:8 - n] if s else d[4:8])
                self._finish(key, p, ts, "ok")
                return f"SDO read node {node} 0x{p['index']:04X}:{p['sub']:02X} done"
            p["segmented"] = True
            p["size"] = _u32(d[4:8]) if d[0] & 1 else None
            return f"SDO upload node {node} 0x{p['index']:04X}:{p['sub']:02X} segmented"
        if scs == 0 and p.get("segmented"):
            if (d[0] >> 4 & 1) != p["toggle"]:
                self._finish(key, p, ts, "toggle bit error")
                return f"SDO segment node {node} (toggle bit error)"
            p["toggle"] ^= 1
            p["data"] += d[1:8 - (d[0] >> 1 & 7)]
            if d[0] & 1:
                self._finish(key, p, ts, "ok")
                return f"SDO read node {node} 0x{p['index']:04X}:{p['sub']:02X} done"
            return f"SDO upload segment node {node}"
        return ""

    def _block_frame(self, key, p: dict, ts: float, d: bytes, side: str) -> Optional[str]:
        """Inside a block transfer: frames are data segments we do not follow. It ends with an abort, the end
        confirmation (0xA1, from the side that does not send the data: a last data segment with sequence number
        33 is 0xA1 too) or silence. None: it had already ended in silence; the caller handles the frame anew."""
        if ts - p["last"] > BLOCK_IDLE_S:
            del self.pending[key]
            return None
        p["last"] = ts
        if d[0] == 0x80 or (d[0] == 0xA1 and side != p.get("data_side")):
            del self.pending[key]
        return "SDO block transfer segment"

    def _finish(self, key, t: dict, ts: float, result: str, code: Optional[int] = None, by: str = "",
                keep: bool = False) -> None:
        if not keep:
            self.pending.pop(key, None)
        else:
            self.pending[key] = t
        data = bytes(t["data"])
        ok = result == "ok"
        name, value, text, type_ = self.describe(t["ch"], t["node"], t["index"], t["sub"], data if ok else None)
        e = {"ts": ts, "channel": t["ch"], "node": t["node"], "server": t["server"], "source": t["source"],
             "op": t["op"], "index": t["index"], "sub": t["sub"], "name": name, "type": type_,
             "size": len(data) if ok else t.get("size"),
             "raw": data.hex(" ").upper() if ok else "", "value": value if ok else None, "text": text if ok else "",
             "result": result, "segmented": bool(t.get("segmented")), "ms": round((ts - t["t0"]) * 1000, 1)}
        if code is not None:
            e.update(abort_code=f"0x{code:08X}", abort_text=abort_text(code), abort_by=by)
        self.log.append(e)
        if ok:
            vkey = (t["ch"], t["node"], t["index"], t["sub"])
            self.values.pop(vkey, None)               # newest last, so the oldest go first when it is full
            while len(self.values) >= MAX_VALUES:
                self.values.pop(next(iter(self.values)))
            self.values[vkey] = {
                "ts": ts, "value": value, "text": text, "raw": e["raw"], "source": t["source"], "op": t["op"]}
