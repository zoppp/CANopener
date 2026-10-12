"""EDS editor: create, open, check and save CANopen EDS/DCF files; compare them with a node, read a node's
values into a DCF, build an EDS by probing a node, and hand a DCF to the Nodes tab as custom steps.

The editor never writes to a node. Device work only reads (SDO uploads) and runs as a node job, so it holds the
bus lock, shows in the Nodes job panel, can be stopped and is logged in logs/nodes.log. Writing a DCF to a
node is done by the Nodes tab (preview, confirm, run) from the steps /dcf-steps returns.

Files are saved in <app folder>/eds.
"""

from __future__ import annotations

import codecs
import threading
from pathlib import Path
from typing import Optional

from fastapi import Body, File, HTTPException, UploadFile

from canopener.ext import Extension
from canopener.nodes.service import NodeError
from canopener.paths import app_dir

from . import device as D
from . import edsdoc as E

SUFFIXES = (".eds", ".dcf", ".xdd", ".xdc")


def eds_dir() -> Path:
    d = app_dir() / "eds"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_bytes(data: bytes, name: str) -> dict:
    if Path(name).suffix.lower() in (".xdd", ".xdc"):
        return E.read_xdd(data, Path(name).name)
    return E.read(data, Path(name).name)


def load_path(path: str) -> dict:
    p = Path(str(path).strip().strip('"'))
    if p.suffix.lower() not in SUFFIXES:
        raise ValueError(f"not an EDS/DCF/XDD file: {p.name}")
    if not p.is_file():
        raise ValueError(f"{p} not found")
    return load_bytes(p.read_bytes(), p.name)


def _file_name(name: str, fmt: str) -> str:
    p = Path(Path(str(name or "").strip()).name)
    stem = p.stem if p.suffix.lower() in SUFFIXES else p.name
    if not stem or stem.startswith("."):
        raise ValueError("give the file a name")
    return f"{stem}.{fmt}"


def save(doc: dict, name: str, fmt: str, overwrite: bool = False) -> Path:
    fmt = fmt.lower()
    if fmt not in ("eds", "dcf"):
        raise ValueError("save as eds or dcf")
    target = eds_dir() / _file_name(name, fmt)
    if target.exists() and not overwrite:
        raise FileExistsError(target.name)
    text = E.write(doc, fmt, target.name)
    enc = str(doc.get("encoding") or "cp1252")
    try:
        codecs.lookup(enc)
    except LookupError:
        enc = "cp1252"
    target.write_bytes(text.encode(enc, errors="replace"))
    return target


def _ranges(text: str) -> tuple:
    """"2000-20FF, 6000-60FF" (hex) -> ((0x2000, 0x20FF), (0x6000, 0x60FF)); empty = the full scan."""
    if not str(text or "").strip():
        return D.FULL_RANGES
    out = []
    for part in str(text).replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        lo, hi = int(a.strip().removeprefix("0x"), 16), int((b or a).strip().removeprefix("0x"), 16)
        if not (0x1000 <= lo <= hi <= 0xFFFF):
            raise ValueError(f"range {part!r}: use hex indices 1000..FFFF, low-high")
        out.append((lo, hi))
    return tuple(out)


class EdsEditor(Extension):
    title = "EDS editor"

    def setup(self, svc) -> None:
        self.svc = svc
        self._lock = threading.Lock()
        self.progress = {"job": None, "kind": "", "done": 0, "total": 0, "found": 0}
        self.results: dict[int, dict] = {}            # job id -> full result (the job itself only has a summary)

    # ------------------------------------------------------------------ device jobs
    def _start(self, kind: str, title: str, channel: int, node: int, work) -> dict:
        nodes = self.svc.nodes
        if not 1 <= int(node) <= 127:
            raise ValueError("node id 1..127")

        def fn(logline, job):
            with self._lock:
                self.progress = {"job": job.id, "kind": kind, "done": 0, "total": 0, "found": 0}
            with nodes.network(int(channel)) as (net, _):          # refuses a listen-only channel
                remote = net.add_node(int(node))
                remote.sdo.RESPONSE_TIMEOUT = 0.25
                from canopen.sdo import SdoAbortedError, SdoCommunicationError

                def upload(index: int, sub: int) -> bytes:
                    try:
                        with remote.sdo.open(index, sub, "rb", buffering=0) as fp:
                            size = fp.size              # None: an expedited reply that did not give its size
                            return D.Reply(fp.read(), size is not None)
                    except SdoAbortedError as exc:
                        raise D.Abort(exc.code) from exc
                    except SdoCommunicationError as exc:
                        raise D.NoAnswer(str(exc)) from exc

                last = [0]

                def progress(done, total, found=None):
                    with self._lock:
                        self.progress.update(done=done, total=total, found=found if found is not None else 0)
                    pct = done * 10 // max(total, 1)
                    if pct > last[0]:
                        last[0] = pct
                        logline(f"{pct * 10} % ({done}/{total})" + (f", {found} object(s)" if found is not None else ""))

                try:                               # Stop and a silent node end early, with what was read so far
                    result, summary = work(upload, progress, lambda: job.stop_requested)
                finally:
                    del net[int(node)]
            self.results = {job.id: result}                    # keep only the latest
            logline(summary)
            return {"summary": summary}

        job = nodes.start_job(kind, title, fn)
        return job.as_dict()

    def compare_job(self, doc: dict, channel: int, node: int) -> dict:
        def work(upload, progress, stopped):
            r = D.compare(doc, upload, int(node), progress, stopped)
            counts = {}
            for row in r["rows"]:
                counts[row["status"]] = counts.get(row["status"], 0) + 1
            summary = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "nothing read"
            return {"rows": r["rows"], "counts": counts, "node": node, "stopped": r["stopped"]}, \
                summary + (f" ({r['stopped']})" if r["stopped"] else "")
        return self._start("eds-compare", f"Compare node {node} with {doc.get('file_name') or 'the EDS'}",
                           channel, node, work)

    def probe_job(self, channel: int, node: int, ranges: str = "") -> dict:
        rng = _ranges(ranges)

        def work(upload, progress, stopped):
            doc = D.probe(upload, rng, progress, stopped, node_id=int(node))
            doc["file_name"] = f"node{node}.eds"
            early = [n["text"] for n in doc["notes"] if n["level"] == "warn"]
            return {"doc": doc, "problems": E.check(doc), "node": node}, \
                f"{len(doc['objects'])} object(s) found" + (f" ({'; '.join(early)})" if early else "")
        return self._start("eds-probe", f"Create EDS from node {node}", channel, node, work)

    # ------------------------------------------------------------------ REST
    def routes(self, router, svc) -> None:
        def answer(doc: dict) -> dict:
            return {"doc": doc, "problems": E.check(doc)}

        def bad(exc: Exception):
            raise HTTPException(400, str(exc))

        @router.get("/templates")
        def templates():
            return {"templates": [{"id": "301", "title": "CiA 301 device (communication objects, 4 RPDO + 4 TPDO)"},
                                  {"id": "401", "title": "CiA 401 generic I/O (digital and analog in/out)"},
                                  {"id": "402", "title": "CiA 402 drive (controlword, statusword, modes, targets)"}],
                    "data_types": [{"code": c, "name": n, "kind": k, "bits": b} for c, (n, k, b, _) in E.DATA_TYPES.items()],
                    "access": list(E.ACCESS), "folder": str(eds_dir())}

        @router.post("/new")
        def new(body: dict = Body(...)):
            name = _file_name(body.get("file_name") or "device.eds", "eds")
            return answer(E.template(str(body.get("profile", "301")), body.get("vendor_name", ""),
                                     body.get("vendor_number") or "0x00000000", body.get("product_name", ""),
                                     body.get("product_number") or "0x00000000", name))

        @router.post("/open")
        def open_path(body: dict = Body(...)):
            try:
                return answer(load_path(body.get("path", "")))
            except (ValueError, E.EdsError, OSError) as exc:
                bad(exc)

        @router.post("/upload")
        async def upload(file: UploadFile = File(...)):
            name = Path(file.filename or "upload.eds").name
            if Path(name).suffix.lower() not in SUFFIXES:
                raise HTTPException(400, f"not an EDS/DCF/XDD file: {name}")
            try:
                return answer(load_bytes(await file.read(), name))
            except E.EdsError as exc:
                bad(exc)

        @router.get("/files")
        def files():
            out = []
            for folder in (eds_dir(), app_dir() / "decode"):
                if folder.is_dir():
                    out += [{"name": p.name, "path": str(p), "folder": folder.name, "size": p.stat().st_size,
                             "mtime": p.stat().st_mtime} for p in sorted(folder.iterdir())
                            if p.is_file() and p.suffix.lower() in SUFFIXES]
            return {"files": out, "folder": str(eds_dir())}

        @router.post("/check")
        def check(body: dict = Body(...)):
            return {"problems": E.check(body["doc"])}

        @router.post("/pdo/add")
        def pdo_add(body: dict = Body(...)):
            """The next free PDO's two objects; the editor adds them to its document."""
            doc = body["doc"]
            kind = str(body.get("kind", "")).lower()
            if kind not in E.PDO_KINDS:
                bad(ValueError("kind: rpdo or tpdo"))
            try:
                n = E.add_pdo(doc, kind)
            except E.EdsError as exc:
                bad(exc)
            return {"number": n, "objects": [E.find(doc, base + n - 1) for base in E.PDO_KINDS[kind]]}

        @router.post("/text")
        def text(body: dict = Body(...)):
            fmt = str(body.get("format") or body["doc"].get("format") or "eds").lower()
            name = _file_name(body.get("name") or body["doc"].get("file_name") or "device", fmt)
            return {"text": E.write(body["doc"], fmt, name), "name": name}

        @router.post("/save")
        def save_route(body: dict = Body(...)):
            try:
                p = save(body["doc"], body.get("name") or body["doc"].get("file_name", ""),
                         str(body.get("format") or "eds"), bool(body.get("overwrite")))
            except FileExistsError as exc:
                raise HTTPException(409, f"{exc} already exists") from None
            except ValueError as exc:
                bad(exc)
            return {"path": str(p), "name": p.name}

        @router.post("/decoder")
        def decoder(body: dict = Body(...)):
            """Save the document and assign it to a node in the CANopen decoder."""
            try:
                p = save(body["doc"], body.get("name") or body["doc"].get("file_name", ""),
                         str(body.get("format") or "eds"), bool(body.get("overwrite")))
                return {"path": str(p), **svc.decode.assign_eds(int(body["node"]), str(p), body.get("channel"))}
            except FileExistsError as exc:
                raise HTTPException(409, f"{exc} already exists") from None
            except (ValueError, KeyError) as exc:
                bad(exc)

        @router.post("/dcf-steps")
        def dcf_steps(body: dict = Body(...)):
            try:
                return D.dcf_steps(body["doc"], int(body["node"]), bool(body.get("only_changed", True)),
                                   bool(body.get("preop", True)), bool(body.get("store", True)),
                                   bool(body.get("reset", False)))
            except (ValueError, KeyError, OverflowError) as exc:
                bad(exc)

        @router.post("/device/compare")
        def compare(body: dict = Body(...)):
            try:
                return self.compare_job(body["doc"], int(body.get("channel", 0)), int(body["node"]))
            except Exception as exc:                  # NodeError (busy, not connected, listen-only), ValueError
                bad(exc)

        @router.post("/device/probe")
        def probe(body: dict = Body(...)):
            try:
                return self.probe_job(int(body.get("channel", 0)), int(body["node"]), body.get("ranges", ""))
            except Exception as exc:
                bad(exc)

        @router.get("/device/progress")
        def progress():
            with self._lock:
                return dict(self.progress)

        @router.get("/device/result")
        def result(job: int):
            r = self.results.get(job)
            if r is None:
                raise HTTPException(404, "no result for this job (it failed, or a newer job replaced it)")
            return r

    # ------------------------------------------------------------------ MCP
    def mcp_tools(self, mcp, svc, run) -> None:
        @mcp.tool()
        def get_eds_problems(path: str) -> dict:
            """Check an EDS/DCF/XDD file (CiA 306 rules, PDO mappings, COB-IDs, value ranges): the problem list."""
            return run(lambda: {"problems": E.check(load_path(path))})

        @mcp.tool()
        def list_eds_objects(path: str, name_filter: str = "") -> dict:
            """The objects of an EDS/DCF/XDD file: index, sub, name, data type, access, default, PDO-mappable."""
            def go():
                f = name_filter.lower()
                rows = [{"index": f"0x{i:04X}", "sub": s, "name": v.get("name", ""),
                         "type": E.type_name(v.get("data_type") or 0), "access": v.get("access", ""),
                         "default": v.get("default", ""), "value": v.get("value", ""), "pdo": bool(v.get("pdo"))}
                        for o, v, i, s in E.variables(load_path(path))
                        if not f or f in v.get("name", "").lower() or f in o.get("name", "").lower()]
                return {"objects": rows[:2000], "truncated": len(rows) > 2000}
            return run(go)

        @mcp.tool()
        def get_eds_pdos(path: str, node_id: int = 0) -> dict:
            """The PDOs of an EDS/DCF file, decoded: COB-ID (valid, RTR, 29-bit, node relative; the id for node_id),
            transmission type in words, inhibit time, event timer, and the mapped objects with their bit position."""
            return run(lambda: {"pdos": E.pdos(load_path(path), node_id or None)})

        @mcp.tool()
        def read_node_vs_eds(path: str, node: int, channel: int = 0) -> dict:
            """Read every readable object of an EDS/DCF from a node (SDO uploads only, nothing is written) and
            list where the node differs from the file."""
            def go():
                job = self.compare_job(load_path(path), channel, node)
                j = svc.nodes.wait(svc.nodes.job, 900)
                if j.state != "done":
                    return {"error": j.error or j.state}
                r = self.results.get(job["id"], {})
                return {"counts": r.get("counts"), "stopped": r.get("stopped", ""),
                        "rows": [x for x in r.get("rows", []) if x["status"] != "same"]}
            return run(go)

        @mcp.tool()
        def create_eds_from_node(node: int, channel: int = 0, ranges: str = "", save_as: str = "") -> dict:
            """Probe a node (SDO uploads only) and save what it answers as an EDS in <app folder>/eds.
            ranges: hex index ranges like "1000-1FFF, 2000-2FFF" (empty = 1000-9FFF, a few minutes).
            Guessed data types and access are listed as problems to check."""
            def go():
                job = self.probe_job(channel, node, ranges)
                j = svc.nodes.wait(svc.nodes.job, 1800)
                if j.state != "done":
                    return {"error": j.error or j.state}
                r = self.results.get(job["id"], {})
                p = save(r["doc"], save_as or r["doc"]["file_name"], "eds", overwrite=bool(save_as))
                probs = r["problems"]
                return {"path": str(p), "objects": len(r["doc"]["objects"]),
                        "errors": sum(1 for x in probs if x["level"] == "error"),
                        "warnings": sum(1 for x in probs if x["level"] == "warn")}
            return run(go)


EXTENSION = EdsEditor()
