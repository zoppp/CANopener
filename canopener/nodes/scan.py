"""Bus scan: passive listen + active SDO ping + identity/name fetch.

Strategy:
  1. Passive listen for `passive_seconds`, recording every raw arbitration ID
     seen (for conflict checking against non-CANopen traffic too) plus the
     node set implied by heartbeat/boot-up (0x700..0x77F). TxPDO1..4
     (0x180/0x280/0x380/0x480 + node id) traffic is only a *hint*: it does
     not by itself list a node, because devices with free-form ids can
     land in this range too and would
     otherwise show up as phantom nodes. A hinted id is promoted to a real
     node only if it also answers the active SDO ping or sends a heartbeat;
     otherwise it's reported separately as `pdo_only_ids` - traffic without
     a CANopen node behind it.
  2. Active SDO read of 0x1000 (device type) on every node id 1..127 not
     already found via heartbeat, with a short timeout. At the default 30ms
     timeout this phase is ~127 * 30ms =~ 3.8s worst case: the canopen SDO
     client's MAX_RETRIES=1 setting is, despite the name, exactly one
     send+wait per id (its retry-countdown reaches 0 and raises on the
     first timeout, it never sends a second time) - confirmed against the
     library source and by timing a 127-id ping on the virtual interface.
  3. For every node found by either method: read identity (0x1018:1..4) and
     device name (0x1008, expedited or segmented - handled transparently by
     the canopen SDO client). Ids in `pdo_only_ids` are skipped here.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import can
import canopen
from canopen.sdo import SdoAbortedError, SdoCommunicationError

HEARTBEAT_BASE = 0x700
TXPDO_BASES = (0x180, 0x280, 0x380, 0x480)

#: Timeout used for the identity/name reads (these nodes are known to be
#: present, so we can afford to wait longer than the initial ping).
IDENTITY_TIMEOUT = 0.3

_NOOP = lambda *a, **k: None  # noqa: E731


@dataclass
class NodeInfo:
    node_id: int
    seen_by: set = field(default_factory=set)  # subset of {"heartbeat", "pdo", "sdo"}
    nmt_state: Optional[int] = None
    vendor_id: Optional[int] = None
    product_code: Optional[int] = None
    revision_number: Optional[int] = None
    serial_number: Optional[int] = None
    device_name: Optional[str] = None

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["seen_by"] = sorted(self.seen_by)
        for key in ("vendor_id", "product_code", "revision_number", "serial_number"):
            if d[key] is not None:
                d[key] = f"0x{d[key]:08X}"
        return d


@dataclass
class ScanResult:
    nodes: dict
    raw_ids: set
    raw_ext_ids: set
    pdo_only_ids: set
    duration_s: float

    def as_dict(self) -> dict:
        return {
            "duration_s": self.duration_s,
            "raw_ids": [f"0x{i:03X}" for i in sorted(self.raw_ids)],
            "raw_ext_ids": [f"0x{i:08X}" for i in sorted(self.raw_ext_ids)],
            "pdo_only_ids": sorted(self.pdo_only_ids),
            "nodes": [n.as_dict() for n in sorted(self.nodes.values(), key=lambda n: n.node_id)],
        }


class _RawCapture(can.Listener):
    """Records every raw ID seen, plus heartbeat/boot-up node evidence and
    PDO "hints" (which don't by themselves confirm a node - see module
    docstring)."""

    def __init__(self):
        self.raw_ids: set[int] = set()
        self.raw_ext_ids: set[int] = set()
        self.nodes: dict[int, NodeInfo] = {}
        self.pdo_hint_ids: set[int] = set()

    def _node(self, node_id: int) -> NodeInfo:
        return self.nodes.setdefault(node_id, NodeInfo(node_id=node_id))

    def on_message_received(self, msg: can.Message) -> None:
        # RTR frames (e.g. NMT node-guarding, 0x700+n with no payload) carry
        # no real data even when msg.dlc says otherwise - skip them entirely
        # rather than risk reading a byte that isn't there.
        if msg.is_error_frame or msg.is_remote_frame:
            return

        cob = msg.arbitration_id
        if msg.is_extended_id:
            # A 29-bit id is a different address space - it is never
            # confused with the 11-bit CiA 301 predefined connection set,
            # so it's tracked separately rather than mixed into raw_ids.
            self.raw_ext_ids.add(cob)
            return

        self.raw_ids.add(cob)

        if HEARTBEAT_BASE <= cob <= HEARTBEAT_BASE + 0x7F:
            node_id = cob - HEARTBEAT_BASE
            if 1 <= node_id <= 127:
                info = self._node(node_id)
                info.seen_by.add("heartbeat")
                if len(msg.data) >= 1:
                    info.nmt_state = msg.data[0] & 0x7F
            return

        for base in TXPDO_BASES:
            if base <= cob <= base + 0x7F:
                node_id = cob - base
                if 1 <= node_id <= 127:
                    # Hint only - promoted to a real node in scan() if it
                    # also answers the SDO ping or heartbeats.
                    self.pdo_hint_ids.add(node_id)
                    if node_id in self.nodes:
                        self.nodes[node_id].seen_by.add("pdo")
                return


def _fill_identity(node: canopen.RemoteNode, info: NodeInfo, log: Callable) -> None:
    for sub, attr in ((1, "vendor_id"), (2, "product_code"), (3, "revision_number"), (4, "serial_number")):
        try:
            data = node.sdo.upload(0x1018, sub)
        except (SdoAbortedError, SdoCommunicationError) as exc:
            log(f"  node {info.node_id}: 0x1018:{sub:02X} failed ({exc})")
            continue
        if len(data) >= 4:
            setattr(info, attr, struct.unpack_from("<I", data)[0])
        elif data:
            setattr(info, attr, int.from_bytes(data, "little"))

    try:
        data = node.sdo.upload(0x1008, 0)
    except (SdoAbortedError, SdoCommunicationError) as exc:
        log(f"  node {info.node_id}: 0x1008 (device name) failed ({exc})")
        return
    info.device_name = data.decode("ascii", errors="replace").rstrip("\x00")


def scan(
    network: canopen.Network,
    passive_seconds: float = 2.5,
    sdo_timeout: float = 0.03,
    log: Callable = _NOOP,
    active: bool = True,
) -> ScanResult:
    """Scan the bus already connected via `network`. active=False only listens (no SDO pings, no identity
    reads): for a listen-only adapter."""
    capture = _RawCapture()
    notifier = network.notifier
    notifier.add_listener(capture)
    try:
        log(f"Passive listen for {passive_seconds}s ...")
        time.sleep(passive_seconds)
        network.check()  # surface a dead notifier now instead of an empty-looking bus
        log(f"  {len(capture.nodes)} node(s) implied by heartbeat/boot-up traffic, "
            f"{len(capture.pdo_hint_ids)} node id(s) hinted by PDO traffic, "
            f"{len(capture.raw_ids)} distinct raw ID(s) seen")

        remaining = [n for n in range(1, 128) if n not in capture.nodes] if active else []
        log(f"Active SDO ping (0x1000) on {len(remaining)} remaining id(s), timeout={sdo_timeout}s ...")
        for node_id in remaining:
            node = network.add_node(node_id, None)
            try:
                node.sdo.RESPONSE_TIMEOUT = sdo_timeout
                try:
                    node.sdo.upload(0x1000, 0)
                    found = True
                    log(f"  node {node_id}: responded to SDO 0x1000")
                except SdoAbortedError:
                    # It answered (with an abort), so it's present.
                    found = True
                    log(f"  node {node_id}: SDO abort on 0x1000 (present, but errored)")
                except SdoCommunicationError:
                    found = False
                if found:
                    info = capture._node(node_id)
                    info.seen_by.add("sdo")
                    if node_id in capture.pdo_hint_ids:
                        info.seen_by.add("pdo")
            finally:
                del network[node_id]
        network.check()

        pdo_only_ids = capture.pdo_hint_ids - set(capture.nodes.keys())
        if pdo_only_ids:
            log(f"  {len(pdo_only_ids)} id(s) had PDO-range traffic but no CANopen node behind them "
                f"(free-form traffic): {sorted(pdo_only_ids)}")

        log(f"Reading identity + device name for {len(capture.nodes)} node(s) found ..." if active
            else "Listen-only: identity not read")
        for node_id, info in (list(capture.nodes.items()) if active else []):
            node = network.add_node(node_id, None)
            try:
                node.sdo.RESPONSE_TIMEOUT = IDENTITY_TIMEOUT
                _fill_identity(node, info, log)
                log(f"  node {node_id}: vendor=0x{info.vendor_id or 0:X} name={info.device_name!r}")
            finally:
                del network[node_id]
        network.check()
    finally:
        notifier.remove_listener(capture)

    return ScanResult(
        nodes=capture.nodes, raw_ids=capture.raw_ids, raw_ext_ids=capture.raw_ext_ids,
        pdo_only_ids=pdo_only_ids, duration_s=passive_seconds,
    )
