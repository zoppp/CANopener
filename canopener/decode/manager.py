"""The decode manager (`svc.decode`): one engine tap, one decoder thread, DBC + CANopen + J1939 decoders.

The receive threads only append to the tap; this thread drains it, looks every frame up by id (unknown ids cost
one dict lookup) and publishes values into `svc.signals`. It keeps the latest decoded text per receive-table row
for the "Decoded" column. The configuration (DBC files, EDS per node, extra SDO servers, J1939 channels) lives in
`svc.settings["decode"]` and in projects.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from pathlib import Path
from typing import Optional

from ..core.channel import F_ERR, F_EXT, F_RTR, F_TX, Tap
from ..core.transmit import parse_id
from ..nodes.service import NodeError
from ..paths import app_dir
from . import edslib
from .canopen_od import NMT_COMMANDS, SEQ_TYPES, CanopenDecoder, od_entries, pdos_from_device, value_of
from .dbc import DbcSet
from .j1939 import J1939Decoder, pgn_name, split_id

log = logging.getLogger(__name__)

TAP_SIZE = 500_000          # ~1 min of a full 1 Mbit/s bus: the decoder can fall behind briefly without loss
BATCH = 2000                # frames per lock hold: config changes and readers wait at most one batch


class DecodeError(ValueError):
    pass


def _ch(value) -> Optional[int]:
    """Channel of a config entry: None (or "", "all") means every channel."""
    return None if value in (None, "", "all") else int(value)


def _portable(path: str) -> str:
    """A file inside the app folder is saved relative to it, so the folder can be moved or its settings copied
    into a new release folder."""
    try:
        return Path(path).resolve().relative_to(app_dir().resolve()).as_posix()
    except ValueError:
        return path


def _resolve(path: str) -> str:
    p = Path(path)
    return str(p if p.is_absolute() else app_dir() / p)


class DecodeManager:
    def __init__(self, svc):
        self.svc = svc
        self.store = svc.signals
        self.dbc = DbcSet()
        self.canopen = CanopenDecoder(self.store)
        self.j1939 = J1939Decoder(self.store, self._j1939_message)
        self.j1939_channels: set[int] = set()
        self.texts: dict[str, str] = {}            # receive-table row key "ch:key" -> decoded text
        self.processed = 0
        self.errors: list[str] = []                # config entries that could not be loaded
        self._lock = threading.Lock()
        self._sdo_lock = threading.Lock()
        self.apply_config(svc.settings.get("decode") or {}, save=False)
        svc.engine.tx.encoder = self.encode_tx
        self.tap = svc.engine.add_tap(Tap(maxlen=TAP_SIZE, accept=lambda cid, fl: not fl & (F_ERR | F_RTR)))
        self.decode_errors = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="decoder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(2)
        self.svc.engine.remove_tap(self.tap)

    # ------------------------------------------------------------------ the decoder thread
    def _run(self) -> None:
        while not self._stop.is_set():
            item = self.tap.get(0.2)
            if item is None:
                continue
            batch = [item] + self.tap.drain(BATCH)
            self.process(batch)

    def process(self, batch: list) -> None:
        """Decode (channel, frame) items. Called by the decoder thread (and directly by tests)."""
        texts = self.texts
        with self._lock:
            dbc, co, j1939, j_chans = self.dbc, self.canopen, self.j1939, self.j1939_channels
            std_dbc = dbc.by_id
            pgn_dbc = bool(dbc.by_pgn)
            for ch, (ts, cid, flags, dlc, data) in batch:
                try:                               # one odd frame must not cost the rest of the batch
                    if flags & F_EXT:
                        if ch in j_chans:
                            text = j1939.handle(ch, ts, cid, data)
                        elif (cid | 0x80000000) in std_dbc:      # exact id only: no PGN matching off J1939
                            m = dbc.find(ch, cid, True, by_pgn=False)
                            text = self._dbc(m, ch, ts, cid, data) if m else None
                        else:
                            continue
                    elif cid in std_dbc:
                        m = dbc.find(ch, cid, False)
                        text = self._dbc(m, ch, ts, cid, data) if m else co.handle(ch, ts, cid, data, flags & F_TX)
                    else:
                        text = co.handle(ch, ts, cid, data, flags & F_TX)
                except Exception:
                    self.decode_errors += 1
                    if self.decode_errors in (1, 10, 100) or self.decode_errors % 10_000 == 0:
                        log.exception("decoding a frame failed (%d so far)", self.decode_errors)
                    continue
                if text:
                    texts[f"{ch}:{cid | ((flags & (F_EXT | F_RTR | F_TX)) << 29)}"] = text
            self.processed += len(batch)

    def _dbc(self, m, ch: int, ts: float, cid: int, data: bytes, prefix: str = "") -> Optional[str]:
        """Decode with a DBC message and publish: dbc/<Message>/<Signal>, J1939 messages j1939/<SA>/<PGN>.<Signal>."""
        values = m.decode(data)
        if not values:
            return None
        if not prefix:
            prefix = f"j1939/{cid & 0xFF}/{m.pgn}." if m.j1939 else f"dbc/{m.name}/"
        publish, meta = self.store.publish, m.meta
        for k, v in values.items():
            unit, lo, hi, choices = meta[k]
            publish(prefix + k, ts, v, unit=unit, minimum=lo, maximum=hi, choices=choices, channel=ch, can_id=cid)
        return m.text(values)

    def _j1939_message(self, ch: int, ts: float, can_id: Optional[int], sa: int, pgn: int,
                       data: bytes) -> Optional[str]:
        """A complete J1939 message (a frame, or reassembled with can_id None): decode it through the DBC."""
        if can_id is not None:
            m = self.dbc.find(ch, can_id, True)
            return self._dbc(m, ch, ts, can_id, data) if m else None
        m = self.dbc.find_pgn(ch, pgn)
        return self._dbc(m, ch, ts, m.msg.frame_id, data, f"j1939/{sa}/{pgn}.") if m else None

    # ------------------------------------------------------------------ configuration
    def config(self, portable: bool = False) -> dict:
        """portable=True for settings and projects: paths inside the app folder are made relative to it."""
        fix = _portable if portable else str
        return {"dbc": [{"path": fix(f["path"]), "channel": f["channel"]} for f in self.dbc.files],
                "eds": [{"node": n.node, "path": fix(n.path), "channel": n.channel}
                        for n in self.canopen.nodes.values()],
                "sdo_servers": [dict(s) for s in self.canopen.sdo_servers],
                "j1939": sorted(self.j1939_channels)}

    def _save(self) -> None:
        self.svc.settings["decode"] = self.config(portable=True)
        self.svc.save_settings()

    def apply_config(self, cfg: dict, save: bool = True) -> dict:
        """Replace the whole configuration (settings at start-up, a project). Missing files are reported, not fatal."""
        errors = []
        with self._lock:
            self.dbc = DbcSet()
            self.canopen = CanopenDecoder(self.store)
            self.j1939.clear()
            self.j1939_channels = {int(c) for c in cfg.get("j1939", [])}
            for d in cfg.get("dbc", []):
                try:
                    self.dbc.load(_resolve(d["path"]), _ch(d.get("channel")))
                except (ValueError, KeyError) as exc:
                    errors.append(f"DBC {d.get('path')}: {exc}")
            for e in cfg.get("eds", []):
                try:
                    self.canopen.assign(int(e["node"]), _resolve(e["path"]), _ch(e.get("channel")))
                except (ValueError, KeyError) as exc:
                    errors.append(f"EDS node {e.get('node')}: {exc}")
            for e in cfg.get("sdo_servers", []):
                try:
                    self.canopen.add_sdo_server(int(e["node"]), parse_id(e["request"]), parse_id(e["response"]),
                                                _ch(e.get("channel")))
                except (ValueError, KeyError) as exc:
                    errors.append(f"SDO server of node {e.get('node')}: {exc}")
            self.texts.clear()
        self.errors = errors
        for msg in errors:
            self.svc.engine.event(f"decode: {msg}", "warn")
        if save:
            self._save()
        return {"errors": errors, **self.config()}

    def load_dbc(self, path: str, channel=None) -> dict:
        with self._lock:
            try:
                info = self.dbc.load(str(path), _ch(channel))
            except ValueError as exc:
                raise DecodeError(str(exc)) from exc
        self._save()
        return info

    def remove_dbc(self, path: str, channel=None) -> None:
        with self._lock:
            self.dbc.remove(str(path), _ch(channel))
            self.texts.clear()
        self._save()

    def assign_eds(self, node: int, path: str, channel=None) -> dict:
        with self._lock:
            try:
                n = self.canopen.assign(int(node), str(path), _ch(channel))
            except ValueError as exc:
                raise DecodeError(str(exc)) from exc
        self._save()
        return n.info()

    def remove_eds(self, node: int, channel=None) -> None:
        with self._lock:
            self.canopen.remove(int(node), _ch(channel))
        self._save()

    # ------------------------------------------------------------------ EDS folders: find and match files
    def eds_folders(self) -> list[str]:
        return edslib.clean_folders(self.svc.settings.get("eds_folders", []))

    def set_eds_folders(self, folders: list) -> dict:
        """The folders searched for EDS/DCF/XDD files (an app setting, not part of a project)."""
        self.svc.settings["eds_folders"] = edslib.clean_folders(folders)
        self.svc.save_settings()
        return self.eds_library()

    def eds_library(self) -> dict:
        files, problems = edslib.scan_folders(self.eds_folders())
        return {"folders": self.eds_folders(), "files": files, "problems": problems}

    def node_identity(self, channel: int, node: int) -> dict:
        """Vendor id, product code and revision of a node: from the last scan of the channel, else read over SDO
        (0x1018 sub 1..3, reads only)."""
        if not 1 <= int(node) <= 127:
            raise DecodeError(f"node id {node} out of range 1..127")
        scan = self.svc.nodes.scan_for(int(channel))
        info = scan.nodes.get(int(node)) if scan else None
        if info is not None and info.vendor_id is not None:
            return {"vendor": info.vendor_id, "product": info.product_code, "revision": info.revision_number,
                    "source": "last scan"}
        out = {"source": "read from the node (SDO 0x1018)"}
        with self._sdo_lock, self._network(channel) as (net, _):
            remote = net.add_node(int(node))
            try:
                for sub, key in ((1, "vendor"), (2, "product"), (3, "revision")):
                    try:
                        out[key] = int.from_bytes(remote.sdo.upload(0x1018, sub)[:4], "little")
                    except Exception as exc:
                        if sub < 3:                # the revision is optional; vendor + product are needed
                            raise DecodeError(f"node {node} did not answer the identity read "
                                              f"(0x1018:{sub}): {exc}") from exc
                        out[key] = None
            finally:
                del net[int(node)]
        return out

    def eds_candidates(self, node: int, channel: int) -> dict:
        """The files of the EDS folders for the assign dialog of a node, best fits first, each with its "match"."""
        lib = self.eds_library()
        try:
            dev = self.node_identity(channel, node)
        except (DecodeError, ValueError, NodeError) as exc:   # not connected, listen-only, no answer: list the files
            dev = {"error": str(exc)}
        best, _ = edslib.best_match(lib["files"], dev) if "error" not in dev else (None, [])
        rank = {"exact": 0, "product": 1, "vendor": 2, "": 3}
        for f in lib["files"]:
            f["match"] = edslib.match_kind(f, dev)
        lib["files"].sort(key=lambda f: (rank[f["match"]], f["name"].lower()))
        return {**lib, "node": int(node), "channel": int(channel), "identity": dev,
                "best": best["path"] if best else None}

    def auto_assign_eds(self) -> dict:
        """For every node without an EDS (heard by heartbeat or found by the last scan): read its identity and
        assign the file of the EDS folders that fits it. Nodes that already have an EDS are left as they are."""
        lib = self.eds_library()
        with self._lock:
            seen = set(self.canopen.nmt)
        for ch in list(self.svc.nodes.scans):
            r = self.svc.nodes.scan_for(ch)
            seen |= {(ch, n) for n in (r.nodes if r else {})}
        results = []
        for ch, node in sorted(seen, key=lambda k: (k[1], k[0])):
            res = {"node": node, "channel": ch}
            results.append(res)
            have = self._node_eds(node, ch)
            if have is not None:
                res.update(result="has EDS", path=have.path)
                continue
            try:
                dev = self.node_identity(ch, node)
            except (DecodeError, ValueError, NodeError) as exc:
                res.update(result="no identity", note=str(exc))
                continue
            res["identity"] = dev
            best, fits = edslib.best_match(lib["files"], dev)
            if best is None:
                res.update(result="several files fit" if fits else "no matching file",
                           files=[f["path"] for f in fits])
                continue
            try:
                self.assign_eds(node, best["path"], ch)
            except DecodeError as exc:
                res.update(result="failed", path=best["path"], note=str(exc))
                continue
            res.update(result="assigned", path=best["path"], match=edslib.match_kind(best, dev))
        return {"results": results, "folders": lib["folders"], "files": len(lib["files"]),
                "problems": lib["problems"]}

    def add_sdo_server(self, node: int, request, response, channel=None) -> dict:
        """Follow a node's SDO transfers on another COB-ID pair too (ids as int or hex text)."""
        with self._lock:
            try:
                s = self.canopen.add_sdo_server(int(node), parse_id(request), parse_id(response), _ch(channel))
            except ValueError as exc:
                raise DecodeError(str(exc)) from exc
        self._save()
        return s

    def remove_sdo_server(self, node: int, request, channel=None) -> None:
        with self._lock:
            self.canopen.remove_sdo_server(int(node), parse_id(request), _ch(channel))
        self._save()

    def set_j1939(self, channel: int, enabled: bool) -> list[int]:
        with self._lock:
            (self.j1939_channels.add if enabled else self.j1939_channels.discard)(int(channel))
            self.texts.clear()
        self._save()
        return sorted(self.j1939_channels)

    # ------------------------------------------------------------------ transmit by signal value
    def encode_tx(self, entry) -> None:
        """TxScheduler hook: fill can_id/ext/data of a transmit entry from its DBC message and signal values."""
        with self._lock:
            entry.can_id, entry.ext, data = self.dbc.encode(entry.dbc_message, entry.signals)
        entry.data = data.hex(" ").upper()

    def encode(self, message: str, signals: dict) -> dict:
        with self._lock:
            can_id, ext, data = self.dbc.encode(message, signals or {})
        return {"id": can_id, "ext": ext, "data": data.hex(" ").upper()}

    # ------------------------------------------------------------------ readers
    def text_map(self) -> dict:
        return dict(self.texts)

    def dbc_info(self) -> dict:
        with self._lock:
            return {"files": [{"path": f["path"], "channel": f["channel"], "messages": len(f["messages"])}
                              for f in self.dbc.files], "messages": self.dbc.messages_info()}

    def canopen_info(self) -> dict:
        with self._lock:
            return {"nodes": self.canopen.nodes_info(), "nmt": self.canopen.nmt_list(),
                    "sdo_servers": [dict(s) for s in self.canopen.sdo_servers], "emcy": list(self.canopen.emcy)[-200:]}

    def j1939_info(self) -> dict:
        with self._lock:
            out = {"channels": sorted(self.j1939_channels), "ecus": self.j1939.ecu_list(),
                   "messages": list(self.j1939.messages), "dm1": self.j1939.dm1_list()}
        return {**out, "sources": self.j1939_sources()}

    def clear_logs(self, what: str = "") -> None:
        """Clear the EMCY log and J1939 TP messages, or with what="sdo" only the SDO log."""
        with self._lock:
            if what == "sdo":
                self.canopen.sdo.log.clear()
                return
            self.canopen.emcy.clear()
            self.j1939.messages.clear()

    # ------------------------------------------------------------------ detail views (receive-table rows)
    def _rows(self, channels, keep) -> list[tuple]:
        """(channel, key, data, t_last, cycle, count) of the receive-table rows of `channels` (None: all)."""
        out = []
        for idx, c in list(self.svc.engine.channels.items()):
            if channels is None or idx in channels:
                out += [(idx, k, *v) for k, v in c.rows_where(keep).items()]
        return out

    @staticmethod
    def _timing(row, now: float) -> dict:
        ch, key, data, t_last, cycle, count = row
        return {"channel": ch, "can_id": key & 0x1FFFFFFF, "tx": bool(key >> 29 & F_TX), "data": data.hex(" ").upper(),
                "age": round(now - t_last, 3), "period_ms": round(cycle * 1000, 3) if count > 1 else None,
                "count": count}

    @staticmethod
    def _latest(rows) -> dict:
        """The most recent row per CAN id (Rx or Tx, any channel)."""
        best: dict[int, tuple] = {}
        for r in rows:
            cid = r[1] & 0x1FFFFFFF
            if cid not in best or r[3] > best[cid][3]:
                best[cid] = r
        return best

    def canopen_node(self, node: int, channel=None) -> dict:
        """One node for the detail view: its PDOs with every mapped object's latest value (from the last frame of
        each PDO), the SDO transfers seen and the last value per object. channel None: every channel. A node
        without an EDS gets its frames on the predefined PDO ids, undecoded."""
        node, chs = int(node), _ch(channel)
        with self._lock:
            co = self.canopen
            n = co.node_for(node, chs) if chs is not None else \
                co.nodes.get((None, node)) or next((x for (c, nn), x in co.nodes.items() if nn == node), None)
            pdos = list(n.pdos) if n is not None else []
            sdo = [e for e in co.sdo.log if e["node"] == node and (chs is None or e["channel"] == chs)]
            values = {}
            for (c, nn, index, sub), v in co.sdo.values.items():
                k = f"{index}:{sub}"
                if nn == node and (chs is None or c == chs) and (k not in values or v["ts"] > values[k]["ts"]):
                    values[k] = v
        if n is not None:
            slots = [(p.name, p.cob_id, p.channel, p) for p in pdos]
            source = n.source
        else:
            slots = [(f"{kind}{i + 1}", base + 0x100 * i + node, chs, None)
                     for kind, base in (("TPDO", 0x180), ("RPDO", 0x200)) for i in range(4)]
            source = "predefined ids"
        cobs = {cob for _, cob, _, _ in slots}
        rows = self._rows(None if chs is None else {chs}, lambda k: k & 0x7FF in cobs and not k >> 29 & (F_EXT | F_RTR))
        now = time.time()
        out_pdos = []
        for name, cob, pch, p in slots:
            best = self._latest(r for r in rows if r[1] & 0x7FF == cob and (pch is None or r[0] == pch)).get(cob)
            if p is None and best is None:
                continue                           # no EDS: only the predefined ids that were seen
            e = {"name": name, "kind": name[:4], "cob_id": cob, "source": source, "channel": None, "data": "",
                 "age": None, "period_ms": None, "count": 0}
            if best is not None:
                e.update(self._timing(best, now))
            with self._lock:
                e["fields"] = p.fields(best[2] if best is not None else None) if p is not None else []
            out_pdos.append(e)
        # copies: the dicts belong to the decoder's shared store (another reader may be serialising them)
        values = {k: {**v, "age": round(now - v["ts"], 3)} for k, v in values.items()}
        return {"node": node, "channel": chs, "eds": n.path if n is not None else "", "pdos": out_pdos,
                "sdo": sdo[::-1][:500], "values": values}

    def dbc_live(self) -> dict:
        """Last seen, period and data of every DBC message (J1939 messages: any source address)."""
        rows = self._rows(None, lambda k: not k >> 29 & F_RTR)
        newest: dict = {}

        def newest_on(ch):
            """The newest row per (id, 29-bit) and per J1939 PGN of one channel (None: any): a message of a
            channel-bound DBC must not vanish because another adapter on the same bus saw the frame later."""
            if ch not in newest:
                by_id: dict[tuple, tuple] = {}
                by_pgn: dict[int, tuple] = {}
                for r in rows:
                    if ch is not None and r[0] != ch:
                        continue
                    cid, ext = r[1] & 0x1FFFFFFF, bool(r[1] >> 29 & F_EXT)
                    if (cid, ext) not in by_id or r[3] > by_id[(cid, ext)][3]:
                        by_id[(cid, ext)] = r
                    if ext:
                        pgn = split_id(cid)[1]
                        if pgn not in by_pgn or r[3] > by_pgn[pgn][3]:
                            by_pgn[pgn] = r
                newest[ch] = (by_id, by_pgn)
            return newest[ch]

        now = time.time()
        out = {}
        with self._lock:
            msgs = [m for f in self.dbc.files for m in f["messages"]]
        for m in msgs:
            by_id, by_pgn = newest_on(m.channel)
            r = by_pgn.get(m.pgn) if m.j1939 else by_id.get((m.msg.frame_id, m.msg.is_extended_frame))
            if r is not None:
                out[m.name] = self._timing(r, now)
        return {"messages": out}

    def dbc_message(self, name: str) -> dict:
        """One DBC message for the detail view: every signal decoded from the last frame (raw and scaled)."""
        with self._lock:
            try:
                m = self.dbc.message(name)
            except Exception as exc:
                raise DecodeError(str(exc)) from exc
            info = m.info()
        live = self.dbc_live()["messages"].get(name)
        values = m.decode(bytes.fromhex(live["data"])) if live else {}
        for s, sig in zip(info["signals"], m.msg.signals):
            v = values.get(s["name"])
            s["value"] = v
            s["raw"] = None if v is None else round((v - sig.offset) / sig.scale)
            s["text"] = s["choices"].get(str(s["raw"]), "") if v is not None else ""
        return {**info, "live": live}

    def j1939_source(self, sa: int, channel=None) -> dict:
        """One source address for the detail view: the PGNs it sends (last frame of each, decoded by a J1939 DBC
        when one covers the PGN), its address claim and its reassembled TP messages."""
        sa, chs = int(sa), _ch(channel)
        with self._lock:
            j_chans = set(self.j1939_channels) if chs is None else {chs} & self.j1939_channels
            ecu = next((e for (c, s), e in self.j1939.ecus.items() if s == sa and c in j_chans), None)
            tp = [m for m in self.j1939.messages if m["sa"] == sa and m["channel"] in j_chans][::-1]
        rows = self._rows(j_chans, lambda k: k >> 29 & F_EXT and not k >> 29 & F_RTR and k & 0xFF == sa)
        by_pgn: dict[int, tuple] = {}
        for r in rows:
            pgn = split_id(r[1] & 0x1FFFFFFF)[1]
            if pgn not in by_pgn or r[3] > by_pgn[pgn][3]:
                by_pgn[pgn] = r
        now = time.time()
        pgns = []
        for pgn, r in sorted(by_pgn.items()):
            cid = r[1] & 0x1FFFFFFF
            prio, _, da, _ = split_id(cid)
            e = {"pgn": pgn, "name": pgn_name(pgn), "priority": prio, "da": da, **self._timing(r, now),
                 "message": "", "signals": []}
            with self._lock:
                m = self.dbc.find(r[0], cid, True)
                if m is not None:
                    e["message"] = m.name
                    for k, v in m.decode(r[2]).items():
                        unit, _, _, choices = m.meta[k]
                        e["signals"].append({"name": k, "value": v, "unit": unit,
                                             "text": choices.get(int(v), "") if float(v).is_integer() else ""})
            pgns.append(e)
        return {"sa": sa, "channel": chs, "ecu": ecu, "pgns": pgns, "tp": tp}

    def j1939_sources(self) -> list[dict]:
        """Every source address seen on the J1939 channels: [{channel, sa, frames, age}]."""
        with self._lock:
            j_chans = set(self.j1939_channels)
        seen: dict[tuple[int, int], list] = {}
        for r in self._rows(j_chans, lambda k: k >> 29 & F_EXT):
            s = seen.setdefault((r[0], r[1] & 0xFF), [0, 0.0])
            s[0] += r[5]
            s[1] = max(s[1], r[3])
        now = time.time()
        return [{"channel": c, "sa": sa, "frames": n, "age": round(now - t, 3)} for (c, sa), (n, t) in sorted(seen.items())]

    def status(self) -> dict:
        return {"processed": self.processed, "dropped": self.tap.dropped, "waiting": len(self.tap.buf),
                "errors": self.errors}

    # ------------------------------------------------------------------ CANopen: SDO browser and plans
    def _node_eds(self, node: int, channel: int):
        with self._lock:
            return self.canopen.node_for(int(node), int(channel))

    def object_dictionary(self, node: int, channel: int = 0) -> list[dict]:
        n = self._node_eds(node, channel)
        if n is None:
            raise DecodeError(f"no EDS/XDD assigned to node {node}")
        return od_entries(n.od)

    @contextlib.contextmanager
    def _network(self, channel: int):
        """A canopen network for SDO reads, holding the node service's bus lock (never beside a node job)."""
        nodes = self.svc.nodes
        if not nodes.bus.acquire(timeout=0.3):
            job = nodes.job
            raise DecodeError(f"'{job.title if job else 'a node operation'}' is using the bus; try again when it "
                              "is done")
        try:
            with nodes.network(int(channel)) as pair:
                yield pair
        finally:
            nodes.bus.release()

    def sdo_read(self, channel: int, node: int, index: int, sub: int = 0) -> dict:
        """SDO upload (changes nothing on the node); decoded with the node's EDS when it has the object."""
        # canopen puts any number on 0x600 + node: outside 1..127 that is another node's PDO, SYNC or EMCY
        if not 1 <= int(node) <= 127:
            raise DecodeError(f"node id {node} out of range 1..127")
        if not (0 <= int(index) <= 0xFFFF and 0 <= int(sub) <= 0xFF):
            raise DecodeError(f"object 0x{int(index):X}:{int(sub)} out of range")
        n = self._node_eds(node, channel)
        var = n.od.get_variable(index, sub) if n is not None else None
        with self._sdo_lock, self._network(channel) as (net, _):
            remote = net.add_node(int(node))
            try:
                data = remote.sdo.upload(int(index), int(sub))
            except Exception as exc:
                raise DecodeError(f"SDO read node {node} 0x{index:04X}:{sub:02X} failed: {exc}") from exc
            finally:
                del net[int(node)]
        value = value_of(var, data)
        text = var.value_descriptions.get(value, "") if var is not None and isinstance(value, int) else ""
        return {"node": node, "index": index, "sub": sub, "name": var.qualname if var is not None else "",
                "raw": data.hex(" ").upper(), "value": value, "text": text}

    def read_pdo_mapping(self, channel: int, node: int) -> dict:
        """Replace the EDS default PDO mapping of a node with the mapping read from the device (SDO uploads)."""
        n = self._node_eds(node, channel)
        if n is None:
            raise DecodeError(f"no EDS/XDD assigned to node {node}")
        with self._sdo_lock, self._network(channel) as (net, _):
            try:
                pdos = pdos_from_device(net, n.od, n.node, n.channel)
            except Exception as exc:
                raise DecodeError(f"reading the PDO mapping of node {node} failed: {exc}") from exc
        with self._lock:
            self.canopen.set_pdos(n, pdos, "device")
        return n.info()

    def plan_sdo_write(self, channel: int, node: int, index: int, sub: int, value, type_: str = "") -> dict:
        """A node plan with one SDO write: the UI/Claude previews the frames, then runs it with the token."""
        n = self._node_eds(node, channel)
        var = n.od.get_variable(int(index), int(sub)) if n is not None else None
        if not type_:
            if var is None:
                raise DecodeError(f"0x{index:04X}:{sub:02X} is not in the EDS of node {node}; give the type")
            type_ = SEQ_TYPES.get(var.data_type, "bytes")
        if var is not None and var.access_type in ("ro", "const"):
            raise DecodeError(f"{var.qualname} is read-only ({var.access_type})")
        name = f" ({var.qualname})" if var is not None else ""
        step = {"op": "sdo_write", "node": int(node), "index": f"0x{int(index):04X}", "sub": int(sub),
                "type": type_, "value": value}
        title = f"Write node {node} 0x{int(index):04X}:{int(sub):02X}{name} = {value}"
        return self.svc.nodes.plan_custom(int(channel), [step], {}, title).as_dict()

    def plan_nmt(self, channel: int, node: int, command: str) -> dict:
        if command not in NMT_COMMANDS:
            raise DecodeError(f"unknown NMT command {command!r}; use one of {', '.join(NMT_COMMANDS)}")
        step = {"op": "nmt", "command": command, "node": int(node)}
        title = f"NMT {command} -> {'all nodes' if int(node) == 0 else f'node {node}'}"
        return self.svc.nodes.plan_custom(int(channel), [step], {}, title).as_dict()
