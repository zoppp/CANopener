"""EDS folders: identity of EDS/XDD files, matching against a node's 0x1018, and auto-assign against the simulator."""

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from canopener.decode import edslib
from canopener.server import create_app
from canopener.service import Service

DATA = Path(__file__).resolve().parent / "data"
EDS = (DATA / "test_node.eds").read_text(encoding="utf-8")
DEMO_A = (0x0CA0, 0x53494D01)            # canopener/sim.py: node 5, revision 0x00010000
DEMO_B = (0x0CA1, 0x53494D02)            # node 10


def eds(vendor, product, revision=None, name="x"):
    """test_node.eds with another identity in [DeviceInfo] (and a different description, so the content differs)."""
    text = EDS.replace("VendorNumber=0x00000CA0", f"VendorNumber=0x{vendor:08X}")
    text = text.replace("ProductNumber=0x00001001", f"ProductNumber=0x{product:08X}"
                        + (f"\nRevisionNumber=0x{revision:08X}" if revision is not None else ""))
    return text.replace("Description=", f"Description={name} ")


XDD = """<?xml version="1.0" encoding="utf-8"?>
<ISO15745ProfileContainer xmlns="http://www.canopen.org/xml/1.1"><ISO15745Profile><ProfileBody>
  <DeviceIdentity><vendorName>ACME</vendorName><vendorID>0x00000CA0</vendorID><productName>Drive</productName>
    <productID>0x53494D01</productID></DeviceIdentity></ProfileBody></ISO15745Profile>
<ISO15745Profile><ProfileBody><ApplicationLayers><CANopenObjectList>
  <CANopenObject index="1018" name="Identity" objectType="9" subNumber="4">
    <CANopenSubObject subIndex="03" name="Revision" objectType="7" dataType="0007" accessType="ro" defaultValue="0x00010000"/>
  </CANopenObject></CANopenObjectList></ApplicationLayers></ProfileBody></ISO15745Profile></ISO15745ProfileContainer>"""


def test_identity_of_eds_and_xdd(tmp_path):
    e = edslib.identity(DATA / "test_node.eds")
    assert (e["vendor"], e["product"], e["revision"], e["product_name"]) == (0xCA0, 0x1001, None, "Test node")
    (tmp_path / "d.xdd").write_text(XDD, encoding="utf-8")
    x = edslib.identity(tmp_path / "d.xdd")
    assert (x["vendor"], x["product"], x["revision"], x["product_name"]) == (0xCA0, 0x53494D01, 0x10000, "Drive")
    # [DeviceInfo] without the numbers: the 1018 defaults of the object list
    (tmp_path / "n.eds").write_text(EDS.replace("VendorNumber=0x00000CA0\n", ""), encoding="utf-8")
    assert edslib.identity(tmp_path / "n.eds")["vendor"] == 0xCA0


def test_scan_folders_finds_files_in_subfolders(tmp_path):
    (tmp_path / "a" / "b").mkdir(parents=True)
    (tmp_path / "a" / "one.EDS").write_text(eds(1, 2), encoding="utf-8")
    (tmp_path / "a" / "b" / "two.xdd").write_text(XDD, encoding="utf-8")
    (tmp_path / "a" / "notes.txt").write_text("x", encoding="utf-8")
    files, problems = edslib.scan_folders([str(tmp_path / "a"), str(tmp_path / "missing")])
    assert sorted(f["name"] for f in files) == ["one.EDS", "two.xdd"]
    assert problems == [f"{tmp_path / 'missing'}: folder not found"]


def test_best_match():
    f = lambda v, p, r, h: {"path": h, "vendor": v, "product": p, "revision": r, "hash": h}
    dev = {"vendor": 1, "product": 2, "revision": 3}
    assert edslib.best_match([f(1, 2, 3, "a"), f(1, 2, 4, "b")], dev)[0]["path"] == "a"      # exact revision wins
    assert edslib.best_match([f(1, 2, None, "a"), f(1, 9, 3, "b")], dev)[0]["path"] == "a"   # the one product fit
    assert edslib.best_match([f(1, 2, 4, "a"), f(1, 2, 5, "b")], dev)[0] is None             # two revisions: unclear
    assert edslib.best_match([f(1, 2, None, "a"), {**f(1, 2, None, "a"), "path": "copy"}], dev)[0]["path"] == "a"
    assert edslib.best_match([f(1, 9, 3, "a")], dev) == (None, [])                           # vendor only: never


@pytest.fixture
def client(channel):
    svc = Service(load_plugins=False)
    with TestClient(create_app(svc), base_url="http://127.0.0.1:8775") as c:
        c.svc = svc
        c.post("/api/sim/start", json={"channel": channel})
        assert c.post("/api/connect", json={"interface": "virtual", "channel": channel, "bitrate": 250000}).status_code == 200
        yield c


def test_auto_assign_and_candidates(client, tmp_path):
    lib = tmp_path / "eds"
    lib.mkdir()
    (lib / "demo_a.eds").write_text(eds(*DEMO_A, 0x00010000, "a"), encoding="utf-8")
    (lib / "demo_a_old.eds").write_text(eds(*DEMO_A, 0x00000001, "a-old"), encoding="utf-8")
    (lib / "demo_b_v1.eds").write_text(eds(*DEMO_B, None, "b1"), encoding="utf-8")
    (lib / "demo_b_v2.eds").write_text(eds(*DEMO_B, None, "b2"), encoding="utf-8")
    r = client.post("/api/decode/canopen/eds/folders", json={"folders": [str(lib) + os.sep, str(lib)]}).json()
    assert r["folders"] == [str(lib)] and len(r["files"]) == 4
    end = time.monotonic() + 3
    while time.monotonic() < end and len(client.get("/api/decode/canopen").json()["nmt"]) < 3:
        time.sleep(0.05)
    res = {x["node"]: x for x in client.post("/api/decode/canopen/eds/auto").json()["results"]}
    assert res[5]["result"] == "assigned" and res[5]["path"].endswith("demo_a.eds") and res[5]["match"] == "exact"
    assert res[10]["result"] == "several files fit" and len(res[10]["files"]) == 2
    assert res[20]["result"] == "no matching file"
    nodes = client.get("/api/decode/canopen").json()["nodes"]
    assert [(n["node"], Path(n["path"]).name) for n in nodes] == [(5, "demo_a.eds")]
    # a second run leaves the assigned node alone
    again = {x["node"]: x for x in client.post("/api/decode/canopen/eds/auto").json()["results"]}
    assert again[5]["result"] == "has EDS"
    # the assign dialog: the identity read over SDO, the two fits of node 10 first
    c = client.post("/api/decode/canopen/eds/candidates", json={"node": 10, "channel": 0}).json()
    assert c["identity"]["vendor"] == DEMO_B[0] and c["identity"]["source"].startswith("read from the node")
    assert [f["match"] for f in c["files"]] == ["product", "product", "", ""] and c["best"] is None
    # no window in a test: the UI falls back to a typed path
    assert client.post("/api/decode/browse", json={"kind": "folder"}).json() == {"available": False}


def test_candidates_without_identity(client):
    """A node that does not answer: the files are still listed, with why the identity is unknown."""
    c = client.post("/api/decode/canopen/eds/candidates", json={"node": 99, "channel": 0}).json()
    assert "error" in c["identity"] and c["files"] == []


def test_candidates_is_a_post_and_refuses_other_sites(client):
    """Review: it may read 0x1018 over SDO (sends on the bus); as a GET any web page could trigger it."""
    assert client.get("/api/decode/canopen/eds/candidates", params={"node": 10}).status_code == 405
    r = client.post("/api/decode/canopen/eds/candidates", json={"node": 10, "channel": 0},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_candidates_on_a_channel_that_cannot_talk_still_lists_the_files(client):
    """Review: listen-only / not connected raised NodeError, which was not caught: the dialog did not open."""
    r = client.post("/api/decode/canopen/eds/candidates", json={"node": 10, "channel": 4})
    assert r.status_code == 200 and "not connected" in r.json()["identity"]["error"]
