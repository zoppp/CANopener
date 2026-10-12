"""Tests that back the user guide's claims about the EDS editor (docs/user-guide.md, "EDS editor" section).
Each test names the guide line it backs. Claims the code does not keep are strict xfails ("CLAIM FALSE")."""

import sys
from pathlib import Path

import pytest

from canopener.ext import load_plugin

ROOT = Path(__file__).resolve().parent.parent
if "canopener_plugin_eds_editor.edsdoc" not in sys.modules:
    load_plugin(ROOT / "plugins" / "eds-editor")
import canopener_plugin_eds_editor.device as D  # noqa: E402  (the plugin's own modules, loaded above)
import canopener_plugin_eds_editor.edsdoc as E  # noqa: E402

JS = "plugins/eds-editor/ui/eds-editor.js"


def texts(doc, level=None):
    return " | ".join(p["text"] for p in E.check(doc) if level is None or p["level"] == level)


def writes(r):
    return [(s["index"], s["sub"], s["value"]) for s in r["steps"] if s["op"] == "sdo_write"]


# ------------------------------------------------------------------------------------------- problem checks

def test_problem_unknown_data_type():
    """user-guide.md:607 the problem list finds unknown data types."""
    doc = E.template("301")
    E.find(doc, 0x1017)["data_type"] = 0x00EE
    assert "unknown data type 0x00EE" in texts(doc, "error")


def test_problem_unknown_access_type():
    """user-guide.md:607 the problem list finds unknown access types."""
    doc = E.template("301")
    E.find(doc, 0x1017)["access"] = "rx"
    assert "access type 'rx' is not one of" in texts(doc, "error")


def test_problem_value_outside_limits():
    """user-guide.md:608 the problem list finds values outside their limits."""
    doc = E.template("301")
    v = E.find(doc, 0x1017)
    v.update(low="10", high="100", default="500")
    assert "default 500 is outside the limits" in texts(doc)


def test_problem_mapped_object_does_not_exist():
    """user-guide.md:610 the problem list finds mapped objects that do not exist."""
    doc = E.template("402")
    E.find_var(doc, 0x1A00, 1)["default"] = "0x2ABC0010"
    assert "TPDO 1: maps 0x2ABC:00, which does not exist" in texts(doc, "error")


def test_problem_tpdo_maps_unreadable_object():
    """user-guide.md:610-611 the problem list finds mapped objects with the wrong access (TPDO side)."""
    doc = E.template("402")
    E.find(doc, 0x6041)["access"] = "wo"
    assert "which is wo (a TPDO needs a readable object)" in texts(doc, "error")


def test_problem_device_info_differs_from_identity():
    """user-guide.md:613 the problem list finds DeviceInfo numbers that differ from 0x1018."""
    doc = E.template("301", "ACME", "0x00000ABC", "W", "0x00000002")
    E.section(doc, "DeviceInfo")["VendorNumber"] = "0x00000ABD"
    E.section(doc, "DeviceInfo")["ProductNumber"] = "0x3"
    t = texts(doc)
    assert "DeviceInfo VendorNumber 0x00000ABD differs from the default of 0x1018:01" in t
    assert "DeviceInfo ProductNumber 0x3 differs from the default of 0x1018:02" in t
    E.section(doc, "DeviceInfo").update(VendorNumber="0xABC", ProductNumber="2")   # same numbers, other spelling
    assert "differs from the default of 0x1018" not in texts(doc)


# ------------------------------------------------------------------------------------------- PDO editor

@pytest.mark.skip(reason=f"UNTESTABLE from Python: browser-only guard in {JS}:418 and :425 (onPdoInput); "
                         "decode_cob/check in edsdoc.py do not cap the 11-bit id")
def test_pdo_11bit_id_stops_at_7ff():
    """user-guide.md:633-634 an 11-bit id stops at 7FF; tick 29-bit for longer ones."""


@pytest.mark.skip(reason=f"UNTESTABLE from Python: browser-only guard in {JS}:427 (onPdoInput refuses "
                         "enabling CAN id 0); the Python side only flags id 0 as NMT in check()")
def test_pdo_can_id_0_cannot_be_enabled():
    """user-guide.md:634 a PDO with CAN id 0 cannot be enabled."""


@pytest.mark.skip(reason=f"UNTESTABLE from Python: [DummyUsage] is set by the browser in {JS}:569 (mapAdd); "
                         "Python only warns when it is not set (see test_dummy_needs_dummy_usage)")
def test_dummy_gap_sets_dummy_usage():
    """user-guide.md:648-649 a dummy gap sets [DummyUsage] to allow it."""


def test_dummy_needs_dummy_usage():
    """user-guide.md:648-649 (Python side) a dummy is allowed once [DummyUsage] has it, else flagged."""
    doc = E.template("402")
    E.find_var(doc, 0x1601, 0)["default"] = "3"
    E.find_var(doc, 0x1601, 3)["default"] = "0x00050008"                    # 8-bit gap (UNSIGNED8 dummy)
    assert "[DummyUsage] Dummy0005 is not 1" in texts(doc, "warn")
    E.section(doc, "DummyUsage")["Dummy0005"] = "1"
    assert "Dummy" not in texts(doc)


@pytest.mark.parametrize("cob, why", [
    ("0x00000080", "SYNC"), ("0x00000100", "TIME"), ("$NODEID+0x80", "EMCY"), ("$NODEID+0x580", "SDO (server"),
    ("$NODEID+0x600", "SDO (client"), ("$NODEID+0x700", "heartbeat"), ("0x000007E5", "reserved (LSS"), ("0x00000000", "NMT")])
def test_problem_cob_id_restricted_by_cia301(cob, why):
    """user-guide.md:659-660 the problem list flags CAN ids CiA 301 keeps for other services."""
    doc = E.template("301")
    E.find_var(doc, 0x1800, 1)["default"] = cob
    t = texts(doc, "error")
    assert "TPDO 1: CAN id" in t and f"restricted by CiA 301 ({why}" in t


def test_problem_cob_id_above_7ff_for_the_node():
    """user-guide.md:659-660 the problem list flags ids above 7FF for some node id."""
    doc = E.template("301")
    doc["format"] = "dcf"
    E.section(doc, "DeviceComissioning", create=True)["NodeID"] = "0x20"
    E.find_var(doc, 0x1800, 1)["value"] = "$NODEID+0x7F0"                   # 0x810 on node 0x20
    assert "TPDO 1: CAN id 0x810 (node 32) is beyond 0x7FF" in texts(doc, "error")
    E.find_var(doc, 0x1800, 1)["value"] = "$NODEID+0x180"
    assert "CAN id" not in texts(doc)


# ------------------------------------------------------------------------------------------- compare with a node

def _compare_doc():
    doc = E.blank()
    for i in range(6):
        doc["objects"].append(E.new_object(0x2300 + i, f"V{i}", data_type=0x0005, access="ro", default=str(i)))
    return doc


def test_compare_keeps_rows_on_stop():
    """user-guide.md:668-669 compare with a node keeps what was read so far when Stop is pressed."""
    calls = []

    def upload(i, s):
        calls.append(i)
        return bytes([i - 0x2300])
    r = D.compare(_compare_doc(), upload, 5, stopped=lambda: len(calls) >= 3)
    assert [(x["index"], x["status"]) for x in r["rows"]] == [(0x2300, "same"), (0x2301, "same"), (0x2302, "same")]
    assert r["stopped"].startswith("stopped at 0x2303")


def test_compare_keeps_rows_when_the_node_stops_answering():
    """user-guide.md:668-669 compare with a node keeps what was read so far when the node stops answering."""
    def upload(i, s):
        if i <= 0x2301:
            return bytes([i - 0x2300])
        raise D.NoAnswer("timeout")
    r = D.compare(_compare_doc(), upload, 5)
    assert [(x["index"], x["status"]) for x in r["rows"][:2]] == [(0x2300, "same"), (0x2301, "same")]
    assert "stopped answering" in r["stopped"]


# ------------------------------------------------------------------------------------------- DCF -> Nodes steps

def test_pdo_that_cannot_be_disabled_keeps_its_mapping():
    """user-guide.md:697-698 a PDO that cannot be disabled (COB-ID not writable) keeps its mapping."""
    doc = E.template("402")
    doc["format"] = "dcf"
    E.find_var(doc, 0x1800, 1)["access"] = "ro"
    E.find_var(doc, 0x1A00, 0)["value"] = "2"
    E.find_var(doc, 0x1A00, 2)["value"] = "0x60640020"
    r = D.dcf_steps(doc, 7)
    assert not [w for w in writes(r) if w[0] in ("0x1A00", "0x1800")]
    assert any("TPDO 1: the mapping is not written, the PDO cannot be disabled first" in s for s in r["skipped"])


def _dcf301():
    doc = E.template("301")
    doc["format"] = "dcf"
    doc["objects"].append(E.new_object(0x1012, "COB-ID TIME", data_type=0x0007, access="rw", default="0x00000100"))
    doc["objects"].append(E._rec(0x1280, "SDO client parameter", [
        ("COB-ID client to server", 0x0007, "rw", "0x80000000", False),
        ("COB-ID server to client", 0x0007, "rw", "0x80000000", False),
        ("Node-ID of the SDO server", 0x0005, "rw", "0", False)]))
    doc["objects"].sort(key=lambda o: o["index"])
    return doc


def test_sync_cob_id_switched_off_before_it_changes():
    """user-guide.md:699 the SYNC COB-ID is switched off (producer bit 30 cleared) before its id changes."""
    doc = _dcf301()
    E.find(doc, 0x1005)["value"] = "0x40000081"
    assert writes(D.dcf_steps(doc, 5)) == [("0x1005", 0, 0x00000081), ("0x1005", 0, 0x40000081)]


def test_time_cob_id_switched_off_before_it_changes():
    """user-guide.md:699 the TIME COB-ID is switched off (bits 31 and 30 cleared) before its id changes."""
    doc = _dcf301()
    E.find(doc, 0x1012)["value"] = "0x80000104"
    assert writes(D.dcf_steps(doc, 5)) == [("0x1012", 0, 0x00000104), ("0x1012", 0, 0x80000104)]


def test_sdo_cob_ids_switched_off_before_they_change():
    """user-guide.md:699 SDO channel COB-IDs are switched off (bit 31 set) before their id changes."""
    doc = _dcf301()
    E.find_var(doc, 0x1280, 1)["value"] = "0x00000605"
    E.find_var(doc, 0x1280, 2)["value"] = "0x00000585"
    assert writes(D.dcf_steps(doc, 5)) == [("0x1280", 1, 0x80000605), ("0x1280", 1, 0x605),
                                           ("0x1280", 2, 0x80000585), ("0x1280", 2, 0x585)]


def test_invalid_values_are_skipped_and_listed():
    """user-guide.md:700 values that are not valid for their type are skipped and listed."""
    doc = E.template("301")
    doc["format"] = "dcf"
    E.find(doc, 0x1017)["value"] = "abc"
    r = D.dcf_steps(doc, 5)
    assert writes(r) == [] and any("0x1017:00" in s and "'abc' is not a valid UNSIGNED16" in s for s in r["skipped"])


@pytest.mark.xfail(strict=True, reason="CLAIM FALSE: a number out of range for its type (70000 for an UNSIGNED16) "
                                       "is not skipped; dcf_steps only skips values that fail to parse "
                                       "(device.py:343-348; _step_value device.py:302 and int_value edsdoc.py:139 do no range check) and writes "
                                       "a u16 step with value 70000")
def test_out_of_range_values_are_skipped():
    """user-guide.md:700 values that are not valid for their type (out of range) are skipped and listed."""
    doc = E.template("301")
    doc["format"] = "dcf"
    E.find(doc, 0x1017)["value"] = "70000"
    r = D.dcf_steps(doc, 5)
    assert writes(r) == [] and any("0x1017:00" in s for s in r["skipped"])


def test_no_store_without_1010_01():
    """user-guide.md:701 without 0x1010:01 in the file there is no store."""
    doc = E.template("301")
    doc["format"] = "dcf"
    E.find(doc, 0x1017)["value"] = "100"
    assert D.dcf_steps(doc, 5)["steps"][-1]["op"] == "store"                # with 0x1010:01: stored
    doc["objects"] = [o for o in doc["objects"] if o["index"] != 0x1010]
    r = D.dcf_steps(doc, 5)
    assert writes(r) == [("0x1017", 0, 100)] and not [s for s in r["steps"] if s["op"] == "store"]
    assert any("no 0x1010:01" in s for s in r["skipped"])
