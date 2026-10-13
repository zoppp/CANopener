"""The decode manager (`svc.decode`): one engine tap, one decoder thread, DBC + CANopen + J1939 decoders.

The receive threads only append to the tap; this thread drains it, looks every frame up by id (unknown ids cost
one dict lookup) and publishes values into `svc.signals`. It keeps the latest decoded text per receive-table row
for the "Decoded" column. The configuration (DBC files, EDS per node, J1939 channels) lives in
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
from ..paths import app_dir
from .canopen_od import NMT_COMMANDS, SEQ_TYPES, CanopenDecoder, od_entries, pdos_from_device, value_of
from .dbc import DbcSet
from .j1939 import J1939Decoder

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
                        text = self._dbc(m, ch, ts, cid, data) if m else co.handle(ch, ts, cid, data)
                    else:
                        text = co.handle(ch, ts, cid, data)
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
                    "emcy": list(self.canopen.emcy)[-200:]}

    def j1939_info(self) -> dict:
        with self._lock:
            return {"channels": sorted(self.j1939_channels), "ecus": self.j1939.ecu_list(),
                    "messages": list(self.j1939.messages), "dm1": self.j1939.dm1_list()}

    def clear_logs(self) -> None:
        with self._lock:
            self.canopen.emcy.clear()
            self.j1939.messages.clear()

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
