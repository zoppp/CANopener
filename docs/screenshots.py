"""Regenerate the user-guide screenshots (docs/img/*.png).

    .venv\\Scripts\\python docs\\screenshots.py [shot names]

Starts its own CANopener server (temporary app folder, port 8799) with the simulator, puts some content in it
(transmit entries, the test DBC, quick signals, a dashboard, a recording, a scan), then drives headless Microsoft
Edge over the DevTools protocol at 1440x900 in the default dark theme.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(__file__).resolve().parent / "img"
EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PORT, DEBUG_PORT = 8799, 9339
W, H = 1440, 900
BASE = f"http://127.0.0.1:{PORT}"

# name, JS run after the page has loaded (selects the tab etc.), seconds to wait before the capture
SHOTS = [
    ("receive", "CO.show('receive')", 3),
    ("trace", "CO.show('trace')", 3),
    ("connect", "void CO.openConnect()", 3),          # the dialog promise resolves only on close
    ("record", "CO.show('record')", 2),
    ("stats", "CO.show('stats')", 6),
    ("nodes", "CO.show('nodes')", 3),
    ("nodes-preview", """(async () => { CO.show('nodes'); await new Promise(r => setTimeout(r, 1500));
        document.querySelector('tr[data-node="20"] [data-act="setid"]').click();
        await new Promise(r => setTimeout(r, 800));
        document.querySelector('#modal-form button[value="ok"]').click(); })()""", 3),
    ("decode", "CO.show('decode')", 3),
    ("dashboards", "CO.show('dash')", 6),
    ("plugins", "CO.show('plugins')", 2),
]


# Local paths (user name, temp and repository folders) never go into a published image: every Windows path in
# the page becomes C:\CANopener\<last part>.
SCRUB = r"""(() => {
  const re = /[A-Za-z]:\\(?:[^\\\s"<>|]+\\)*([^\\\s"<>|]*)/g, has = /[A-Za-z]:\\/;
  const fix = (s) => s.replace(re, (m, last) => "C:\\CANopener\\" + last);
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  for (let n = w.nextNode(); n; n = w.nextNode()) if (has.test(n.nodeValue)) n.nodeValue = fix(n.nodeValue);
  document.querySelectorAll("input, [title]").forEach((e) => {
    if (e.value) e.value = fix(e.value);
    if (e.title) e.title = fix(e.title);
  });
})()"""


def api(path: str, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def seed() -> None:
    api("/api/sim/start", {"channel": "demo"})
    api("/api/connect", {"interface": "virtual", "channel": "demo", "bitrate": 250000, "index": 0})
    api("/api/tx", {"can_id": "100", "data": "A0 0F 0F 78 00 00 00 00", "period_ms": 100, "enabled": True,
                    "comment": "Engine (DBC)"})
    api("/api/tx", {"can_id": "18FF0010", "ext": True, "data": "DE AD BE EF", "period_ms": 20, "enabled": True,
                    "comment": "cyclic test frame"})
    api("/api/tx", {"can_id": "000", "data": "01 00", "comment": "NMT start all"})
    api("/api/decode/dbc/load", {"path": str(ROOT / "tests" / "data" / "test.dbc")})
    api("/api/raw_signals", {"signals": [
        {"name": "sine", "can_id": "0CF", "start_bit": 0, "length": 16},
        {"name": "node5_position", "can_id": "185", "start_bit": 0, "length": 16, "signed": True}]})
    api("/api/dashboards", {"name": "Demo", "widgets": [
        {"type": "plot", "x": 0, "y": 0, "w": 8, "h": 6, "title": "Sine and node 5 position", "window": 30,
         "series": [{"key": "raw/sine"}, {"key": "raw/node5_position"}]},
        {"type": "gauge", "x": 8, "y": 0, "w": 4, "h": 6, "title": "Sine", "key": "raw/sine", "min": 0, "max": 2000},
        {"type": "number", "x": 0, "y": 6, "w": 4, "h": 3, "title": "Engine speed", "key": "dbc/Engine/EngineSpeed"},
        {"type": "number", "x": 4, "y": 6, "w": 4, "h": 3, "title": "Gear", "key": "dbc/Engine/Gear"},
        {"type": "lamp", "x": 8, "y": 6, "w": 4, "h": 3, "title": "Position > 0", "key": "raw/node5_position",
         "threshold": 0}]})
    api("/api/record/start", {"name": "demo", "format": "trc"})
    time.sleep(2)
    api("/api/record/stop", {})
    api("/api/nodes/scan", {"channel": 0, "seconds": 1.5})
    for _ in range(100):
        if api("/api/nodes/job").get("state") != "running":
            break
        time.sleep(0.2)
    time.sleep(8)                                    # some history for the plots and statistics


async def shoot(only: set[str]) -> None:
    for _ in range(50):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{DEBUG_PORT}/json/version") as r:
                browser_ws = json.loads(r.read())["webSocketDebuggerUrl"]
            break
        except OSError:
            await asyncio.sleep(0.2)
    async with websockets.connect(browser_ws, max_size=50_000_000) as ws:
        n = 0

        async def call(method, params=None, session=None):
            nonlocal n
            n += 1
            msg = {"id": n, "method": method, "params": params or {}}
            if session:
                msg["sessionId"] = session
            await ws.send(json.dumps(msg))
            while True:
                r = json.loads(await ws.recv())
                if r.get("id") == n:
                    if "error" in r:
                        raise RuntimeError(f"{method}: {r['error']}")
                    return r.get("result", {})

        for name, js, wait in SHOTS:
            if only and name not in only:
                continue
            target = (await call("Target.createTarget", {"url": "about:blank"}))["targetId"]
            sess = (await call("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": W, "height": H, "deviceScaleFactor": 1, "mobile": False}, sess)
            await call("Page.navigate", {"url": f"{BASE}/ui/"}, sess)
            await asyncio.sleep(3)
            await call("Runtime.evaluate", {"expression": js, "awaitPromise": True}, sess)
            await asyncio.sleep(wait)
            await call("Runtime.evaluate", {"expression": SCRUB}, sess)
            png = (await call("Page.captureScreenshot", {"format": "png"}, sess))["data"]
            (OUT / f"{name}.png").write_bytes(base64.b64decode(png))
            print("saved", name)
            await call("Target.closeTarget", {"targetId": target})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("only", nargs="*", help="shot names (default: all)")
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        env = {**os.environ, "CANOPENER_HOME": str(Path(tmp) / "home"), "CANOPENER_PLUGIN_DIRS": str(ROOT / "plugins")}
        server = subprocess.Popen([sys.executable, "-m", "canopener.cli", "serve", "--port", str(PORT)], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        edge = None
        try:
            for _ in range(100):
                try:
                    api("/api/about")
                    break
                except OSError:
                    time.sleep(0.2)
            seed()
            edge = subprocess.Popen([EDGE, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                                     f"--user-data-dir={Path(tmp) / 'edge'}", f"--remote-debugging-port={DEBUG_PORT}",
                                     f"--window-size={W},{H}", "about:blank"])
            asyncio.run(shoot(set(a.only)))
        finally:
            if edge is not None:
                edge.terminate()
            server.terminate()
            time.sleep(1)


if __name__ == "__main__":
    main()
