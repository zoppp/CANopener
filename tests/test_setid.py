import struct

import can
from fakeslave import FakeSlave

from canopener.nodes import recipes as recipes_module
from canopener.nodes import scan as scan_mod
from canopener.nodes import setid

SDO_TIMEOUT = 0.01


def test_set_with_sdo_recipe_writes_the_recipe_frames(channel, network):
    slave = FakeSlave(channel, node_id=1, heartbeat_ms=100)          # simulator demo device A (has a recipe)
    try:
        scan_result = scan_mod.scan(network, passive_seconds=0.4, sdo_timeout=SDO_TIMEOUT)
        recipes = recipes_module.load_recipes()

        result, _conflict = setid.set_node_id(
            network, scan_result, old_id=1, new_id=72, recipes=recipes, verify_timeout=0.2,
        )

        assert result.method == "sdo"
        assert result.power_cycle_required is False                   # effect nmt_reset
        assert slave.writes == [
            (0x2100, 0, struct.pack("<B", 72)),
            (0x1010, 1, struct.pack("<I", 0x65766173)),
        ]
    finally:
        slave.close()


def test_recipe_chosen_by_vendor_and_product_code(channel, network):
    slave = FakeSlave(channel, node_id=3)
    other = FakeSlave(channel, node_id=4, product_code=0x1234)          # same vendor, another product
    try:
        scan_result = scan_mod.scan(network, passive_seconds=0.3, sdo_timeout=SDO_TIMEOUT)
        recipes = recipes_module.load_recipes()
        info = scan_result.nodes[3]
        recipe = recipes_module.resolve_recipe(recipes, info.vendor_id, product_code=info.product_code)
        assert recipe["method"] == "sdo" and recipe["name"] == "sim-demo-a"
        info = scan_result.nodes[4]
        recipe = recipes_module.resolve_recipe(recipes, info.vendor_id, product_code=info.product_code)
        assert recipe["name"] == "generic-lss"                         # the product guard keeps it off
    finally:
        slave.close()
        other.close()


def test_conflict_detected_against_raw_non_node_traffic(channel, network):
    """A free-form frame (as some motor controllers send) at a predefined COB-ID
    for the target node must be flagged even though it's not a scanned node."""
    slave = FakeSlave(channel, node_id=50, heartbeat_ms=0)
    new_id = 9
    # Sent from a separate bus instance (python-can's virtual bus does not
    # loop a message back to its own sender) periodically so it's guaranteed
    # to land during the scan window.
    other_bus = can.Bus(interface="virtual", channel=channel, bitrate=250_000)
    free_form_task = other_bus.send_periodic(
        can.Message(arbitration_id=0x080 + new_id, is_extended_id=False,
                    data=b"\x00\x10\x00\x00\x00\x00\x00\x00"), 0.05
    )
    try:
        scan_result = scan_mod.scan(network, passive_seconds=0.3, sdo_timeout=SDO_TIMEOUT)
        conflict = setid.check_conflict(scan_result, old_id=50, new_id=new_id)

        assert conflict.has_conflict
        assert (0x080 + new_id) in conflict.conflicting_ids
    finally:
        free_form_task.stop()
        other_bus.shutdown()
        slave.close()


def test_conflict_detected_against_existing_node_id(channel, network):
    slave_a = FakeSlave(channel, node_id=20, heartbeat_ms=50)
    slave_b = FakeSlave(channel, node_id=21, heartbeat_ms=50)
    try:
        scan_result = scan_mod.scan(network, passive_seconds=0.4, sdo_timeout=SDO_TIMEOUT)
        conflict = setid.check_conflict(scan_result, old_id=20, new_id=21)

        assert conflict.has_conflict
        assert 21 in conflict.conflicting_nodes
    finally:
        slave_a.close()
        slave_b.close()


def test_set_raises_on_unconfirmed_conflict(channel, network):
    slave_a = FakeSlave(channel, node_id=30, heartbeat_ms=50)
    slave_b = FakeSlave(channel, node_id=31, heartbeat_ms=50)
    try:
        scan_result = scan_mod.scan(network, passive_seconds=0.4, sdo_timeout=SDO_TIMEOUT)
        recipes = recipes_module.load_recipes()

        try:
            setid.set_node_id(network, scan_result, old_id=30, new_id=31, recipes=recipes)
            assert False, "expected a ConflictError"
        except setid.ConflictError as exc:
            assert 31 in exc.conflict.conflicting_nodes
    finally:
        slave_a.close()
        slave_b.close()


def test_readonly_recipe_refuses_to_change():
    recipes = {"unknown": {"locked": {"method": "readonly", "note": "this tool cannot change it"}}}
    recipe = recipes_module.resolve_recipe(recipes, vendor_id=None, override="sdo:locked")
    assert recipe["method"] == "readonly"


def test_node_id_range_is_enforced(channel, network):
    """A recipe's node_id_range refuses an id outside it before any SDO traffic."""
    slave = FakeSlave(channel, node_id=5, vendor_id=0x777, heartbeat_ms=100)
    try:
        scan_result = scan_mod.scan(network, passive_seconds=0.4, sdo_timeout=SDO_TIMEOUT)
        recipes = {"vendors": {"0x777": {"name": "narrow", "method": "sdo", "node_id_range": [1, 63],
                                         "steps": [{"index": "0x2110", "sub": 1, "type": "u8", "value": "$NEW_ID"}],
                                         "store": "0x1010:01", "effect": "nmt_reset"}}}
        try:
            setid.set_node_id(network, scan_result, old_id=5, new_id=72, recipes=recipes)
            assert False, "expected a SetIdError for an out-of-range node id"
        except setid.SetIdError as exc:
            assert "range" in str(exc)
        assert slave.writes == []
    finally:
        slave.close()
