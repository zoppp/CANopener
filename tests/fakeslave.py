"""A minimal CANopen slave on a python-can `virtual` channel, used by the
test suite so tests exercise real SDO wire traffic (expedited and segmented
upload) instead of mocking the canopen library.

Answers SDO reads of 0x1000 (device type), 0x1008 (device name - long enough
to force a segmented upload) and 0x1018:1..4 (identity), accepts SDO writes to
0x1010:01/02 (store), 0x2100:00, 0x2101:00 and 0x2110:01 (the simulator demo
devices' recipe objects) plus any `extra_objects`, recording every write for
assertions, and sends heartbeats. By default it identifies as the simulator's
demo device A (vendor 0x0CA0, product 0x53494D01), which has a recipe.
"""

from __future__ import annotations

import struct

import canopen

DEMO_VENDOR_ID = 0x0CA0
DEMO_PRODUCT_A = 0x53494D01


def _add(od: canopen.ObjectDictionary, index: int, subs: tuple, access: str = "rw") -> None:
    """A variable (subs == (0,)) or a record with the given subindices."""
    if subs == (0,):
        v = canopen.objectdictionary.ODVariable(f"Object 0x{index:04X}", index, 0)
        v.access_type = access
        od.add_object(v)
        return
    rec = canopen.objectdictionary.ODRecord(f"Object 0x{index:04X}", index)
    for sub in subs:
        var = canopen.objectdictionary.ODVariable(f"Object 0x{index:04X} sub {sub}", index, sub)
        var.access_type = access
        rec.add_member(var)
    od.add_object(rec)


def _build_od(extra_objects=()) -> canopen.ObjectDictionary:
    od = canopen.ObjectDictionary()
    _add(od, 0x1000, (0,), "ro")
    _add(od, 0x1008, (0,), "ro")
    _add(od, 0x1018, (1, 2, 3, 4), "ro")
    _add(od, 0x1010, (1, 2))
    _add(od, 0x2100, (0,))
    _add(od, 0x2101, (0,))
    _add(od, 0x2110, (1,))
    for index, subs in extra_objects:
        _add(od, index, tuple(subs))
    return od


class FakeSlave:
    def __init__(
        self,
        channel: str,
        node_id: int,
        *,
        device_name: str = "Demo device A (test)",
        vendor_id: int = DEMO_VENDOR_ID,
        product_code: int = DEMO_PRODUCT_A,
        revision: int = 0x00010000,
        serial: int = 0x00000042,
        heartbeat_ms: float = 100.0,
        extra_objects: tuple = (),
    ):
        self.channel = channel
        self.node_id = node_id
        self.vendor_id = vendor_id
        self.product_code = product_code
        self.revision = revision
        self.serial = serial
        self.writes: list[tuple[int, int, bytes]] = []

        self.network = canopen.Network()
        self.network.connect(interface="virtual", channel=channel, bitrate=250_000)

        od = _build_od(extra_objects)
        self.local = self.network.create_node(node_id, od)
        self.local.data_store[0x1000] = {0: struct.pack("<I", 0x00000191)}
        self.local.data_store[0x1008] = {0: device_name.encode("ascii")}
        self.local.data_store[0x1018] = {
            1: struct.pack("<I", vendor_id),
            2: struct.pack("<I", product_code),
            3: struct.pack("<I", revision),
            4: struct.pack("<I", serial),
        }
        self.local.add_write_callback(self._on_write)

        self._heartbeat_task = None
        if heartbeat_ms:
            self._heartbeat_task = self.network.send_periodic(
                0x700 + node_id, [0x05], heartbeat_ms / 1000.0
            )

    def _on_write(self, index, subindex, od, data):
        self.writes.append((index, subindex, bytes(data)))

    def close(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat_task.stop()
        self.network.disconnect()
