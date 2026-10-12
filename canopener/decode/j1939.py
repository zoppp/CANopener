"""J1939: 29-bit id split, PGN names, address claim (NAME), transport protocol (RTS/CTS and BAM) and DM1.

Runs on the decode thread only (DecodeManager holds its lock around every call), so no locking here.
"""

from __future__ import annotations

import collections
from typing import Callable, Optional

PGN_AC = 60928           # address claimed
PGN_TP_CM = 60416        # transport protocol, connection management
PGN_TP_DT = 60160        # transport protocol, data transfer
PGN_DM1 = 65226

PGN_NAMES = {
    0: "TSC1", 256: "TC1", 59392: "ACKM", 59904: "RQST", 60160: "TP.DT", 60416: "TP.CM", 60928: "AC",
    61184: "PROPA", 61440: "ERC1", 61441: "EBC1", 61442: "ETC1", 61443: "EEC2", 61444: "EEC1", 61445: "ETC2",
    64965: "ECUID", 65226: "DM1", 65227: "DM2", 65228: "DM3", 65242: "SOFT", 65248: "VD", 65253: "HOURS",
    65254: "TD", 65257: "LFC", 65259: "CI", 65260: "VI", 65262: "ET1", 65263: "EFL/P1", 65265: "CCVS",
    65266: "LFE", 65269: "AMB", 65270: "IC1", 65271: "VEP1", 65272: "TRF1", 65276: "DD",
}

INDUSTRY_GROUPS = {0: "global", 1: "on-highway", 2: "agricultural/forestry", 3: "construction", 4: "marine",
                   5: "industrial/process"}
TP_ABORT_REASONS = {1: "already in a session", 2: "resources needed elsewhere", 3: "timeout", 4: "CTS during transfer",
                    5: "max retransmit reached", 6: "unexpected data packet", 7: "bad sequence number",
                    8: "duplicate sequence number", 9: "message too large"}
LAMPS = ("MIL", "RSL", "AWL", "PL")       # malfunction, red stop, amber warning, protect


def split_id(can_id: int) -> tuple[int, int, int, int]:
    """29-bit id -> (priority, PGN, destination address, source address). PDU2 (PF >= 240) is broadcast: DA 0xFF."""
    prio = (can_id >> 26) & 0x7
    dp = (can_id >> 24) & 0x3                   # extended data page + data page
    pf = (can_id >> 16) & 0xFF
    ps = (can_id >> 8) & 0xFF
    if pf < 240:
        return prio, (dp << 16) | (pf << 8), ps, can_id & 0xFF
    return prio, (dp << 16) | (pf << 8) | ps, 0xFF, can_id & 0xFF


def pgn_name(pgn: int) -> str:
    return PGN_NAMES.get(pgn, "PROPB" if 65280 <= pgn <= 65535 else "")


def decode_name(data: bytes) -> dict:
    """The 64-bit NAME of an address claim."""
    n = int.from_bytes(data[:8].ljust(8, b"\x00"), "little")
    return {"name": f"{n:016X}", "identity": n & 0x1FFFFF, "manufacturer": (n >> 21) & 0x7FF,
            "ecu_instance": (n >> 32) & 0x7, "function_instance": (n >> 35) & 0x1F, "function": (n >> 40) & 0xFF,
            "vehicle_system": (n >> 49) & 0x7F, "vehicle_system_instance": (n >> 56) & 0xF,
            "industry_group": (n >> 60) & 0x7, "industry": INDUSTRY_GROUPS.get((n >> 60) & 0x7, ""),
            "arbitrary_address": bool(n >> 63)}


TP_TIMEOUT_S = 1.25        # J1939-21 T1 (750 ms) / T2 (1250 ms): longer silence ends a transport session


def decode_dm1(data: bytes) -> dict:
    """DM1: lamp status and the active DTCs (SPN, FMI, occurrence count)."""
    lamps = {}
    if data:
        for i, name in enumerate(LAMPS):
            lamps[name] = (data[0] >> (6 - 2 * i)) & 0x3
    dtcs = []
    for i in range(2, len(data) - 3, 4):
        b = data[i:i + 4]
        if b in (b"\x00\x00\x00\x00", b"\xff\xff\xff\xff"):
            continue                              # padding / "no DTC"
        dtcs.append({"spn": b[0] | (b[1] << 8) | ((b[2] >> 5) << 16), "fmi": b[2] & 0x1F, "oc": b[3] & 0x7F,
                     "cm": b[3] >> 7})
    return {"lamps": lamps, "dtcs": dtcs}


def id_text(pgn: int, da: int, sa: int) -> str:
    name = pgn_name(pgn)
    return f"PGN {pgn}{' ' + name if name else ''} SA 0x{sa:02X}" + (f" DA 0x{da:02X}" if da != 0xFF else "")


class J1939Decoder:
    """State of the J1939 decoding: ECUs from address claims, TP sessions, reassembled messages, DM1 per ECU.

    `on_message(ch, ts, can_id, sa, pgn, data)` is called with every complete J1939 message (a frame, or
    reassembled with can_id None), so the manager can decode it through a DBC."""

    def __init__(self, store, on_message: Callable[..., Optional[str]]):
        self.store = store
        self.on_message = on_message
        self.ecus: dict[tuple[int, int], dict] = {}
        self.sessions: dict[tuple[int, int, int], dict] = {}
        self.messages: collections.deque = collections.deque(maxlen=200)
        self.dm1: dict[tuple[int, int], dict] = {}

    def clear(self) -> None:
        self.ecus.clear()
        self.sessions.clear()
        self.messages.clear()
        self.dm1.clear()

    def handle(self, ch: int, ts: float, can_id: int, data: bytes) -> str:
        """Decode one extended frame; returns the receive-table text."""
        prio, pgn, da, sa = split_id(can_id)
        text = id_text(pgn, da, sa)
        if pgn == PGN_TP_CM:
            return text + " " + self._tp_cm(ch, ts, sa, da, data)
        if pgn == PGN_TP_DT:
            return text + " " + self._tp_dt(ch, ts, sa, da, data)
        if pgn == PGN_AC and len(data) >= 8:
            info = decode_name(data)
            self.ecus[(ch, sa)] = {"channel": ch, "sa": sa, "ts": ts, **info}
            return text + f" NAME {info['name']} mfr {info['manufacturer']} fn {info['function']}"
        more = self._complete(ch, ts, can_id, sa, pgn, data)
        return f"{text} {more}" if more else text

    def _complete(self, ch: int, ts: float, can_id: Optional[int], sa: int, pgn: int, data: bytes) -> str:
        if pgn == PGN_DM1:
            dm = decode_dm1(data)
            self.dm1[(ch, sa)] = {"channel": ch, "sa": sa, "ts": ts, **dm}
            self.store.publish(f"j1939/{sa}/DM1.ActiveDTCs", ts, len(dm["dtcs"]), channel=ch)
            return " ".join(f"SPN {d['spn']} FMI {d['fmi']} OC {d['oc']}"
                            + (" (CM=1: older SPN layout, SPN may be wrong)" if d["cm"] else "")
                            for d in dm["dtcs"]) or "no active DTC"
        return self.on_message(ch, ts, can_id, sa, pgn, data) or ""

    # ------------------------------------------------------------------ transport protocol
    def _tp_cm(self, ch: int, ts: float, sa: int, da: int, d: bytes) -> str:
        if len(d) < 8:
            return "TP.CM (short)"
        ctrl, pgn = d[0], d[5] | (d[6] << 8) | (d[7] << 16)
        if ctrl in (16, 32):                       # RTS, BAM: the sender opens a session
            for key in [k for k, v in self.sessions.items() if ts - v["ts"] > TP_TIMEOUT_S]:
                del self.sessions[key]              # forget sessions that never finished
            size, packets = d[1] | (d[2] << 8), d[3]
            kind = "BAM" if ctrl == 32 else "RTS/CTS"
            self.sessions[(ch, sa, da)] = {"pgn": pgn, "size": size, "packets": packets, "kind": kind, "data": {},
                                           "ts": ts}
            return f"{'BAM' if ctrl == 32 else 'RTS'} PGN {pgn} {size} bytes in {packets} packets"
        if ctrl == 17:
            return f"CTS PGN {pgn} {d[1]} packets from {d[2]}"
        if ctrl == 19:
            return f"EOMA PGN {pgn} {d[1] | (d[2] << 8)} bytes"
        if ctrl == 255:                            # either side may abort: drop both directions
            s = self.sessions.pop((ch, sa, da), None) or self.sessions.pop((ch, da, sa), None)
            reason = TP_ABORT_REASONS.get(d[1], str(d[1]))
            if s:
                self.messages.append({"ts": ts, "channel": ch, "sa": sa, "da": da, "pgn": pgn, "name": pgn_name(pgn),
                                      "kind": s["kind"], "size": s["size"], "data": "",
                                      "decoded": f"aborted: {reason}"})
            return f"Abort PGN {pgn}: {reason}"
        return f"TP.CM control {ctrl}"

    def _tp_dt(self, ch: int, ts: float, sa: int, da: int, d: bytes) -> str:
        s = self.sessions.get((ch, sa, da))
        if s is None or not d:
            return "TP.DT (no session)"
        if ts - s["ts"] > TP_TIMEOUT_S:             # T1/T2: the sender went quiet; the data is incomplete
            del self.sessions[(ch, sa, da)]
            return "TP.DT (session timed out)"
        seq = d[0]
        if not 1 <= seq <= s["packets"]:
            return f"TP.DT {seq} (outside 1..{s['packets']}, ignored)"
        s["data"][seq] = d[1:8]
        s["ts"] = ts
        if len(s["data"]) < s["packets"]:           # complete only when every sequence number is there
            return f"TP.DT {seq}/{s['packets']}"
        del self.sessions[(ch, sa, da)]
        payload = b"".join(s["data"].get(i, b"") for i in range(1, s["packets"] + 1))[: s["size"]]
        decoded = self._complete(ch, ts, None, sa, s["pgn"], payload)
        self.messages.append({"ts": ts, "channel": ch, "sa": sa, "da": da, "pgn": s["pgn"], "name": pgn_name(s["pgn"]),
                              "kind": s["kind"], "size": len(payload), "data": payload.hex(" ").upper(),
                              "decoded": decoded})
        return f"TP.DT {seq}/{s['packets']} complete: PGN {s['pgn']} {len(payload)} bytes"

    # ------------------------------------------------------------------ readers
    def ecu_list(self) -> list[dict]:
        return sorted(self.ecus.values(), key=lambda e: (e["channel"], e["sa"]))

    def dm1_list(self) -> list[dict]:
        return sorted(self.dm1.values(), key=lambda e: (e["channel"], e["sa"]))
