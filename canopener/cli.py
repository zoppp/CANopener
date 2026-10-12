"""CANopener command line (canopener-cli.exe).

  app        the app window (default when started without a command)
  serve      the server only; open the UI in a browser
  devices    list adapters that are plugged in
  scan       scan a CANopen bus
  set        change a node's node id (per-vendor recipe)
  baud       change bit rate (LSS or vendor SDO recipe)
  trace      capture to .trc/.canbin (or `trace convert in.canbin out.trc`)
  record     capture to any format (.trc .asc .blf .log .csv .canbin)
  replay     play a recorded file onto a bus
  convert    convert between recording formats
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time

import can

from . import __version__
from .core import adapters
from .core import canbin as canbin_mod
from .core.adapters import ChannelConfig
from .core.engine import Engine, EngineError

_NOOP = lambda *a, **k: None  # noqa: E731


def _make_log(verbose: bool):
    return print if verbose else _NOOP


def _cfg(args) -> ChannelConfig:
    return ChannelConfig(interface=args.interface, channel=args.channel, bitrate=args.bitrate,
                         fd=getattr(args, "fd", False), data_bitrate=getattr(args, "data_bitrate", 2_000_000),
                         listen_only=getattr(args, "listen_only", False))


def _open(args):
    from .nodes.service import NodeService
    engine = Engine()
    engine.connect(_cfg(args))
    return engine, NodeService(engine)


def _common_args(parser: argparse.ArgumentParser, listen_only: bool = False) -> None:
    parser.add_argument("--interface", default="virtual", choices=adapters.SUPPORTED_INTERFACES,
                        help="adapter backend (default: virtual)")
    parser.add_argument("--channel", default=None, help="adapter channel (backend default if omitted)")
    parser.add_argument("--bitrate", type=int, default=250_000, help="bus bit rate in bit/s (default: 250000)")
    parser.add_argument("--fd", action="store_true", help="CAN FD")
    parser.add_argument("--data-bitrate", type=int, default=2_000_000, help="CAN FD data bit rate (default 2M)")
    if listen_only:
        parser.add_argument("--listen-only", action="store_true", help="open the adapter listen-only")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every step and frame sent")


# ------------------------------------------------------------------------------------------ commands

def cmd_devices(args) -> int:
    devs = adapters.detect_devices(include_virtual=False)
    if args.json:
        print(json.dumps([d.as_dict() for d in devs], indent=2, default=str))
        return 0
    if not devs:
        print("No adapters found. (Kvaser/PCAN/Vector/IXXAT need their vendor driver installed.)")
        return 0
    for d in devs:
        print(f"{d.interface:<8} {str(d.channel):<14} {d.label}" + (f"  [{d.detail}]" if d.detail else ""))
    return 0


def cmd_scan(args) -> int:
    log = _make_log(args.verbose)
    engine, svc = _open(args)
    try:
        result = svc.scan(0, passive_s=args.seconds, sdo_timeout=args.sdo_timeout, log_fn=log)
    finally:
        engine.shutdown()

    if args.json:
        print(json.dumps(result.as_dict(), indent=2))
        return 0

    print(f"Scan complete ({result.duration_s:.1f}s passive + active ping). "
          f"{len(result.nodes)} node(s) found, {len(result.raw_ids)} raw ID(s) seen on the bus.")
    if result.pdo_only_ids:
        ids = ", ".join(str(n) for n in sorted(result.pdo_only_ids))
        print(f"  traffic without a CANopen node behind it (PDO-range ids, no SDO/heartbeat answer): {ids}")
    if not result.nodes:
        return 0
    print(f"{'ID':>4}  {'Name':<28} {'Vendor':<10} {'Seen by':<20} NMT")
    for node in sorted(result.nodes.values(), key=lambda n: n.node_id):
        vendor = f"0x{node.vendor_id:X}" if node.vendor_id is not None else "-"
        name = node.device_name or "-"
        seen = ",".join(sorted(node.seen_by)) or "-"
        nmt = str(node.nmt_state) if node.nmt_state is not None else "-"
        print(f"{node.node_id:>4}  {name:<28} {vendor:<10} {seen:<20} {nmt}")
    return 0


def _print_plan(plan) -> None:
    print(f"{plan.title}  ({plan.meta.get('recipe') or plan.meta.get('method') or plan.kind})")
    for i, p in enumerate(plan.preview, 1):
        print(f"  {i}. {p['text']}" + (f"   [{', '.join(p['flags'])}]" if p["flags"] else ""))
        for fr in p["frames"]:
            print(f"       0x{fr['id']:03X}  {fr['data']}")
    for w in plan.warnings:
        print(f"WARNING: {w}")


def _confirm(args, prompt: str) -> int | None:
    if args.yes:
        return None
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        print("error: no input available to confirm (use --yes to skip the prompt)")
        return 2
    if answer not in ("y", "yes"):
        print("Aborted.")
        return 1
    return None


def cmd_set(args) -> int:
    from .nodes.service import NodeError
    from .nodes import recipes as recipes_module
    log = _make_log(args.verbose)
    if args.from_id == args.to_id:
        print("error: --from and --to are the same node id")
        return 1
    engine, svc = _open(args)
    try:
        if args.recipes:
            fixed = recipes_module.load_recipes(args.recipes)
            svc.recipes = lambda: fixed
        print(f"Scanning bus for {args.scan_seconds:.1f}s (conflict check + identity) ...")
        svc.scan(0, passive_s=args.scan_seconds, sdo_timeout=args.sdo_timeout, log_fn=log)
        try:
            plan = svc.plan_set_id(0, args.from_id, args.to_id, args.method, args.verify_timeout)
        except NodeError as exc:
            print(f"error: {exc}")
            return 1
        _print_plan(plan)
        rc = _confirm(args, "Send this? [y/N] ")
        if rc is not None:
            return rc
        try:
            res = svc.run_now(plan.token, log)
        except NodeError as exc:
            print(f"error: {exc}")
            return 1
        print(f"node {res['old_id']} -> {res['new_id']} ({res['method']}): {res['message']}")
        return 0
    finally:
        engine.shutdown()


def cmd_baud(args) -> int:
    from .nodes.service import NodeError
    if args.interface == "socketcan":
        print("error: socketcan sets the bit rate in the OS (ip link); change it there.")
        return 2
    log = _make_log(args.verbose)
    engine, svc = _open(args)
    try:
        if args.node is not None:
            print(f"Scanning bus for {args.scan_seconds:.1f}s to fetch identity for node {args.node} ...")
            svc.scan(0, passive_s=args.scan_seconds, sdo_timeout=args.sdo_timeout, log_fn=log)
        try:
            plan = svc.plan_baud(0, args.to, args.node, args.switch_delay_ms, args.verify_timeout)
        except NodeError as exc:
            print(f"error: {exc}")
            return 2
        _print_plan(plan)
        rc = _confirm(args, "Send this? [y/N] ")
        if rc is not None:
            return rc
        try:
            res = svc.run_now(plan.token, log)
        except NodeError as exc:
            print(f"error: {exc}")
            return 2
        print(res.get("message", "done"))
        return 0
    finally:
        engine.shutdown()


def _capture(args, path: str) -> int:
    engine = Engine()
    try:
        engine.connect(_cfg(args))
        rec = engine.start_recording(path)
        print(f"Capturing for {args.seconds}s -> {path}")
        time.sleep(args.seconds)
        st = engine.stop_recording()
    finally:
        engine.shutdown()
    print(f"Wrote {st['written']} frame(s) to {path}")
    return 1 if st["error"] else 0


def cmd_trace(args) -> int:
    if getattr(args, "trace_command", None) == "convert":
        warn = lambda msg: print(f"warning: {msg}", file=sys.stderr)  # noqa: E731
        try:
            count = canbin_mod.canbin_to_trc(args.in_path, args.out_path, warn=warn)
        except canbin_mod.CanbinError as exc:
            print(f"error: {exc}")
            return 1
        print(f"Wrote {count} frame(s) to {args.out_path}")
        return 0
    if args.seconds is None or args.out is None:
        print("error: --seconds and --out are required for a live capture")
        return 2
    out = args.out
    if args.canbin and not out.lower().endswith(".canbin"):
        out += ".canbin"
    return _capture(args, out)


def cmd_record(args) -> int:
    return _capture(args, args.out)


def cmd_replay(args) -> int:
    engine = Engine()
    try:
        engine.connect(_cfg(args))
        p = engine.start_replay(args.file, speed=args.speed, loop=False, only=args.only)
        while p.state in ("playing", "paused"):
            time.sleep(0.1)
        st = p.status()
    finally:
        engine.shutdown()
    print(f"Sent {st['sent']} of {st['total']} frame(s)" + (f", {st['errors']} error(s): {st['last_error']}"
                                                          if st["errors"] else ""))
    return 0 if st["state"] == "done" and not st["errors"] else 1


def cmd_convert(args) -> int:
    from .core.recorder import _open_writer, read_frames
    from pathlib import Path
    if args.in_path.lower().endswith(".canbin") and args.out_path.lower().endswith(".trc") and not args.v21:
        return cmd_trace(argparse.Namespace(trace_command="convert", in_path=args.in_path, out_path=args.out_path))
    w = _open_writer(Path(args.out_path), 0)
    n = 0
    try:
        for bus, fr in read_frames(args.in_path):
            w.write(bus, fr)
            n += 1
    finally:
        w.close()
    print(f"Wrote {n} frame(s) to {args.out_path}")
    return 0


def cmd_app(args) -> int:
    from . import desktop
    code = desktop.run(args)
    desktop.exit_now(code)
    return code


def cmd_serve(args) -> int:
    from . import desktop
    return desktop.serve(args)


# ------------------------------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="canopener", description=f"CANopener {__version__}: CAN toolbox")
    parser.add_argument("--version", action="version", version=f"CANopener {__version__}")
    sub = parser.add_subparsers(dest="command")

    p_app = sub.add_parser("app", help="the app window (default)")
    p_app.add_argument("--port", type=int, default=8775, help="local port (default 8775)")
    p_app.set_defaults(func=cmd_app)

    p_serve = sub.add_parser("serve", help="server only; open http://127.0.0.1:<port>/ in a browser")
    p_serve.add_argument("--port", type=int, default=8775)
    p_serve.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1, local only)")
    p_serve.set_defaults(func=cmd_serve)

    p_dev = sub.add_parser("devices", help="list adapters that are plugged in")
    p_dev.add_argument("--json", action="store_true")
    p_dev.set_defaults(func=cmd_devices)

    p_scan = sub.add_parser("scan", help="scan the bus and list nodes")
    _common_args(p_scan)
    p_scan.add_argument("--seconds", type=float, default=2.5, help="passive listen time (default: 2.5)")
    p_scan.add_argument("--sdo-timeout", type=float, default=0.03, help="active SDO ping timeout (default: 0.03)")
    p_scan.add_argument("--json", action="store_true", help="print machine-readable JSON")
    p_scan.set_defaults(func=cmd_scan)

    p_set = sub.add_parser("set", help="change a node's node ID")
    _common_args(p_set)
    p_set.add_argument("--from", dest="from_id", type=int, required=True, help="current node id")
    p_set.add_argument("--to", dest="to_id", type=int, required=True, help="new node id")
    p_set.add_argument("--method", default=None, help="lss | readonly | sdo:<name> (default: auto by vendor id)")
    p_set.add_argument("--yes", action="store_true", help="don't ask before sending")
    p_set.add_argument("--recipes", default=None, help="path to a recipes.json (default: bundled + user)")
    p_set.add_argument("--scan-seconds", type=float, default=1.5, help="pre-scan passive listen time (default: 1.5)")
    p_set.add_argument("--sdo-timeout", type=float, default=0.03, help="active SDO ping timeout (default: 0.03)")
    p_set.add_argument("--verify-timeout", type=float, default=5.0, help="seconds to wait for the new id to come up")
    p_set.set_defaults(func=cmd_set)

    p_baud = sub.add_parser("baud", help="change bit rate (LSS or vendor SDO recipe)")
    _common_args(p_baud)
    p_baud.add_argument("--to", type=int, required=True, help="new bit rate in kbit/s (e.g. 250, 500, 125)")
    group = p_baud.add_mutually_exclusive_group()
    group.add_argument("--node", type=int, default=None, help="change only this node")
    group.add_argument("--global", dest="global_", action="store_true", help="change every LSS device (default)")
    p_baud.add_argument("--switch-delay-ms", type=int, default=500, help="LSS switch delay in ms (default: 500)")
    p_baud.add_argument("--scan-seconds", type=float, default=1.5, help="pre-scan time when --node is used")
    p_baud.add_argument("--sdo-timeout", type=float, default=0.03, help="active SDO ping timeout (default: 0.03)")
    p_baud.add_argument("--verify-timeout", type=float, default=5.0, help="seconds to wait for traffic at the new rate")
    p_baud.add_argument("--yes", action="store_true", help="don't ask before sending")
    p_baud.set_defaults(func=cmd_baud)

    p_trace = sub.add_parser("trace", help="capture to .trc (or .canbin), or convert a .canbin file")
    _common_args(p_trace, listen_only=True)
    p_trace.add_argument("--seconds", type=float, default=None, help="capture duration")
    p_trace.add_argument("--out", default=None, help="output file path")
    p_trace.add_argument("--canbin", action="store_true", help="write the .canbin binary format instead of .trc")
    trace_sub = p_trace.add_subparsers(dest="trace_command")
    p_convert = trace_sub.add_parser("convert", help="convert a .canbin capture to a PCAN-View .trc file")
    p_convert.add_argument("in_path", metavar="in.canbin", help="input .canbin file")
    p_convert.add_argument("out_path", metavar="out.trc", help="output .trc file")
    p_trace.set_defaults(func=cmd_trace)

    p_rec = sub.add_parser("record", help="capture to .trc .asc .blf .log .csv or .canbin")
    _common_args(p_rec, listen_only=True)
    p_rec.add_argument("--seconds", type=float, required=True)
    p_rec.add_argument("--out", required=True, help="output file; the extension picks the format")
    p_rec.set_defaults(func=cmd_record)

    p_rep = sub.add_parser("replay", help="play a recorded file onto the bus")
    _common_args(p_rep)
    p_rep.add_argument("file")
    p_rep.add_argument("--speed", type=float, default=1.0, help="1 = recorded timing, 2 = twice as fast")
    p_rep.add_argument("--only", choices=("all", "rx", "tx"), default="all", help="which recorded direction")
    p_rep.set_defaults(func=cmd_replay)

    p_conv = sub.add_parser("convert", help="convert between recording formats (by extension)")
    p_conv.add_argument("in_path")
    p_conv.add_argument("out_path")
    p_conv.add_argument("--v21", action="store_true", help=".canbin -> .trc: write v2.1 instead of v1.1")
    p_conv.set_defaults(func=cmd_convert)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:                      # double-click: the app window
        args = parser.parse_args(["app"] + list(argv or []))
    # The canopen library logs an ERROR for every SDO timeout/abort, which is routine while pinging ~126 empty
    # node ids during a scan; CANopener narrates its own steps instead.
    if args.command not in ("app", "serve"):
        logging.basicConfig(level=logging.WARNING)
    logging.getLogger("canopen").setLevel(logging.CRITICAL)
    # python-can warns about every vendor library that is not installed (Vector, IXXAT, ...) while detecting
    logging.getLogger("can").setLevel(logging.ERROR)
    try:
        return args.func(args)
    except EngineError as exc:
        print(f"error: {exc}")
        return 2
    except can.CanError as exc:
        print(f"CAN adapter error: {exc}")
        return 2
    except KeyboardInterrupt:
        print("Interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
