"""Example plugin: CANopen heartbeat monitor.

Shows the whole plugin API in a small, useful feature:
- setup(): an engine Tap (every frame of every channel, never blocks the receive threads) read by our own thread;
- routes(): GET /api/x/heartbeat-monitor/nodes;
- mcp_tools(): get_heartbeat_nodes (read-only: the name starts with get_);
- ui/heartbeat.js: a tab registered with window.canopener.registerTab.
"""

from __future__ import annotations

import threading
import time

from canopener.core.channel import F_ERR, F_EXT, F_RTR, F_TX, Tap
from canopener.ext import Extension

STATES = {0: "boot-up", 4: "stopped", 5: "operational", 127: "pre-operational"}


class HeartbeatMonitor(Extension):
    title = "Heartbeat monitor"

    def setup(self, svc) -> None:
        self.nodes: dict[tuple[int, int], dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # only 11-bit 0x701..0x77F, received: the receive thread skips everything else for us
        self.tap = svc.engine.add_tap(Tap(maxlen=10_000, include_tx=False,
                                          accept=lambda cid, fl: 0x701 <= cid <= 0x77F and not fl & (F_EXT | F_ERR | F_RTR)))
        threading.Thread(target=self._run, name="heartbeat-monitor", daemon=True).start()

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self.tap.get(0.5)
            if item is None:
                continue
            ch, (ts, cid, flags, dlc, data) = item
            if flags & F_TX or not data:
                continue
            key = (ch, cid - 0x700)
            with self._lock:
                n = self.nodes.get(key)
                if n is None:
                    n = self.nodes[key] = {"channel": ch, "node": key[1], "count": 0, "period_ms": 0.0, "last": 0.0,
                                           "state": "", "boots": 0, "heartbeats": 0, "last_hb": 0.0, "_prev": 0.0}
                n["count"] += 1
                n["last"] = ts
                st = data[0] & 0x7F
                n["state"] = STATES.get(st, f"0x{st:02X}")
                if st == 0:                        # a boot-up is not a heartbeat: no period, and the gap across
                    n["boots"] += 1                # the reset does not count as one either
                    n["_prev"] = 0.0
                    continue
                if n["_prev"]:
                    p = (ts - n["_prev"]) * 1000
                    n["period_ms"] = p if not n["period_ms"] else n["period_ms"] * 0.8 + p * 0.2
                n["heartbeats"] += 1
                n["last_hb"] = n["_prev"] = ts

    def snapshot(self) -> list[dict]:
        now = time.time()
        with self._lock:
            out = []
            for n in sorted(self.nodes.values(), key=lambda n: (n["channel"], n["node"])):
                hb_age = (now - n["last_hb"]) * 1000
                lost = bool(n["period_ms"]) and hb_age > 3 * max(n["period_ms"], 50)
                out.append({**{k: v for k, v in n.items() if k[0] != "_"}, "age_ms": round((now - n["last"]) * 1000),
                            "period_ms": round(n["period_ms"], 1), "lost": lost, "boot_only": not n["heartbeats"]})
            return out

    def routes(self, router, svc) -> None:
        @router.get("/nodes")
        def nodes():
            return {"nodes": self.snapshot()}

        @router.post("/clear")
        def clear():
            with self._lock:
                self.nodes.clear()
            return {"ok": True}

    def mcp_tools(self, mcp, svc, run) -> None:
        @mcp.tool()
        def get_heartbeat_nodes() -> dict:
            """CANopen nodes seen by their heartbeat: NMT state, average period, time since the last frame, lost flag
            (no heartbeat for 3 periods). Boot-ups are counted but are not heartbeats: boot_only = boot-ups and no
            heartbeat yet (no period, never lost)."""
            return {"nodes": self.snapshot()}

    def shutdown(self, svc) -> None:
        self._stop.set()
        svc.engine.remove_tap(self.tap)


EXTENSION = HeartbeatMonitor()
