"""MCP endpoint (streamable HTTP at /mcp/, same process and same bus session as the UI).

Tool naming says whether a tool changes anything:
- get_ / list_ / read_ / scan_ tools only read (scan_nodes sends SDO reads, nothing that changes a node) and may
  be allowlisted;
- every other verb (connect_, send_, set_, start_, stop_, run_, save_, open_ ...) changes the bus, a node or
  files and should stay on approval, so the user sees each call.
Writes to nodes are two tools: a get_..._preview tool returns the exact frames and a token, run_node_plan(token)
executes that plan unchanged. Never a dry-run flag on a write tool.
"""

from __future__ import annotations

from typing import Any, Optional

from mcp.server.mcpserver import MCPServer

from .core import adapters
from .core.channel import F_EXT, F_FD, F_RTR, F_TX
from .core.engine import EngineError
from .core.transmit import parse_data, parse_id
from .decode.routes import register_mcp as register_decode_tools
from .dash import routes as dash_routes
from .nodes.service import NodeError

Out = dict[str, Any]

INSTRUCTIONS = """CANopener: CAN bus toolbox (PCAN-View replacement) running on the user's PC. One bus session is shared
with the user's app window. Channels are numbered from 0 (channel 0 = CAN1 in the UI). CAN ids are hex strings
("0x18FEF100" or "18FEF100"); data is hex bytes ("11 22 33").
Typical flow: get_status -> list_devices / connect_channel (approval) -> get_receive_table, get_trace, get_bus_stats.
CANopen nodes: scan_nodes -> get_node_scan -> get_set_node_id_preview or get_baud_preview or
get_sequence_preview (exact frames + token) -> show the frames to the user -> run_node_plan(token, title)
(approval)
-> get_node_job for the log. Only run plans the user asked for."""


def _fr(ch: int, fr) -> dict:
    ts, cid, flags, dlc, data = fr
    return {"ch": ch, "ts": round(ts, 6), "id": f"0x{cid:X}", "ext": bool(flags & F_EXT), "fd": bool(flags & F_FD),
            "rtr": bool(flags & F_RTR), "dir": "tx" if flags & F_TX else "rx", "dlc": dlc,
            "data": data.hex(" ").upper()}


def build_mcp(svc) -> MCPServer:
    mcp = MCPServer("canopener", instructions=INSTRUCTIONS)
    eng = svc.engine
    nodes = svc.nodes

    def run(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except (EngineError, NodeError, ValueError, KeyError) as exc:
            return {"error": str(exc)}
        except Exception as exc:                    # adapter errors etc.: report, never crash the endpoint
            return {"error": f"{type(exc).__name__}: {exc}"}

    # ------------------------------------------------------------------ read-only
    @mcp.tool()
    def get_about() -> Out:
        """CANopener version/build, app folder, loaded plugins."""
        return svc.about()

    @mcp.tool()
    def get_status() -> Out:
        """Connected channels with statistics, recording, replay, transmit list, node job."""
        return svc.status()

    @mcp.tool()
    def list_devices() -> Out:
        """CAN adapters plugged in right now (interface + channel to pass to connect_channel)."""
        return {"devices": [d.as_dict() for d in adapters.detect_devices()],
                "backends": adapters.backends_info()}

    @mcp.tool()
    def get_bus_stats() -> Out:
        """Per channel: frames/s, bus load %, peak load, error frames, rx/tx counts, bus state."""
        return {"channels": [c.stats() for c in eng.channels.values()]}

    @mcp.tool()
    def get_receive_table(channel: int = 0, limit: int = 500) -> Out:
        """Per-ID receive list of one channel: id, last data, count, cycle time (ms, last/min/max)."""
        def go():
            ch = eng.channel(channel)
            _, rows = ch.rows_snapshot()
            rows.sort(key=lambda r: (r[2] & F_EXT, r[1]))
            out = [{"id": f"0x{r[1]:X}", "ext": bool(r[2] & F_EXT), "dir": "tx" if r[2] & F_TX else "rx",
                    "dlc": r[3], "data": r[4].hex(" ").upper(), "count": r[6],
                    "cycle_ms": round(r[9] * 1000, 3), "min_ms": round(r[10] * 1000, 3),
                    "max_ms": round(r[11] * 1000, 3)} for r in rows[:limit]]
            return {"channel": channel, "rows": out, "count": len(rows)}
        return run(go)

    @mcp.tool()
    def get_trace(channel: int = 0, count: int = 200, ids: str = "") -> Out:
        """The newest frames of a channel (oldest first). ids: optional hex filter like "100-1FF,7E5"."""
        def go():
            from .server import parse_id_filter
            ch = eng.channel(channel)
            ranges = parse_id_filter(ids)
            frames = ch.trace_tail(200_000 if ranges else count)
            if ranges:
                frames = [f for f in frames if any(lo <= f[1] <= hi for lo, hi in ranges)]
            frames = frames[-count:]
            return {"channel": channel, "frames": [_fr(channel, f) for f in frames], "count": len(frames)}
        return run(go)

    @mcp.tool()
    def get_signals(prefix: str = "") -> Out:
        """Decoded signal values (DBC / CANopen / J1939 decoders): key, last value, unit, time. prefix filters keys."""
        return {"signals": svc.signals.list(prefix)}

    @mcp.tool()
    def get_signal_history(key: str, seconds: float = 30.0, max_points: int = 500) -> Out:
        """[time, value] points of one decoded signal over the last `seconds`."""
        pts = svc.signals.history(key, seconds, max_points)
        return {"key": key, "points": [[round(t, 4), v] for t, v in pts], "count": len(pts)}

    @mcp.tool()
    def list_recordings() -> Out:
        """Recorded files in the recordings folder (name, size, modified)."""
        return {"files": svc.list_recordings()}

    @mcp.tool()
    def read_recording(name: str, limit: int = 500, ids: str = "") -> Out:
        """Frames from a recorded file (.trc .asc .blf .log .csv .canbin), first `limit` after the id filter."""
        def go():
            from .core.recorder import read_frames
            from .server import parse_id_filter
            ranges = parse_id_filter(ids)
            out = []
            total = 0
            for bus, fr in read_frames(svc.resolve_file(name)):
                if ranges and not any(lo <= fr[1] <= hi for lo, hi in ranges):
                    continue
                total += 1
                if len(out) < limit:
                    out.append(_fr(bus - 1, fr))
            return {"name": name, "frames": out, "matching": total}
        return run(go)

    @mcp.tool()
    def scan_nodes(channel: int = 0, seconds: float = 2.5) -> Out:
        """Scan for CANopen nodes (listens, then SDO-reads 0x1000/0x1018/0x1008; changes nothing). Waits for it."""
        def go():
            job = nodes.wait(nodes.scan_job(channel, seconds), 60)
            return job.as_dict() if job.state != "done" else {"state": "done", **(job.result or {})}
        return run(go)

    @mcp.tool()
    def get_node_scan(channel: int = 0) -> Out:
        """The last scan of a channel: nodes with identity, name, recipe, raw ids seen."""
        return nodes.scan_dict(channel) or {"error": "no scan yet; call scan_nodes"}

    @mcp.tool()
    def get_node_job(since_line: int = 0) -> Out:
        """The current/last node operation: state, log lines, result."""
        return nodes.job.as_dict(since_line) if nodes.job else {"state": "none"}

    @mcp.tool()
    def list_recipes() -> Out:
        """Node-id / bit-rate recipes by vendor id (bundled + user)."""
        from .nodes import recipes as rm
        return {"recipes": rm.recipe_list(nodes.recipes())}

    @mcp.tool()
    def get_set_node_id_preview(old_id: int, new_id: int, channel: int = 0, method: Optional[str] = None) -> Out:
        """Plan a node-id change (needs a scan): exact frames, warnings and a token for run_node_plan."""
        return run(lambda: nodes.plan_set_id(channel, old_id, new_id, method).as_dict())

    @mcp.tool()
    def get_baud_preview(kbit: int, node: Optional[int] = None, channel: int = 0) -> Out:
        """Plan a bit-rate change (node given: that node's recipe; none: every LSS device): frames + token."""
        return run(lambda: nodes.plan_baud(channel, kbit, node).as_dict())

    @mcp.tool()
    def get_sequence_preview(steps: list[dict], vars: Optional[dict] = None, channel: int = 0,
                             title: str = "") -> Out:
        """Plan a custom node sequence (ops: sdo_write, sdo_read, store, nmt, raw, lss, delay, reopen, wait_node,
        wait_traffic; placeholders $OLD_ID $NEW_ID $BAUD ...): exact frames + token."""
        return run(lambda: nodes.plan_custom(channel, steps, vars or {}, title).as_dict())

    # ------------------------------------------------------------------ writes (approval)
    @mcp.tool()
    def connect_channel(interface: str, channel: str = "", bitrate: int = 250000, fd: bool = False,
                        data_bitrate: int = 2000000, listen_only: bool = False, index: Optional[int] = None) -> Out:
        """Open an adapter (interface: pcan, kvaser, slcan, gs_usb, vector, ixxat, gvret, obdlink, socketcan, virtual)."""
        return run(svc.connect, {"interface": interface, "channel": channel or None, "bitrate": bitrate, "fd": fd,
                                 "data_bitrate": data_bitrate, "listen_only": listen_only, "index": index})

    @mcp.tool()
    def disconnect_channel(index: int = 0) -> Out:
        """Close a channel."""
        res = run(svc.disconnect, index)
        return res if isinstance(res, dict) else {"ok": True}

    @mcp.tool()
    def send_frame(id: str, data: str = "", channel: int = 0, ext: Optional[bool] = None, fd: bool = False,
                   brs: bool = False, rtr: bool = False) -> Out:
        """Send one CAN frame now."""
        return run(lambda: {"ok": True, "ts": eng.send(channel, parse_id(id), parse_data(data), ext=ext, fd=fd,
                                                       brs=brs, rtr=rtr)})

    @mcp.tool()
    def add_transmit_entry(id: str, data: str = "", period_ms: float = 0, channel: int = 0, ext: bool = False,
                           fd: bool = False, comment: str = "", start: bool = False) -> Out:
        """Add a frame to the transmit list (period_ms > 0 and start=True: send cyclically)."""
        return run(svc.tx_add, {"can_id": id, "data": data, "period_ms": period_ms, "channel": channel, "ext": ext,
                                "fd": fd, "comment": comment, "enabled": bool(start and period_ms > 0)})

    @mcp.tool()
    def set_transmit_entry(entry_id: int, enabled: Optional[bool] = None, data: Optional[str] = None,
                           period_ms: Optional[float] = None) -> Out:
        """Start/stop a cyclic entry or change its data/period."""
        changes = {k: v for k, v in (("enabled", enabled), ("data", data), ("period_ms", period_ms)) if v is not None}
        return run(svc.tx_update, entry_id, changes)

    @mcp.tool()
    def stop_all_transmit() -> Out:
        """Stop every cyclic transmit entry."""
        eng.tx.stop_all()
        return {"ok": True}

    @mcp.tool()
    def start_recording(name: str = "", format: str = "trc") -> Out:
        """Record all connected channels to recordings/<name>.<format> (trc, asc, blf, log, csv, canbin)."""
        return run(lambda: eng.start_recording(svc.new_recording_path(name, format)).status())

    @mcp.tool()
    def stop_recording() -> Out:
        """Stop the recording."""
        return run(eng.stop_recording) or {"error": "not recording"}

    @mcp.tool()
    def start_replay(name: str, speed: float = 1.0, loop: bool = False) -> Out:
        """Replay a recorded file onto the first connected channel (transmits!)."""
        return run(lambda: eng.start_replay(svc.resolve_file(name), speed=speed, loop=loop).status())

    @mcp.tool()
    def stop_replay() -> Out:
        """Stop a replay."""
        return run(eng.stop_replay) or {"error": "no replay"}

    @mcp.tool()
    def open_recording_view(name: str) -> Out:
        """Load a recorded file into offline channels in the app (receive table + trace) for analysis."""
        return run(lambda: {"channels": [c.index for c in eng.open_file_view(svc.resolve_file(name))]})

    @mcp.tool()
    def run_node_plan(token: str, title: str) -> Out:
        """Execute a previewed node plan exactly as previewed. `title` must be the preview's title (e.g.
        "Node 5 -> 6"), so the approval shows what runs. Waits for it."""
        def go():
            job = nodes.wait(nodes.run_job(token, actor="claude", expected_title=title), 600)
            return job.as_dict()
        return run(go)

    @mcp.tool()
    def save_user_recipe(vendor_id: str, name: str, kind: str, steps: list[dict], effect: str = "immediate") -> Out:
        """Save a working sequence as the user recipe for a vendor id (kind: set_id or baud)."""
        return run(lambda: {"path": nodes.save_recipe(parse_id(vendor_id), name, kind, steps, effect)})

    register_decode_tools(mcp, svc, run)
    dash_routes.register_mcp(mcp, svc, run)        # list_dashboards, get_dashboard, save_dashboard, delete_dashboard

    for ext in svc.extensions:
        try:
            ext.mcp_tools(mcp, svc, run)
        except Exception as exc:
            svc.extension_errors.append({"plugin": ext.id, "error": f"mcp_tools: {type(exc).__name__}: {exc}"})
    return mcp
