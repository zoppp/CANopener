"""REST routes (/api/decode/...) and MCP tools of the decoders. server.create_app and mcp_server.build_mcp call
register() and register_mcp() with one line each.

Writes to a CANopen node (SDO write, NMT) are plans: the routes return the plan (exact frames + token); the
UI shows it with CO.showPlan and runs it through /api/nodes/run, Claude through run_node_plan(token).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, File, Form, UploadFile
from fastapi.concurrency import run_in_threadpool

from ..core.transmit import parse_id
from ..paths import app_dir

Out = dict[str, Any]
UPLOAD_SUFFIXES = (".dbc", ".eds", ".dcf", ".xdd")


def _num(v) -> int:
    """Index/sub as given by the UI or Claude: an int, "0x1018" or "1018h" (hex), or a decimal string."""
    if isinstance(v, int):
        return v
    s = str(v).strip()
    return parse_id(s) if s.lower().startswith("0x") or s.lower().endswith("h") else int(s)


def _save_upload(name: str, content: bytes) -> Path:
    """Uploaded decode files are kept in <app folder>/decode so they are still there after a restart."""
    p = Path(name or "upload").name
    if Path(p).suffix.lower() not in UPLOAD_SUFFIXES or p.startswith("."):
        raise ValueError(f"not a DBC/EDS/DCF/XDD file: {p!r}")
    folder = app_dir() / "decode"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / p
    if target.exists() and target.read_bytes() != content:   # another node/channel may use the existing file
        stem, suffix = Path(p).stem, Path(p).suffix
        target = folder / f"{stem}-{hashlib.sha1(content).hexdigest()[:8]}{suffix}"
    if not target.exists():
        target.write_bytes(content)
    return target


def register(app, svc) -> None:
    dec = svc.decode

    async def call(fn, *a):
        return await run_in_threadpool(fn, *a)

    @app.get("/api/decode")
    async def decode_status():
        return {**dec.config(), **dec.status()}

    @app.get("/api/decode/text")
    async def decode_text():
        return {"text": dec.text_map()}

    # ---------------------------------------------------------------- DBC
    @app.get("/api/decode/dbc")
    async def dbc_info():
        return dec.dbc_info()

    @app.post("/api/decode/dbc/load")
    async def dbc_load(body: dict = Body(...)):
        return await call(dec.load_dbc, body.get("path", ""), body.get("channel"))

    @app.post("/api/decode/dbc/upload")
    async def dbc_upload(file: UploadFile = File(...), channel: str = Form("")):
        p = _save_upload(file.filename, await file.read())
        return await call(dec.load_dbc, str(p), channel)

    @app.post("/api/decode/dbc/remove")
    async def dbc_remove(body: dict = Body(...)):
        dec.remove_dbc(body.get("path", ""), body.get("channel"))
        return {"ok": True}

    @app.post("/api/decode/dbc/encode")
    async def dbc_encode(body: dict = Body(...)):
        return dec.encode(body.get("message", ""), body.get("signals") or {})

    # ---------------------------------------------------------------- J1939
    @app.get("/api/decode/j1939")
    async def j1939_info():
        return dec.j1939_info()

    @app.post("/api/decode/j1939")
    async def j1939_set(body: dict = Body(...)):
        return {"channels": dec.set_j1939(int(body.get("channel", 0)), bool(body.get("enabled", True)))}

    # ---------------------------------------------------------------- CANopen
    @app.get("/api/decode/canopen")
    async def canopen_info():
        return dec.canopen_info()

    @app.post("/api/decode/canopen/eds")
    async def eds_assign(body: dict = Body(...)):
        return await call(dec.assign_eds, int(body["node"]), body.get("path", ""), body.get("channel"))

    @app.post("/api/decode/canopen/eds/upload")
    async def eds_upload(file: UploadFile = File(...), node: int = Form(...), channel: str = Form("")):
        p = _save_upload(file.filename, await file.read())
        return await call(dec.assign_eds, node, str(p), channel)

    @app.post("/api/decode/canopen/eds/remove")
    async def eds_remove(body: dict = Body(...)):
        dec.remove_eds(int(body["node"]), body.get("channel"))
        return {"ok": True}

    @app.get("/api/decode/canopen/od")
    async def canopen_od(node: int, channel: int = 0):
        return {"node": node, "objects": dec.object_dictionary(node, channel)}

    @app.post("/api/decode/canopen/pdo_mapping")
    async def canopen_pdo_mapping(body: dict = Body(...)):
        return await call(dec.read_pdo_mapping, int(body.get("channel", 0)), int(body["node"]))

    @app.post("/api/decode/canopen/sdo_read")
    async def canopen_sdo_read(body: dict = Body(...)):
        return await call(dec.sdo_read, int(body.get("channel", 0)), int(body["node"]), _num(body["index"]),
                          _num(body.get("sub", 0)))

    @app.post("/api/decode/canopen/plan/sdo_write")
    async def canopen_plan_sdo_write(body: dict = Body(...)):
        return await call(dec.plan_sdo_write, int(body.get("channel", 0)), int(body["node"]), _num(body["index"]),
                          _num(body.get("sub", 0)), body.get("value"), body.get("type") or "")

    @app.post("/api/decode/canopen/plan/nmt")
    async def canopen_plan_nmt(body: dict = Body(...)):
        return await call(dec.plan_nmt, int(body.get("channel", 0)), int(body.get("node", 0)),
                          str(body.get("command", "")))

    @app.post("/api/decode/clear")
    async def decode_clear():
        dec.clear_logs()
        return {"ok": True}


def register_mcp(mcp, svc, run) -> None:
    dec = svc.decode

    @mcp.tool()
    def get_decode_config() -> Out:
        """Decoder setup: loaded DBC files (per channel or all), EDS/XDD per CANopen node, J1939 channels, counters."""
        return {**dec.config(), **dec.status()}

    @mcp.tool()
    def get_decoded_frames(channel: int = 0) -> Out:
        """The latest decoded text of every decoded id on a channel (as in the receive table's Decoded column)."""
        rows = {}
        for k, v in dec.text_map().items():
            ch, key = k.split(":")
            if int(ch) == channel:
                key = int(key)                     # id | (EXT, RTR, TX flags) << 29, as in the receive table
                label = f"0x{key & 0x1FFFFFFF:X}" + (" ext" if key & (1 << 29) else "") + \
                    (" rtr" if key & (1 << 30) else "") + (" tx" if key & (1 << 35) else "")
                rows[label] = v
        return {"channel": channel, "decoded": rows}

    @mcp.tool()
    def list_dbc_messages(name_filter: str = "") -> Out:
        """Messages and signals (unit, range, choices) of the loaded DBC files. name_filter: substring of the name."""
        msgs = [m for m in dec.dbc_info()["messages"] if name_filter.lower() in m["name"].lower()]
        return {"messages": msgs, "count": len(msgs)}

    @mcp.tool()
    def get_j1939_state() -> Out:
        """J1939: ECUs from address claims (NAME decoded), reassembled TP/BAM messages, DM1 active DTCs per ECU."""
        return dec.j1939_info()

    @mcp.tool()
    def get_canopen_state() -> Out:
        """CANopen decoding: EDS/XDD per node with its PDOs, NMT state per node (heartbeats), EMCY log."""
        return dec.canopen_info()

    @mcp.tool()
    def list_object_dictionary(node: int, channel: int = 0, name_filter: str = "") -> Out:
        """The object dictionary of a node from its EDS/XDD (index, sub, name, type, access, default)."""
        def go():
            objs = [o for o in dec.object_dictionary(node, channel) if name_filter.lower() in o["name"].lower()]
            return {"node": node, "objects": objs, "count": len(objs)}
        return run(go)

    @mcp.tool()
    def read_sdo(node: int, index: str, sub: int = 0, channel: int = 0) -> Out:
        """SDO upload of one object (changes nothing on the node). index: hex like "0x1018"."""
        return run(lambda: dec.sdo_read(channel, node, parse_id(index), sub))

    @mcp.tool()
    def load_pdo_mapping_from_device(node: int, channel: int = 0) -> Out:
        """Read a node's real PDO mapping over SDO (uploads only) and decode its PDOs with it instead of the EDS."""
        return run(lambda: dec.read_pdo_mapping(channel, node))

    @mcp.tool()
    def get_sdo_write_preview(node: int, index: str, sub: int, value: str, channel: int = 0,
                              type: Optional[str] = None) -> Out:
        """Plan an SDO write (type from the EDS, or u8/u16/u32/i8/i16/i32/u64/i64/f32/str/bytes): exact frames + token.
        Show the frames to the user; run_node_plan(token) executes it."""
        return run(lambda: dec.plan_sdo_write(channel, node, parse_id(index), sub, value, type or ""))

    @mcp.tool()
    def get_nmt_preview(node: int, command: str, channel: int = 0) -> Out:
        """Plan an NMT command (start, stop, preop, reset_node, reset_comm; node 0 = all): frames + token for
        run_node_plan."""
        return run(lambda: dec.plan_nmt(channel, node, command))

    @mcp.tool()
    def load_dbc_file(path: str, channel: Optional[int] = None) -> Out:
        """Load a DBC file (full path) for decoding and transmit by signal value; channel None = all channels."""
        return run(dec.load_dbc, path, channel)

    @mcp.tool()
    def assign_eds_file(node: int, path: str, channel: Optional[int] = None) -> Out:
        """Assign an EDS/DCF/XDD file (full path) to a CANopen node id for PDO decoding and the SDO browser."""
        return run(dec.assign_eds, node, path, channel)

    @mcp.tool()
    def set_j1939_decoding(channel: int, enabled: bool = True) -> Out:
        """Switch J1939 decoding (PGN split, address claim, TP/BAM, DM1) on or off for a channel."""
        return run(lambda: {"channels": dec.set_j1939(channel, enabled)})
