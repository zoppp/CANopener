"""Conflict check, then change a node id: the recipe is compiled into sequence steps (see sequence.py)
so the node workspace can preview exactly the frames that set_node_id() sends."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import canopen

from . import recipes as recipes_module
from . import sequence
from .scan import ScanResult

#: Predefined connection set base COB-IDs (CiA 301): each covers base+n for
#: node id n. Checked against every raw ID seen on the bus (not just other
#: CANopen nodes) because some devices (e.g. motor controllers) use free-form
#: IDs that still land in this space.
CONFLICT_COB_BASES = (
    0x080, 0x180, 0x200, 0x280, 0x300, 0x380,
    0x400, 0x480, 0x500, 0x580, 0x600, 0x700,
)

#: NMT command specifiers used below (CiA 301 table 3).
NMT_RESET_NODE = 0x81  # "reset node" - used after a recipe's SDO steps + store
NMT_RESET_COMMUNICATION = 0x82  # "reset communication" - used after the LSS sequence

_NOOP = lambda *a, **k: None  # noqa: E731


class SetIdError(Exception):
    """Raised when the node ID change cannot be carried out."""


@dataclass
class ConflictReport:
    new_id: int
    conflicting_ids: set = field(default_factory=set)
    conflicting_nodes: set = field(default_factory=set)

    @property
    def has_conflict(self) -> bool:
        return bool(self.conflicting_ids) or bool(self.conflicting_nodes)


class ConflictError(SetIdError):
    def __init__(self, conflict: ConflictReport):
        self.conflict = conflict
        ids = ", ".join(f"0x{i:03X}" for i in sorted(conflict.conflicting_ids))
        nodes = ", ".join(str(n) for n in sorted(conflict.conflicting_nodes))
        parts = []
        if ids:
            parts.append(f"COB-ID(s) already on the bus: {ids}")
        if nodes:
            parts.append(f"node id already in use by: {nodes}")
        super().__init__(f"node id {conflict.new_id} conflicts - " + "; ".join(parts))


@dataclass
class SetResult:
    old_id: int
    new_id: int
    method: str
    power_cycle_required: bool
    verified: bool
    message: str


def check_conflict(scan_result: Optional[ScanResult], old_id: int, new_id: int) -> ConflictReport:
    if scan_result is None:
        return ConflictReport(new_id=new_id)

    new_cobs = {base + new_id for base in CONFLICT_COB_BASES}
    old_cobs = {base + old_id for base in CONFLICT_COB_BASES}
    # The old node's own predefined IDs don't count against it - they are
    # about to move (or the id being tested may equal the node's current id).
    other_raw_ids = scan_result.raw_ids - old_cobs
    conflicting_ids = new_cobs & other_raw_ids

    conflicting_nodes = {
        n for n in scan_result.nodes
        if n == new_id and n != old_id
    }

    return ConflictReport(new_id=new_id, conflicting_ids=conflicting_ids, conflicting_nodes=conflicting_nodes)


def _check_node_id_range(recipe: dict, old_id: int, new_id: int) -> None:
    node_range = recipe.get("node_id_range")
    if not node_range:
        return
    lo, hi = node_range
    if not (lo <= new_id <= hi):
        raise SetIdError(
            f"node {old_id}: new id {new_id} is outside this device's supported range {lo}..{hi}"
        )


@dataclass
class SetPlan:
    old_id: int
    new_id: int
    recipe: dict
    steps: list
    vars: dict
    effect: str
    conflict: ConflictReport

    @property
    def method(self) -> str:
        return self.recipe.get("method", "?")


def plan_set_id(
    scan_result: Optional[ScanResult],
    old_id: int,
    new_id: int,
    recipes: dict,
    method_override: Optional[str] = None,
    verify_timeout: float = 5.0,
) -> SetPlan:
    """Pick the recipe and compile it into steps. Raises SetIdError when it cannot be done."""
    if not (1 <= new_id <= 127):
        raise SetIdError(f"new node id {new_id} is out of range 1..127")
    if not (1 <= old_id <= 127):
        raise SetIdError(f"old node id {old_id} is out of range 1..127")
    if old_id == new_id:
        raise SetIdError("the old and new node id are the same")

    conflict = check_conflict(scan_result, old_id, new_id)
    info = scan_result.nodes.get(old_id) if scan_result else None
    vendor_id = info.vendor_id if info else None
    try:
        recipe = recipes_module.resolve_recipe(recipes, vendor_id, method_override,
                                               info.product_code if info else None)
    except (KeyError, ValueError) as exc:
        raise SetIdError(str(exc)) from exc

    if recipe.get("method") == "readonly":
        note = recipe.get("note", "device cannot be re-addressed by this tool")
        raise SetIdError(f"node {old_id}: recipe is read-only - {note}")
    _check_node_id_range(recipe, old_id, new_id)

    if recipe.get("method") == "lss":
        if info is None:
            raise SetIdError(f"node {old_id}: no scan data for LSS identity; scan first")
        # CiA 305 allows product code and revision 0; only vendor id and serial number must be nonzero.
        if not info.vendor_id or not info.serial_number:
            raise SetIdError(
                f"node {old_id}: LSS selective addressing needs a nonzero vendor id and serial number "
                f"from 0x1018 (got vendor=0x{info.vendor_id or 0:X}, serial=0x{info.serial_number or 0:X}); "
                f"scan first"
            )
    try:
        steps, effect = recipes_module.set_id_steps(recipe, verify_timeout)
    except ValueError as exc:
        raise SetIdError(str(exc)) from exc
    vars = {"OLD_ID": old_id, "NEW_ID": new_id, "NODE": old_id, **recipes_module.identity_vars(info)}
    try:
        sequence.preview(steps, vars)
    except sequence.SequenceError as exc:
        raise SetIdError(f"node {old_id}: {exc}") from exc
    return SetPlan(old_id, new_id, recipe, steps, vars, effect, conflict)


def run_plan(network: canopen.Network, plan: SetPlan, log: Callable = _NOOP, send_raw=None,
             stop: Optional[Callable[[], bool]] = None) -> SetResult:
    def raw(can_id, data, ext=False, fd=False, brs=False):
        network.send_message(can_id, data)

    log(f"Recipe: {plan.recipe.get('name', plan.method)} (method={plan.method})")
    ctx = sequence.Context(network=network, vars=plan.vars, send_raw=send_raw or raw, log=log, stop=stop)
    result = sequence.execute(plan.steps, ctx)
    if not result.ok:
        raise SetIdError(f"node {plan.old_id}: {result.error}")

    wait = result.value_of("wait_node")
    if plan.effect == "power_cycle":
        return SetResult(plan.old_id, plan.new_id, plan.method, True, False,
                         "recipe stored; power-cycle the device to complete the change")
    if wait is None:
        msg = "change sent (verification skipped for this recipe)"
        verified = False
    elif wait.status == "ok":
        verified, msg = True, f"verified node {plan.new_id} is alive"
    else:
        verified = False
        msg = f"could not verify node {plan.new_id} came up ({wait.detail}); check manually"
    return SetResult(plan.old_id, plan.new_id, plan.method, False, verified, msg)


def set_node_id(
    network: canopen.Network,
    scan_result: Optional[ScanResult],
    old_id: int,
    new_id: int,
    recipes: dict,
    method_override: Optional[str] = None,
    confirm_conflict: bool = False,
    verify_timeout: float = 5.0,
    log: Callable = _NOOP,
) -> "tuple[SetResult, ConflictReport]":
    plan = plan_set_id(scan_result, old_id, new_id, recipes, method_override, verify_timeout)
    if plan.conflict.has_conflict and not confirm_conflict:
        raise ConflictError(plan.conflict)
    return run_plan(network, plan, log), plan.conflict
