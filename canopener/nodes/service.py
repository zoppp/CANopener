"""Node workspace service: scan, plan (preview + confirm token), run, log. Used by the UI, MCP and the CLI.

Every write to a node goes through a plan: plan_*() compiles the steps and returns the exact frames plus a
token; run(token) executes that stored plan unchanged. One node operation runs at a time, in a job thread,
and every line (plan, confirmation, each step, result) is appended to logs/nodes.log.
"""

from __future__ import annotations

import contextlib
import copy
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..core.engine import Engine, EngineError
from ..core.transmit import build_message
from ..paths import logs_dir
from . import baud as baud_mod
from . import recipes as recipes_module
from . import scan as scan_mod
from . import sequence
from . import setid
from .bridge import open_network

log = logging.getLogger(__name__)

PLAN_TTL_S = 900
_job_ids = itertools.count(1)


class NodeError(Exception):
    pass


@dataclass
class Plan:
    token: str
    kind: str                       # set_id | baud | custom
    channel: int
    title: str
    steps: list
    vars: dict
    preview: list
    warnings: list = field(default_factory=list)
    effect: str = ""
    meta: dict = field(default_factory=dict)
    created: float = field(default_factory=time.time)
    set_plan: Any = None            # setid.SetPlan for kind == set_id
    bus: dict = field(default_factory=dict)   # adapter config the plan was made for; checked before running

    def as_dict(self) -> dict:
        return {"token": self.token, "kind": self.kind, "channel": self.channel, "title": self.title,
                "steps": self.steps, "vars": self.vars, "preview": self.preview, "warnings": self.warnings,
                "effect": self.effect, "meta": self.meta,
                "frame_count": sum(len(p["frames"]) for p in self.preview)}


@dataclass
class Job:
    id: int
    kind: str
    title: str
    state: str = "running"          # running | done | failed
    lines: list = field(default_factory=list)
    result: Any = None
    error: str = ""
    started: float = field(default_factory=time.time)
    ended: Optional[float] = None
    stop_requested: bool = False

    def as_dict(self, since: int = 0) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "state": self.state,
                "lines": self.lines[since:], "line_count": len(self.lines), "result": self.result,
                "error": self.error, "started": self.started, "ended": self.ended}


class NodeService:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.scans: dict[int, scan_mod.ScanResult] = {}
        self._scan_bus: dict[int, dict] = {}       # the connection each scan was made on (see scan_for)
        self.plans: dict[str, Plan] = {}
        self.job: Optional[Job] = None
        self._lock = threading.Lock()
        # held by every node operation (jobs, scans, runs); other SDO users (decoder reads) must take it too,
        # so two SDO clients never talk to the bus at the same time
        self.bus = threading.RLock()
        self.log_path = logs_dir() / "nodes.log"

    # ------------------------------------------------------------------ helpers
    def recipes(self) -> dict:
        return recipes_module.load_recipes()

    def _log_file(self, text: str) -> None:
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}\n")
        except OSError:
            log.exception("cannot write %s", self.log_path)

    def _live_channel(self, index: int, transmit: bool = True):
        try:
            ch = self.engine.channel(index)
        except EngineError as exc:
            raise NodeError(str(exc)) from exc
        if not ch.is_open:
            raise NodeError(f"{ch.name} is not connected to an adapter")
        if transmit and ch.cfg.listen_only:
            raise NodeError(f"{ch.name} is listen-only; reconnect in normal mode to talk to nodes")
        return ch

    @contextlib.contextmanager
    def network(self, index: int, transmit: bool = True):
        ch = self._live_channel(index, transmit)
        net = open_network(ch)
        try:
            yield net, ch
        finally:
            net.disconnect()

    def _busy(self) -> None:
        if self.job is not None and self.job.state == "running":
            raise NodeError(f"'{self.job.title}' is still running")

    def start_job(self, kind: str, title: str, fn: Callable[[Callable[[str], None], Job], Any]) -> Job:
        """Run fn(log, job) in a thread. One job at a time."""
        with self._lock:
            self._busy()
            job = self.job = Job(next(_job_ids), kind, title)

        def logline(text: str) -> None:
            job.lines.append(f"{time.strftime('%H:%M:%S')} {text}")
            self._log_file(f"[job {job.id}] {text}")

        def run() -> None:
            logline(f"start: {title}")
            try:
                with self.bus:
                    job.result = fn(logline, job)
                job.state = "done"
                logline("done")
            except Exception as exc:          # reported in the job, never crashes the server
                job.error = str(exc) if isinstance(exc, (NodeError, setid.SetIdError, baud_mod.BaudChangeError,
                                                         sequence.SequenceError, EngineError)) \
                    else f"{type(exc).__name__}: {exc}"
                job.state = "failed"
                logline(f"FAILED: {job.error}")
                if not isinstance(exc, (NodeError, setid.SetIdError, baud_mod.BaudChangeError,
                                        sequence.SequenceError, EngineError)):
                    log.exception("node job %s failed", title)
            finally:
                job.ended = time.time()
                self.engine.event(f"{title}: {job.state}" + (f" - {job.error}" if job.error else ""),
                                  "info" if job.state == "done" else "warn")

        threading.Thread(target=run, name=f"node-job-{job.id}", daemon=True).start()
        return job

    def wait(self, job: Job, timeout: float = 600) -> Job:
        end = time.monotonic() + timeout
        while job.state == "running" and time.monotonic() < end:
            time.sleep(0.02)
        return job

    # ------------------------------------------------------------------ scan
    def scan(self, index: int, passive_s: float = 2.5, sdo_timeout: float = 0.03,
             log_fn: Callable = lambda s: None) -> scan_mod.ScanResult:
        ch = self._live_channel(index, transmit=False)
        active = not ch.cfg.listen_only
        with self.bus, self.network(index, transmit=False) as (net, _):
            result = scan_mod.scan(net, passive_seconds=passive_s, sdo_timeout=sdo_timeout, log=log_fn,
                                   active=active)
        self.scans[index] = result
        self._scan_bus[index] = self._bus_key(index)
        return result

    def scan_for(self, index: int) -> Optional[scan_mod.ScanResult]:
        """The last scan of this channel, or None when the adapter was reconnected or changed since: after a
        reconnect the bus (or the device at an id) may be another one, so the scan says nothing about it."""
        r = self.scans.get(index)
        try:
            same = r is not None and self._scan_bus.get(index) == self._bus_key(index)
        except EngineError:
            same = False
        if r is not None and not same:
            self.scans.pop(index, None)
        return r if same else None

    def scan_job(self, index: int, passive_s: float = 2.5, sdo_timeout: float = 0.03) -> Job:
        self._live_channel(index, transmit=False)

        def fn(logline, job):
            r = self.scan(index, passive_s, sdo_timeout, logline)
            logline(f"{len(r.nodes)} node(s), {len(r.raw_ids)} raw id(s), {len(r.raw_ext_ids)} extended id(s)")
            return self.scan_dict(index)
        return self.start_job("scan", f"Scan {self.engine.channel(index).name}", fn)

    def scan_dict(self, index: int) -> Optional[dict]:
        r = self.scan_for(index)
        if r is None:
            return None
        d = r.as_dict()
        recipes = self.recipes()
        for n in d["nodes"]:
            info = r.nodes[n["node_id"]]
            rec = recipes_module.resolve_recipe(recipes, info.vendor_id, product_code=info.product_code)
            n["recipe"] = rec.get("name", rec.get("method"))
            n["recipe_method"] = rec.get("method")
            n["recipe_note"] = rec.get("note", "")
            n["baud"] = ("sequence" if rec.get("baud_steps") else "sdo" if rec.get("baud_sdo")
                         else "lss" if rec.get("method") == "lss" else "none")
        return d

    # ------------------------------------------------------------------ plans
    def _bus_key(self, index: int) -> dict:
        ch = self.engine.channel(index)
        c = ch.cfg
        return {"serial": ch.serial, "interface": c.interface, "channel": str(c.channel), "bitrate": c.bitrate,
                "fd": c.fd, "data_bitrate": c.data_bitrate}

    def _store(self, plan: Plan) -> Plan:
        plan.steps = copy.deepcopy(plan.steps)     # the caller's list must not change what runs
        plan.vars = copy.deepcopy(plan.vars)
        plan.bus = self._bus_key(plan.channel)
        now = time.time()
        self.plans = {t: p for t, p in self.plans.items() if now - p.created < PLAN_TTL_S}
        self.plans[plan.token] = plan
        self._log_file(f"[plan {plan.token}] {plan.title} on channel {plan.channel + 1}: "
                       + " | ".join(p["text"] for p in plan.preview))
        return plan

    def plan_set_id(self, index: int, old_id: int, new_id: int, method: Optional[str] = None,
                    verify_timeout: float = 5.0) -> Plan:
        self._live_channel(index)
        scan_result = self.scan_for(index)
        if scan_result is None:
            raise NodeError("scan the bus first (the conflict check and the recipe need it; a reconnect "
                            "makes the last scan stale)")
        if old_id not in scan_result.nodes:
            raise NodeError(f"node {old_id} was not found in the last scan")
        try:
            sp = setid.plan_set_id(scan_result, old_id, new_id, self.recipes(), method, verify_timeout)
        except setid.SetIdError as exc:
            raise NodeError(str(exc)) from exc
        warnings = []
        info = scan_result.nodes.get(old_id)
        if method is None and recipes_module.vendor_wide(sp.recipe):
            warnings.append(recipes_module.vendor_wide_warning(sp.recipe, info))
        if method is not None:
            fit = recipes_module.forced_fit_warning(self.recipes(), sp.recipe, info)
            if fit:
                warnings.append(fit)
        if sp.conflict.has_conflict:
            warnings.append(str(setid.ConflictError(sp.conflict)))
        if sp.effect == "power_cycle":
            warnings.append("The new id only takes effect after a power cycle of the device.")
        pv = sequence.preview(sp.steps, sp.vars)
        token = sequence.plan_token(sp.steps, sp.vars, index)
        rec = sp.recipe
        return self._store(Plan(token, "set_id", index, f"Node {old_id} -> {new_id}", sp.steps, sp.vars, pv,
                                warnings, sp.effect,
                                {"recipe": rec.get("name", rec.get("method")), "method": rec.get("method"),
                                 "note": rec.get("note", ""), "source": rec.get("source", ""),
                                 "old_id": old_id, "new_id": new_id}, set_plan=sp))

    def plan_baud(self, index: int, new_kbit: int, node: Optional[int] = None, switch_delay_ms: int = 500,
                  verify_timeout: float = 5.0) -> Plan:
        ch = self._live_channel(index)
        if ch.cfg.interface == "socketcan":
            raise NodeError("socketcan sets the bit rate in the OS (ip link); change it there")
        warnings = []
        effect = "reopen"
        meta: dict = {"new_kbit": new_kbit, "node": node, "old_kbit": ch.cfg.bitrate // 1000}
        if node is None:
            steps, vars = baud_mod.plan_lss(new_kbit, None, switch_delay_ms, verify_timeout)
            title = f"All LSS devices -> {new_kbit} kbit/s"
            meta["method"] = "lss (global)"
            warnings.append("Every LSS device on the bus switches. Devices without LSS stay at "
                            f"{ch.cfg.bitrate // 1000} kbit/s and will not be heard after the adapter reopens.")
        else:
            scan_result = self.scan_for(index)
            info = scan_result.nodes.get(node) if scan_result else None
            if info is None:
                raise NodeError(f"node {node} was not found in the last scan; scan first")
            recipe = recipes_module.resolve_recipe(self.recipes(), info.vendor_id, product_code=info.product_code)
            meta["recipe"] = recipe.get("name", recipe.get("method"))
            if (recipe.get("baud_steps") or recipe.get("baud_sdo")) and recipe.get("product_code") is None:
                warnings.append(recipes_module.vendor_wide_warning(recipe, info))
            if recipe.get("baud_steps"):
                steps = [dict(s) for s in recipe["baud_steps"]]
                vars = {"OLD_ID": node, "NODE": node, "BAUD": new_kbit,
                        "BAUD_INDEX": sequence.BITRATE_TABLE.get(new_kbit), **recipes_module.identity_vars(info)}
                meta["method"] = "user sequence"
            elif recipe.get("baud_sdo"):
                steps, vars, effect = baud_mod.plan_sdo(node, recipe["baud_sdo"], new_kbit, verify_timeout)
                vars.update(recipes_module.identity_vars(info))
                meta["method"] = "sdo"
            if recipe.get("baud_steps") or recipe.get("baud_sdo"):
                # like Set ID: read 0x1018 at the node first, so a swapped module or another machine since the
                # scan stops the run before anything is written
                steps = setid._with_identity_checks(steps, {"method": "sdo"}, info)
                if recipe["baud_sdo"].get("note"):
                    warnings.append(recipe["baud_sdo"]["note"])
            elif recipe.get("method") == "lss":
                steps, vars = baud_mod.plan_lss(new_kbit, info, switch_delay_ms, verify_timeout)
                meta["method"] = "lss (selective)"
            else:
                raise NodeError(f"node {node} ({meta['recipe']}): no bit-rate recipe. "
                                + (recipe.get("baud_note") or recipe.get("note") or
                                   "Use the custom mode if you know the device's mechanism."))
            title = f"Node {node} -> {new_kbit} kbit/s"
            if effect == "power_cycle":
                warnings.append("The new rate takes effect after a power cycle; reconnect at the new rate then.")
            else:
                warnings.append("Other nodes on the bus stay at the old rate and will not be heard after the "
                                "adapter reopens.")
        try:
            pv = sequence.preview(steps, vars)
        except sequence.SequenceError as exc:
            raise NodeError(str(exc)) from exc
        token = sequence.plan_token(steps, vars, index)
        return self._store(Plan(token, "baud", index, title, steps, vars, pv, warnings, effect, meta))

    def plan_custom(self, index: int, steps: list[dict], vars: Optional[dict] = None, title: str = "") -> Plan:
        self._live_channel(index)
        vars = {str(k).upper().lstrip("$"): v for k, v in (vars or {}).items() if v not in (None, "")}
        vars = {k: (sequence.resolve(v, {}) if isinstance(v, str) else v) for k, v in vars.items()}
        if not steps:
            raise NodeError("the sequence has no steps")
        try:
            pv = sequence.preview(steps, vars)
        except sequence.SequenceError as exc:
            raise NodeError(str(exc)) from exc
        warnings = []
        if any(s.get("op") == "reopen" for s in steps):
            warnings.append("The adapter is reopened at another bit rate during this sequence.")
        if any(s.get("op") == "lss" and s.get("command") == "switch_global"
               and str(s.get("state", "")).startswith("config") for s in steps) and \
                not any(s.get("op") == "lss" and s.get("command") == "switch_global" and s.get("always")
                        and str(s.get("state", "waiting")) == "waiting" for s in steps):
            warnings.append("This switches LSS devices to configuration state with no 'always' step back to "
                            "waiting: a failure would leave them there.")
        scan_result = self.scan_for(index)
        if "NEW_ID" in vars and scan_result is not None and "OLD_ID" in vars:
            c = setid.check_conflict(scan_result, int(vars["OLD_ID"]), int(vars["NEW_ID"]))
            if c.has_conflict:
                warnings.append(str(setid.ConflictError(c)))
        token = sequence.plan_token(steps, vars, index)
        return self._store(Plan(token, "custom", index, title or "Custom sequence", steps, vars, pv, warnings,
                                "", {}))

    # ------------------------------------------------------------------ run
    def _context(self, net, ch, plan: Plan, logline, job: Optional[Job]) -> sequence.Context:
        def send_raw(can_id, data, ext=False, fd=False, brs=False):
            ch.send(build_message(can_id, data, ext=ext, fd=fd, brs=brs))

        def reopen(kbit: int) -> None:
            logline(f"reopening {ch.name} at {kbit} kbit/s")
            ch.reopen(bitrate=kbit * 1000 if kbit != 83 else 83_333)

        def traffic_seen(timeout: float) -> bool:
            n0 = ch.rx_frames
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if ch.rx_frames > n0:
                    logline(f"traffic seen at {ch.cfg.bitrate // 1000} kbit/s")
                    return True
                time.sleep(0.05)
            return False

        return sequence.Context(network=net, vars=plan.vars, send_raw=send_raw, reopen=reopen,
                                traffic_seen=traffic_seen, log=logline,
                                stop=(lambda: job.stop_requested) if job else None)

    def run_now(self, token: str, logline: Callable[[str], None] = lambda s: None, job: Optional[Job] = None,
                actor: str = "user") -> dict:
        outer = logline

        def logline(text: str) -> None:            # every run goes to logs/nodes.log, also from the CLI
            self._log_file(f"[run {token}] {text}")
            outer(text)

        plan = self.plans.pop(token, None)
        if plan is None or time.time() - plan.created > PLAN_TTL_S:
            raise NodeError("this preview has expired or was already run; preview again")
        if sequence.plan_token(plan.steps, plan.vars, plan.channel) != token:
            raise NodeError("the plan changed after the preview; preview again")
        try:
            now_bus = self._bus_key(plan.channel)
        except EngineError as exc:
            raise NodeError(str(exc)) from exc
        if now_bus != plan.bus:
            raise NodeError(f"channel {plan.channel + 1} was reconnected or changed since the preview; "
                            "preview again")
        logline(f"confirmed by {actor}: {plan.title} ({len(plan.steps)} steps)")
        with self.bus, self.network(plan.channel) as (net, ch):
            if plan.kind == "set_id":
                ctx = self._context(net, ch, plan, logline, job)
                try:
                    res = setid.run_plan(net, plan.set_plan, logline, send_raw=ctx.send_raw, stop=ctx.stop)
                finally:
                    self.scans.pop(plan.channel, None)       # node ids changed (maybe half way): the scan is stale
                logline(f"result: {res.message}")
                return {"ok": True, "message": res.message, "verified": res.verified,
                        "power_cycle_required": res.power_cycle_required, "old_id": res.old_id,
                        "new_id": res.new_id, "method": res.method}
            ctx = self._context(net, ch, plan, logline, job)
            result = sequence.execute(plan.steps, ctx)
        out = result.as_dict()
        if plan.kind == "custom" and any(s.get("op") in sequence.WRITE_OPS for s in plan.steps):
            self.scans.pop(plan.channel, None)       # it may have moved a node: the "id in use" check needs a scan
        if plan.kind == "baud":
            wait = result.value_of("wait_traffic")
            out["verified"] = bool(wait and wait.status == "ok")
            out["bitrate"] = ch.cfg.bitrate
            if result.ok:
                out["message"] = ("stored; power-cycle the device, then reconnect at the new rate"
                                  if plan.effect == "power_cycle" else
                                  f"verified traffic at {plan.meta['new_kbit']} kbit/s" if out["verified"]
                                  else f"could not verify traffic at {plan.meta['new_kbit']} kbit/s; check manually")
            self.scans.pop(plan.channel, None)
        logline(f"result: {'ok' if result.ok else 'FAILED - ' + result.error}"
                + (f" - {out['message']}" if out.get("message") else ""))
        if not result.ok:
            raise NodeError(result.error)
        return out

    def run_job(self, token: str, actor: str = "user", expected_title: Optional[str] = None) -> Job:
        plan = self.plans.get(token)
        if plan is None:
            raise NodeError("this preview has expired or was already run; preview again")
        if expected_title is not None and expected_title.strip() != plan.title:
            raise NodeError(f"this token is for '{plan.title}', not '{expected_title}'; preview again")
        self._busy()
        return self.start_job(plan.kind, plan.title, lambda logline, job: self.run_now(token, logline, job, actor))

    def stop_job(self) -> None:
        if self.job is not None and self.job.state == "running":
            self.job.stop_requested = True

    # ------------------------------------------------------------------ user recipes
    def save_recipe(self, vendor_id: int, name: str, kind: str, steps: list[dict], effect: str = "immediate",
                    node_id_range: Optional[list] = None, note: str = "") -> str:
        try:
            sequence.preview(steps, {"OLD_ID": 1, "NEW_ID": 2, "NODE": 1, "BAUD": 250, "BAUD_INDEX": 3,
                                     "VENDOR": vendor_id, "PRODUCT": 0, "REVISION": 0, "SERIAL": 1})
        except sequence.SequenceError as exc:
            raise NodeError(f"cannot save: {exc}") from exc
        existing = recipes_module.load_user_recipes().get("vendors", {})
        rec = next((dict(r) for k, r in existing.items() if recipes_module._int_key(k) == vendor_id), None) or {}
        if name:
            rec["name"] = name
        if kind == "baud":
            rec["baud_steps"] = steps
        else:
            rec["method"] = "sequence"
            rec["steps"] = steps
            rec["effect"] = effect
            rec["verify_on_new_id"] = effect != "power_cycle"
            if node_id_range:
                rec["node_id_range"] = node_id_range
        if note:
            rec["note"] = note
        path = recipes_module.save_user_recipe(vendor_id, rec)
        self._log_file(f"saved user recipe for vendor 0x{vendor_id:X} ({kind}) to {path}")
        return str(path)
