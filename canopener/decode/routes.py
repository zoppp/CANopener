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

from ..core.transmit import parse_bool, parse_id
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


def _native_dialog(kind: str) -> dict:
    """A Windows file/folder dialog over the app window, for a full path the server can use (an upload only
    gives the content). {"available": False} in a browser (`canopener serve`): the UI then asks for a typed path."""
    try:
        import webview
    except ImportError:
        return {"available": False}
    if not webview.windows:
        return {"available": False}
    if kind == "folder":
        picked = webview.windows[0].create_file_dialog(webview.FileDialog.FOLDER)
    else:
        picked = webview.windows[0].create_file_dialog(
            webview.FileDialog.OPEN, file_types=("EDS / DCF / XDD files (*.eds;*.dcf;*.xdd)", "All files (*.*)"))
    return {"available": True, "paths": [str(p) for p in (picked or [])]}


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

    @app.get("/api/decode/dbc/live")
    async def dbc_live():
        return await call(dec.dbc_live)

    @app.get("/api/decode/dbc/message")
    async def dbc_message(name: str):
        return await call(dec.dbc_message, name)

    @app.post("/api/decode/dbc/encode")
    async def dbc_encode(body: dict = Body(...)):
        return dec.encode(body.get("message", ""), body.get("signals") or {})

    # ---------------------------------------------------------------- J1939
    @app.get("/api/decode/j1939")
    async def j1939_info():
        return dec.j1939_info()

    @app.get("/api/decode/j1939/source")
    async def j1939_source(sa: int, channel: str = ""):
        return await call(dec.j1939_source, sa, channel)

    @app.post("/api/decode/j1939")
    async def j1939_set(body: dict = Body(...)):
        return {"channels": dec.set_j1939(int(body.get("channel", 0)), parse_bool(body.get("enabled", True), "enabled"))}

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

    @app.post("/api/decode/canopen/sdo_server")
    async def sdo_server_add(body: dict = Body(...)):
        return dec.add_sdo_server(int(body["node"]), body["request"], body["response"], body.get("channel"))

    @app.post("/api/decode/canopen/sdo_server/remove")
    async def sdo_server_remove(body: dict = Body(...)):
        dec.remove_sdo_server(int(body["node"]), body["request"], body.get("channel"))
        return {"ok": True}

    @app.get("/api/decode/canopen/eds/folders")
    async def eds_folders():
        return await call(dec.eds_library)

    @app.post("/api/decode/canopen/eds/folders")
    async def eds_folders_set(body: dict = Body(...)):
        return await call(dec.set_eds_folders, list(body.get("folders") or []))

    @app.post("/api/decode/canopen/eds/candidates")      # POST: it may read 0x1018 over SDO (sends on the bus),
    async def eds_candidates(body: dict = Body(...)):      # so the cross-site check applies (a GET would not)
        return await call(dec.eds_candidates, int(body.get("node", 0)), int(body.get("channel", 0)))

    @app.post("/api/decode/canopen/eds/auto")
    async def eds_auto():
        return await call(dec.auto_assign_eds)

    @app.post("/api/decode/browse")
    async def browse(body: dict = Body(...)):
        return await call(_native_dialog, str(body.get("kind", "file")))

    @app.get("/api/decode/canopen/node")
    async def canopen_node(node: int, channel: str = ""):
        return await call(dec.canopen_node, node, channel)

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
    async def decode_clear(body: dict = Body(None)):
        dec.clear_logs(str((body or {}).get("what") or ""))
        return {"ok": True}


def register_mcp(mcp, svc, run) -> None:
    dec = svc.decode

    @mcp.tool()
    def get_decode_config() -> Out:
        """Decoder setup: loaded DBC files (per channel or all), EDS/XDD per CANopen node, extra SDO servers, J1939
        channels, counters."""
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
    def get_pdo_values(node: int, channel: Optional[int] = None) -> Out:
        """The PDOs of a CANopen node with every mapped object (index:sub, bit position, raw and decoded value, unit,
        range), last seen and period, from the last frame of each PDO. Without an EDS: the frames on the predefined
        PDO ids, undecoded. channel None = every channel."""
        def go():
            d = dec.canopen_node(node, channel)
            return {"node": node, "eds": d["eds"], "pdos": d["pdos"]}
        return run(go)

    @mcp.tool()
    def get_sdo_log(node: int, channel: Optional[int] = None, limit: int = 100) -> Out:
        """SDO transfers of a CANopen node seen on the bus, newest first: source ("bus" = another master, "CANopener"
        = this tool), read/write, index:sub, name, value, result (ok / abort code and text). Also the last value
        seen per object ("index:sub", decimal). channel None = every channel."""
        def go():
            d = dec.canopen_node(node, channel)
            return {"node": node, "sdo": d["sdo"][:max(1, limit)], "values": d["values"]}
        return run(go)

    @mcp.tool()
    def get_dbc_message_values(name: str) -> Out:
        """One DBC message: every signal decoded from its last frame (raw and scaled value, unit, range, value
        name), with last seen, period and data."""
        return run(dec.dbc_message, name)

    @mcp.tool()
    def get_j1939_source(sa: int, channel: Optional[int] = None) -> Out:
        """One J1939 source address: the PGNs it sends (last frame, period, signals when a J1939 DBC covers the
        PGN), its address claim and its reassembled TP messages. channel None = every J1939 channel."""
        return run(dec.j1939_source, sa, channel)

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
    def add_sdo_server(node: int, request_cob_id: str, response_cob_id: str, channel: Optional[int] = None) -> Out:
        """Follow a node's SDO transfers on another COB-ID pair too, for a second SDO server (0x1201...) such as
        request 0x627 / response 0x5A7 for node 7: the SDO log (get_sdo_log) then shows them under that node, decoded
        with its EDS. COB-IDs in hex. Only changes what the decoder listens to; nothing is sent."""
        return run(dec.add_sdo_server, node, request_cob_id, response_cob_id, channel)

    @mcp.tool()
    def remove_sdo_server(node: int, request_cob_id: str, channel: Optional[int] = None) -> Out:
        """Stop following an extra SDO server added with add_sdo_server (same node, request COB-ID and channel)."""
        return run(lambda: (dec.remove_sdo_server(node, request_cob_id, channel), {"ok": True})[1])

    @mcp.tool()
    def set_j1939_decoding(channel: int, enabled: bool = True) -> Out:
        """Switch J1939 decoding (PGN split, address claim, TP/BAM, DM1) on or off for a channel."""
        return run(lambda: {"channels": dec.set_j1939(channel, enabled)})
