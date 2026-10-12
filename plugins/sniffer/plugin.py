"""Sniffer: find what the bytes of unknown frames mean, by watching which bits change and when.

- Every id's bytes and bits, with the bits that just changed lit (fading over about 10 s).
- Notch: hide everything that is changing now, then do something on the machine; only the bits that follow it
  stay lit.
- Guesses per byte (counter, 4-bit counter, checksum, constant) and candidate bit ranges, which become quick
  signals (dashboards) or DBC lines in the tab.

It only reads frames (an engine tap); it never transmits. It starts analysing the first time the tab or Claude
asks for it, so it costs nothing until then.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from fastapi import HTTPException, Query

from canopener.core.channel import F_ERR, F_RTR, F_TX, Tap
from canopener.core.transmit import parse_id
from canopener.ext import Extension

from .analysis import MAX_IDS, Sniffer

log = logging.getLogger(__name__)
WARMUP_S = 3.0                       # a notch needs this much traffic first, or everything after it looks new
MAX_RANGES = 64


def parse_ids(text: str) -> Optional[list]:
    """"0CF, 18FEF100, 700-77F" -> [(0xCF, 0xCF), (0x18FEF100, 0x18FEF100), (0x700, 0x77F)] (None = all)."""
    text = str(text or "").strip()
    if not text:
        return None
    out = []
    for part in text.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        lo, hi = parse_id(a.strip()), parse_id((b or a).strip())
        if hi < lo:
            raise ValueError(f"range {part!r}")
        out.append((lo, hi))
        if len(out) > MAX_RANGES:
            raise ValueError(f"at most {MAX_RANGES} ids or ranges")
    return out


class SnifferPlugin(Extension):
    title = "Sniffer"

    def setup(self, svc) -> None:
        self.sniffer = Sniffer()
        self.active = False                  # the tap passes nothing until someone looks
        self.dropped_at_reset = 0
        self._stop = threading.Event()
        self.tap = svc.engine.add_tap(Tap(maxlen=50_000, include_tx=False,
                                          accept=lambda cid, fl: self.active and not fl & (F_ERR | F_RTR)))
        threading.Thread(target=self._run, name="sniffer", daemon=True).start()

    def _run(self) -> None:
        last_tags = 0.0
        while not self._stop.is_set():
            try:
                first = self.tap.get(0.5)
                if first is not None:
                    batch = [first] + self.tap.drain()
                    self.sniffer.feed([(ch, f) for ch, f in batch if not f[2] & F_TX])
                now = time.time()
                if now - last_tags >= 1.0:           # guesses are refreshed here, not in the routes
                    self.sniffer.update_tags(now)
                    last_tags = now
            except Exception:                         # one bad frame must not stop the analysis for good
                log.exception("sniffer: frame analysis failed")
                time.sleep(0.5)

    def start(self) -> None:
        if not self.active:
            self.reset()
            self.active = True

    def reset(self) -> None:
        self.sniffer.reset()
        self.dropped_at_reset = self.tap.dropped

    def warming_up(self) -> Optional[str]:
        """Why a notch would be useless now (the sniffer has not seen enough traffic yet), or None."""
        self.start()
        age = time.time() - self.sniffer.started
        if age < WARMUP_S:
            return (f"the sniffer started {age:.0f} s ago: wait about {WARMUP_S - age + 2:.0f} s so it sees which bits "
                    f"change by themselves, then notch again")
        return None

    def state(self, channel: Optional[int] = None, ids: str = "", only_new: bool = False,
              hide_static: bool = False) -> dict:
        self.start()
        s = self.sniffer
        rows = s.snapshot(channel, parse_ids(ids), only_new, hide_static)
        notched_at, started = s.notched_at, s.started
        now = time.time()
        return {"ids": rows, "frames": s.frames, "dropped": self.tap.dropped - self.dropped_at_reset,
                "notched": notched_at is not None,
                "notched_s": round(now - notched_at, 1) if notched_at is not None else None,
                "running_s": round(now - started, 1),
                "capped": f"more than {MAX_IDS} ids: new ones are ignored" if s.capped else ""}

    # ------------------------------------------------------------------ REST
    def routes(self, router, svc) -> None:
        @router.get("/state")
        def state(channel: Optional[int] = None, ids: str = "", only_new: bool = False, hide_static: bool = False):
            try:
                return self.state(channel, ids, only_new, hide_static)
            except ValueError as exc:
                raise HTTPException(400, f"ids: {exc}") from None

        @router.get("/detail")
        def detail(key: str = Query(...), all_changes: bool = False):
            try:
                d = self.sniffer.detail(key, all_changes)
            except ValueError as exc:
                raise HTTPException(400, str(exc)) from None
            if d is None:
                raise HTTPException(404, "no such id (yet)")
            return d

        @router.post("/notch")
        def notch():
            why = self.warming_up()
            if why:
                raise HTTPException(409, why)
            return {"hidden_bits": self.sniffer.notch()}

        @router.post("/notch/clear")
        def clear_notch():
            self.sniffer.clear_notch()
            return {"ok": True}

        @router.post("/reset")
        def reset():
            self.reset()
            return {"ok": True}

    # ------------------------------------------------------------------ MCP
    def mcp_tools(self, mcp, svc, run) -> None:
        @mcp.tool()
        def get_sniffer_changes(only_new_since_notch: bool = True, channel: int = -1, max_ids: int = 50) -> dict:
            """Reverse engineering: the CAN ids whose bits changed (by default only bits that changed after the
            last notch), with the changed bits, byte guesses (counter, checksum, constant) and candidate signal
            ranges. Workflow: set_sniffer_notch, ask the user to do one thing on the machine, then call this."""
            def go():
                warm = self.warming_up()
                notched = self.sniffer.notched_at is not None
                only = only_new_since_notch and notched
                st = self.state(None if channel < 0 else channel, "", only, True)
                rows = sorted(st["ids"], key=lambda r: (r["age_ms"] is None, r["age_ms"] or 0))
                out = []
                for r in rows[:max(1, min(max_ids, 200))]:
                    d = self.sniffer.detail(r["key"], all_changes=not only) or {}
                    mask = int(r["new"] if only else r["ever"], 16)
                    out.append({"channel": r["channel"], "id": f"0x{r['id']:X}", "ext": r["ext"], "data": r["data"],
                                "period_ms": r["period_ms"], "changed_ms_ago": r["age_ms"], "new_id": r["new_id"],
                                "changed_bits": [b for b in range(len(r["data"]) * 4) if mask >> b & 1],
                                "byte_tags": r["tags"], "candidates": d.get("candidates", [])[:8]})
                return {"note": warm or ("" if notched or not only_new_since_notch else
                                         "no notch set: these are all changes since the sniffer started"),
                        "notched": st["notched"], "seconds_since_notch": st["notched_s"], "frames": st["frames"],
                        "ids": out}
            return run(go)

        @mcp.tool()
        def set_sniffer_notch() -> dict:
            """Hide every bit that has changed so far in the sniffer, so the next changes stand out (this only
            changes the sniffer's view, nothing is sent). Needs a few seconds of traffic first."""
            def go():
                why = self.warming_up()
                return {"error": why} if why else {"hidden_bits": self.sniffer.notch()}
            return run(go)

    def shutdown(self, svc) -> None:
        self._stop.set()
        svc.engine.remove_tap(self.tap)


EXTENSION = SnifferPlugin()
