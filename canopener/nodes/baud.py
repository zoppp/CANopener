"""Bit-rate change: LSS (configure, store, activate; CiA 305) for generic devices, or a vendor SDO recipe
(a recipe's `"baud_sdo"` field) for devices without LSS. Either way the adapter is then
reopened at the new rate (the `reopen` callback) and traffic is checked (`traffic_seen`).

CiA 305 bit timing table (table selector 0):
  0=1M 1=800k 2=500k 3=250k 4=125k 5=100k 6=50k 7=20k 8=10k
"""

from __future__ import annotations

from typing import Callable, Optional

import canopen

from . import recipes as recipes_module
from . import sequence
from .scan import NodeInfo
from .sequence import BITRATE_TABLE  # noqa: F401  (re-exported)

_NOOP = lambda *a, **k: None  # noqa: E731


class BaudChangeError(Exception):
    pass


def _run(network, steps, vars, log, reopen, traffic_seen) -> sequence.RunResult:
    ctx = sequence.Context(network=network, vars=vars, send_raw=lambda i, d, *a: network.send_message(i, d),
                           reopen=reopen, traffic_seen=traffic_seen, log=log)
    try:
        result = sequence.execute(steps, ctx)
    except sequence.SequenceError as exc:
        raise BaudChangeError(str(exc)) from exc
    if not result.ok:
        raise BaudChangeError(f"bit-rate sequence failed - {result.error}")
    return result


def _verified(result: sequence.RunResult) -> bool:
    wait = result.value_of("wait_traffic")
    return wait is not None and wait.status == "ok"


def plan_lss(new_kbit: int, node_info: Optional[NodeInfo] = None, switch_delay_ms: int = 500,
             verify_timeout: float = 5.0, reopen: bool = True) -> tuple[list, dict]:
    if new_kbit not in BITRATE_TABLE:
        raise BaudChangeError(f"unsupported bit rate {new_kbit} kbit/s; choose one of {sorted(BITRATE_TABLE)}")
    if node_info is not None and (not node_info.vendor_id or not node_info.serial_number):
        raise BaudChangeError(
            f"node {node_info.node_id}: LSS selective addressing needs a nonzero vendor id and "
            f"serial number from 0x1018 (got vendor=0x{node_info.vendor_id or 0:X}, "
            f"serial=0x{node_info.serial_number or 0:X})")
    steps = recipes_module.baud_lss_steps(node_info is not None, switch_delay_ms, verify_timeout, reopen)
    vars = {"BAUD": new_kbit, "BAUD_INDEX": BITRATE_TABLE[new_kbit], **recipes_module.identity_vars(node_info)}
    if node_info is not None:
        vars["OLD_ID"] = vars["NODE"] = node_info.node_id
    return steps, vars


def plan_sdo(old_id: int, spec: dict, new_kbit: int, verify_timeout: float = 5.0) -> tuple[list, dict, str]:
    try:
        steps, effect = recipes_module.baud_sdo_steps(spec, new_kbit, verify_timeout)
    except ValueError as exc:
        raise BaudChangeError(str(exc)) from exc
    return steps, {"OLD_ID": old_id, "NODE": old_id, "BAUD": new_kbit}, effect


def change_bitrate(
    network: canopen.Network,
    new_kbit: int,
    node_info: Optional[NodeInfo] = None,
    switch_delay_ms: int = 500,
    verify_timeout: float = 5.0,
    log: Callable = _NOOP,
    reopen: Optional[Callable[[int], None]] = None,
    traffic_seen: Optional[Callable[[float], bool]] = None,
) -> bool:
    """Change bit rate via LSS for one node (`node_info` given) or every LSS node (global switch).

    The device only keeps the new rate across a power cycle because of the LSS store (0x17). Returns True if
    traffic was seen at the new rate after `reopen(new_kbit)`. Without `reopen` the adapter is not touched and
    the result is False (nothing verified). Raises BaudChangeError when a step gets no reply."""
    steps, vars = plan_lss(new_kbit, node_info, switch_delay_ms, verify_timeout, reopen=reopen is not None)
    result = _run(network, steps, vars, log, reopen, traffic_seen)
    return _verified(result)


def change_bitrate_sdo(
    network: canopen.Network,
    old_id: int,
    spec: dict,
    new_kbit: int,
    verify_timeout: float = 5.0,
    log: Callable = _NOOP,
    reopen: Optional[Callable[[int], None]] = None,
    traffic_seen: Optional[Callable[[float], bool]] = None,
) -> Optional[bool]:
    """Change one node's bit rate with its vendor SDO recipe (`baud_sdo` in recipes.json).

    `spec["unit"]`: "enum" (`spec["enum"]` maps kbit/s to the raw value), "bps" (raw value in bit/s) or
    "kbit/s" (raw value is new_kbit, bounded by spec["min"]/["max"]).

    Returns None if `spec["effect"]` is "power_cycle" (stored, not live yet - nothing to verify), otherwise
    True/False from the traffic check after reopening."""
    steps, vars, effect = plan_sdo(old_id, spec, new_kbit, verify_timeout)
    if reopen is None:
        steps = [s for s in steps if s["op"] not in ("reopen", "wait_traffic")]
    result = _run(network, steps, vars, log, reopen, traffic_seen)
    if effect == "power_cycle":
        log("Recipe stored; power-cycle the device to apply the new bit rate.")
        return None
    return _verified(result)
