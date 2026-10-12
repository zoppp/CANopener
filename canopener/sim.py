"""Simulated devices on a virtual bus, for trying CANopener without hardware (and for the test suite).

SimBus starts on a python-can `virtual` channel (in this process only) and adds:
- CANopen nodes that answer SDO (identity, name, the vendor node-id objects of the bundled recipes), send
  heartbeats and TPDOs, and really move to their new node id after the recipe's store + NMT reset;
- a CiA 305 LSS slave per LSS-capable node (switch global/selective, configure node id / bit timing, store);
- some non-CANopen traffic: a fast 11-bit frame and J1939 frames (two ECUs claiming addresses, a DM1 by single
  frame and one by BAM transport protocol), and now and then two CANopen emergency messages.
"""

from __future__ import annotations

import logging
import math
import struct
import threading
import time
from typing import Optional

import can
import canopen

log = logging.getLogger(__name__)

# (node id, name, vendor id, product, node-id object (index, sub) or None for LSS, store needed, lss)
# Made-up demo devices; recipes.json has recipes for A and B (guarded by these product codes).
DEFAULT_NODES = (
    (5, "Demo device A (SDO)", 0x0CA0, 0x53494D01, (0x2100, 0), True, False),
    (10, "Demo device B (SDO, power cycle)", 0x0CA1, 0x53494D02, (0x2110, 1), True, False),
    (20, "Demo LSS device", 0x0CA2, 0x53494D03, None, False, True),
)


def _j1939_name(identity: int, function: int) -> bytes:
    """The NAME of a J1939 address claim (arbitrary address capable)."""
    return ((1 << 63) | (function << 40) | identity).to_bytes(8, "little")


class SimNode:
    def __init__(self, sim: "SimBus", node_id: int, name: str, vendor: int, product: int,
                 id_obj: Optional[tuple], needs_store: bool, lss: bool, serial: int):
        self.sim, self.name, self.vendor, self.product = sim, name, vendor, product
        self.id_obj, self.needs_store, self.lss, self.serial = id_obj, needs_store, lss, serial
        self.revision = 0x00010000
        self.node_id = node_id
        self.pending_id: Optional[int] = None
        self.stored = False
        self.writes: list = []
        self.local: Optional[canopen.LocalNode] = None
        self._make_local()

    def _make_local(self) -> None:
        od = canopen.ObjectDictionary()
        for idx, sub, nm in ((0x1000, 0, "Device type"), (0x1008, 0, "Device name")):
            v = canopen.objectdictionary.ODVariable(nm, idx, sub)
            v.access_type = "ro"
            od.add_object(v)
        rec = canopen.objectdictionary.ODRecord("Identity", 0x1018)
        for sub in (1, 2, 3, 4):
            v = canopen.objectdictionary.ODVariable(f"id{sub}", 0x1018, sub)
            v.access_type = "ro"
            rec.add_member(v)
        od.add_object(rec)
        st = canopen.objectdictionary.ODRecord("Store", 0x1010)
        for sub in (1, 2):
            v = canopen.objectdictionary.ODVariable(f"store{sub}", 0x1010, sub)
            v.access_type = "rw"
            st.add_member(v)
        od.add_object(st)
        # the same small manufacturer area on every demo node: node id 0x2100 (A), bit rate 0x2101 (A), 0x2110:01 (B)
        for idx, name in ((0x2100, "Node id (demo A)"), (0x2101, "Bit rate kbit/s (demo A)")):
            v = canopen.objectdictionary.ODVariable(name, idx, 0)
            v.access_type = "rw"
            od.add_object(v)
        r = canopen.objectdictionary.ODRecord("Config (demo B)", 0x2110)
        v = canopen.objectdictionary.ODVariable("Node id (demo B)", 0x2110, 1)
        v.access_type = "rw"
        r.add_member(v)
        od.add_object(r)
        local = self.sim.network.create_node(self.node_id, od)
        local.data_store[0x1000] = {0: struct.pack("<I", 0x191)}
        local.data_store[0x1008] = {0: self.name.encode()}
        local.data_store[0x1018] = {1: struct.pack("<I", self.vendor), 2: struct.pack("<I", self.product),
                                    3: struct.pack("<I", self.revision), 4: struct.pack("<I", self.serial)}
        # the demo objects read back like on a real device (node id, bit rate in kbit/s)
        local.data_store[0x2100] = {0: bytes([self.node_id])}
        local.data_store[0x2101] = {0: struct.pack("<H", 250)}
        local.data_store[0x2110] = {1: bytes([self.node_id])}
        local.add_write_callback(self._on_write)
        self.local = local

    def _on_write(self, index, subindex, od, data):
        self.writes.append((index, subindex, bytes(data)))
        if self.id_obj and (index, subindex) == self.id_obj and data:
            self.pending_id = data[0]
        if index == 0x1010 and bytes(data) == b"save":
            self.stored = True

    def reset(self) -> None:
        """NMT reset: apply a pending node id (if it was stored where the recipe needs a store)."""
        new = self.pending_id
        if new and (self.stored or not self.needs_store) and 1 <= new <= 127 and new != self.node_id:
            log.info("sim: node %d -> %d", self.node_id, new)
            del self.sim.network[self.node_id]
            self.node_id = new
            self._make_local()
        self.pending_id, self.stored = None, False
        self.sim.send(0x700 + self.node_id, b"\x00")          # boot-up


class SimBus(can.Listener):
    def __init__(self, channel: str = "canopener", nodes=DEFAULT_NODES, traffic: bool = True):
        self.channel = channel
        self.network = canopen.Network()
        self.network.connect(interface="virtual", channel=channel, receive_own_messages=False)
        self.network.notifier.add_listener(self)
        self.nodes = [SimNode(self, nid, name, ven, prod, obj, store, lss, 0x1000 + i)
                      for i, (nid, name, ven, prod, obj, store, lss) in enumerate(nodes)]
        self._lss_state = {id(n): "waiting" for n in self.nodes}
        self._lss_sel: dict = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, args=(traffic,), name="sim", daemon=True)
        self._thread.start()

    def send(self, can_id: int, data: bytes, ext: bool = False) -> None:
        try:
            self.network.bus.send(can.Message(arbitration_id=can_id, data=data, is_extended_id=ext))
        except (can.CanError, AttributeError):
            pass

    # traffic -----------------------------------------------------------------------------------
    def _run(self, traffic: bool) -> None:
        t0 = time.monotonic()
        n = 0
        while not self._stop.wait(0.01):
            n += 1
            now = time.monotonic() - t0
            if traffic:
                v = int(1000 + 800 * math.sin(now))
                self.send(0x0CF, struct.pack("<HH", v & 0xFFFF, n & 0xFFFF))          # 100 Hz free-form frame
                if n % 10 == 0:
                    self.send(0x18FEF100, bytes([0xFF, n & 0xFF, 0x20, 0, 0, 0, 0, 0xFF]), ext=True)  # J1939 CCVS-like
                if n % 50 == 0:
                    self.send(0x18F00400, bytes([0xF0, 0x7D, 0x7D, (n >> 2) & 0xFF, 0x0C, 0, 0xF0, 0x7D]), ext=True)
                if n % 100 == 50:                   # J1939 DM1 of SA 0x00: amber lamp, SPN 110 FMI 0 OC 3
                    self.send(0x18FECA00, bytes.fromhex("04FF6E000003FFFF"), ext=True)
                if n % 500 == 1:                    # address claims (normally at power-up; repeated for late viewers)
                    self.send(0x18EEFF00, _j1939_name(0x1234, 0), ext=True)
                    self.send(0x18EEFF28, _j1939_name(0x0042, 60), ext=True)
                if n % 3000 == 250:                 # every 30 s a two-packet DM1 of SA 0x28 by BAM (transport protocol)
                    self.send(0x1CECFF28, bytes.fromhex("200A0002FFCAFE00"), ext=True)
                    self.send(0x1CEBFF28, bytes.fromhex("0104FF64000101BE"), ext=True)
                    self.send(0x1CEBFF28, bytes.fromhex("02000402FFFFFFFF"), ext=True)
                if n % 6000 == 300:                 # every minute CANopen emergencies: node 5 voltage, node 10 heartbeat
                    self.send(0x080 + self.nodes[0].node_id, bytes.fromhex("1032050000000000"))
                    self.send(0x080 + self.nodes[1].node_id, bytes.fromhex("3081110000000000"))
            for node in self.nodes:
                if n % 10 == 0:
                    self.send(0x180 + node.node_id, struct.pack("<hH", int(500 * math.cos(now)), n & 0xFFFF))
                if n % 50 == 0:
                    self.send(0x700 + node.node_id, b"\x05")

    # NMT + LSS slave ---------------------------------------------------------------------------
    def on_message_received(self, msg: can.Message) -> None:
        if msg.is_extended_id or msg.is_remote_frame:
            return
        d = bytes(msg.data)
        if msg.arbitration_id == 0x000 and len(d) >= 2 and d[0] in (0x81, 0x82):
            for node in self.nodes:
                if d[1] in (0, node.node_id):
                    node.reset()
        elif msg.arbitration_id == 0x7E5 and d:
            self._lss(d)

    def _lss(self, d: bytes) -> None:
        cs = d[0]
        lss_nodes = [n for n in self.nodes if n.lss]
        if cs == 0x04:
            for n in lss_nodes:
                self._lss_state[id(n)] = "configuration" if d[1] == 1 else "waiting"
            return
        if 0x40 <= cs <= 0x43:
            val = struct.unpack_from("<I", d, 1)[0]
            for n in lss_nodes:
                want = (n.vendor, n.product, n.revision, n.serial)[cs - 0x40]
                sel = self._lss_sel.setdefault(id(n), [])
                if cs == 0x40:
                    sel.clear()
                sel.append(val == want)
                if cs == 0x43 and len(sel) == 4 and all(sel):
                    self._lss_state[id(n)] = "configuration"
                    self.send(0x7E4, bytes([0x44]) + bytes(7))
            return
        configuring = [n for n in lss_nodes if self._lss_state[id(n)] == "configuration"]
        for n in configuring:
            if cs == 0x11:
                n.pending_id = d[1]
                self.send(0x7E4, bytes([0x11, 0]) + bytes(6))
            elif cs == 0x13:
                self.send(0x7E4, bytes([0x13, 0]) + bytes(6))
            elif cs == 0x17:
                n.stored = True
                self.send(0x7E4, bytes([0x17, 0]) + bytes(6))
            elif cs == 0x5E:
                self.send(0x7E4, bytes([0x5E, n.node_id]) + bytes(6))

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(2)
        try:
            self.network.disconnect()
        except Exception:
            pass
