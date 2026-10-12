"""Regenerate the user-guide screenshots (docs/img/*.png).

    .venv\\Scripts\\python docs\\screenshots.py [shot names]

Starts its own CANopener server (temporary app folder, port 8799) with the simulator, puts some content in it
(transmit entries incl. one by DBC signal value, the test DBC, an EDS on node 5, J1939 decoding with address
claims, DM1 and a BAM transfer, two EMCY messages, quick signals, a dashboard, a recording, a project, a scan),
then drives headless Microsoft Edge over the DevTools protocol at 1440x900 in the default dark theme. Every shot
starts from a fresh page with the UI's stored preferences cleared. Afterwards run docs\\build_guide.py.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import shutil
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
SHOWN = "C:" + chr(92) + "CANopener"         # how the app folder appears in the images

# name, JS run after the page has loaded (the body of an async function with sleep(ms); open a dialog with
# `void`, its promise resolves only on close), seconds to wait before the capture, optional JS run after it
SHOTS = [
    ("receive", "CO.show('receive')", 3),
    ("connect", "void CO.openConnect()", 3),
    ("tx-dbc", """CO.show('receive'); await sleep(800);
        const e = (await CO.api('/api/tx')).entries.find((x) => x.dbc_message === 'Engine');
        void CO.editTx(e);""", 2),
    ("trace", """CO.show('trace'); await sleep(300);
        const ids = document.querySelector('#tr-ids'); ids.value = '0CF, 18FEF100';
        document.querySelector('#tr-ex').checked = true;
        ids.dispatchEvent(new Event('input'));""", 4),
    ("record", """CO.show('record'); await sleep(800);
        document.querySelector('#rec-name').value = 'fault-hunt';
        document.querySelector('#rec-start').click();""", 4, "await CO.api('/api/record/stop', {})"),
    ("replay", """CO.show('record'); await sleep(800);
        document.querySelector('tr[data-name="demo.trc"] [data-act=replay]').click();""", 2),
    ("file-view", """CO.show('record'); await sleep(800);
        document.querySelector('tr[data-name="demo.trc"] [data-act=open]').click(); await sleep(2500);
        const file = CO.state.channels.find((c) => c.state === 'file'), ch = document.querySelector('#rx-ch');
        ch.value = String(file.index); ch.dispatchEvent(new Event('change'));""", 2,
     "for (const c of CO.state.channels) if (c.state === 'file') await CO.api('/api/disconnect', {index: c.index})"),
    ("stats", "CO.show('stats')", 6),
    ("nodes", "CO.show('nodes')", 3),
    ("nodes-preview", """CO.show('nodes'); await sleep(1500);
        document.querySelector('tr[data-node="20"] [data-act="setid"]').click(); await sleep(800);
        document.querySelector('#modal-form button[value="ok"]').click();""", 3),
    ("nodes-preview-sdo", """CO.show('nodes'); await sleep(1500);
        document.querySelector('tr[data-node="5"] [data-act="setid"]').click(); await sleep(800);
        document.querySelector('#modal-form button[value="ok"]').click();""", 3),
    ("nodes-custom", """CO.show('nodes'); await sleep(1500);
        const t = document.querySelector('#cs-tpl'); t.value = 'sdo-id'; t.dispatchEvent(new Event('change'));
        await sleep(300);
        for (const [k, v] of [['OLD_ID', '5'], ['NEW_ID', '6']]) {
          const i = document.querySelector(`[data-var="${k}"]`); i.value = v;
          i.dispatchEvent(new Event('input', {bubbles: true})); }
        document.querySelector('#cs').scrollIntoView();""", 2),
    ("decode-dbc", "CO.show('decode'); await sleep(300); document.querySelector('[data-sub=dbc]').click();", 3),
    ("decode-canopen", """CO.show('decode'); await sleep(300); document.querySelector('[data-sub=canopen]').click();
        await sleep(1500);
        document.querySelector('#dec-body tr[data-node="5"] [data-act=od]').click(); await sleep(1000);
        for (const k of ['4096:0', '4120:1', '4120:2', '4104:0']) {
          document.querySelector(`#od-rows tr[data-k="${k}"] [data-sdo=read]`).click(); await sleep(700); }
        const q = document.querySelector('#od-filter'); q.value = '0x10'; q.dispatchEvent(new Event('input'));""", 2),
    ("decode-j1939", "CO.show('decode'); await sleep(300); document.querySelector('[data-sub=j1939]').click();", 3),
    ("dashboards", "CO.show('dash')", 6),
    ("dash-edit", "CO.show('dash'); await sleep(1500); document.querySelector('#db-edit').click();", 4),
    ("dash-widget", """CO.show('dash'); await sleep(1500); document.querySelector('#db-edit').click(); await sleep(800);
        document.querySelectorAll('.dash-w')[1].querySelector('[data-act=cfg]').click();""", 2),
    ("quick-signals", "CO.show('dash'); await sleep(1000); document.querySelector('#db-quick').click();", 3),
    ("projects", "document.querySelector('#btn-project').click();", 2),
    ("plugins", "CO.show('plugins')", 2),
    ("about", "document.querySelector('#btn-about').click();", 2),
    ("light-theme", "document.querySelector('#btn-theme').click(); CO.show('dash');", 6),
]


# Local paths (user name, temp and repository folders) never go into a published image: the app folder and the
# repository show as C:\CANopener (the rest of each path is kept), any other Windows path as C:\CANopener\<last
# part>, and the screenshot server's port as the default 8775 - now and after every later change of the page (the
# feed re-renders some views ten times a second).
SCRUB = r"""(() => {
  const prefixes = __PREFIXES__;          // [real folder, shown as] pairs
  const re = /[A-Za-z]:\\(?:[^\\\s"<>|]+\\)*([^\\\s"<>|]*)/g, has = /[A-Za-z]:\\|127\.0\.0\.1:\d+/;
  const generic = (m, last) => m.startsWith("C:\\CANopener") ? m : "C:\\CANopener\\" + last;
  const fix = (s) => {
    for (const [real, shown] of prefixes) s = s.split(real).join(shown);
    return s.replace(re, generic).replace(/127\.0\.0\.1:\d+/g, "127.0.0.1:8775");
  };
  const scrub = () => {
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = w.nextNode(); n; n = w.nextNode()) {
      if (has.test(n.nodeValue)) { const v = fix(n.nodeValue); if (v !== n.nodeValue) n.nodeValue = v; }
    }
    document.querySelectorAll("input, [title]").forEach((e) => {
      if (e.value && has.test(e.value)) { const v = fix(e.value); if (v !== e.value) e.value = v; }
      if (e.title && has.test(e.title)) { const v = fix(e.title); if (v !== e.title) e.title = v; }
    });
  };
  scrub();
  if (!window._scrubbing) {
    window._scrubbing = new MutationObserver(scrub);
    window._scrubbing.observe(document.body, { subtree: true, childList: true, characterData: true, attributes: true,
                                               attributeFilter: ["title"] });
  }
})()"""


PREFIXES: list = []                       # set by main(): [real folder, shown as] pairs
HOME = Path(".")                           # set by main(): the temporary app folder


def scrub_js() -> str:
    return SCRUB.replace("__PREFIXES__", json.dumps(PREFIXES))


def api(path: str, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def seed() -> None:
    api("/api/sim/start", {"channel": "demo"})
    api("/api/connect", {"interface": "virtual", "channel": "demo", "bitrate": 250000, "index": 0})
    api("/api/decode/dbc/load", {"path": str(HOME / "decode" / "test.dbc")})
    api("/api/decode/canopen/eds", {"node": 5, "path": str(HOME / "decode" / "test_node.eds")})
    api("/api/decode/j1939", {"channel": 0, "enabled": True})
    api("/api/tx", {"dbc_message": "Engine", "signals": {"EngineSpeed": 1500, "Gear": 1, "CoolantTemp": 85},
                    "period_ms": 100, "enabled": True, "comment": "Engine (DBC signals)"})
    api("/api/tx", {"can_id": "18FF0010", "ext": True, "data": "DE AD BE EF", "period_ms": 20, "enabled": True,
                    "comment": "cyclic test frame"})
    api("/api/tx", {"can_id": "000", "data": "01 00", "comment": "NMT start all"})
    api("/api/raw_signals", {"signals": [
        {"name": "sine", "can_id": "0CF", "start_bit": 0, "length": 16},
        {"name": "node5_position", "can_id": "185", "start_bit": 0, "length": 16, "signed": True}]})
    api("/api/dashboards", {"name": "Demo", "widgets": [
        {"type": "plot", "x": 0, "y": 0, "w": 8, "h": 6, "title": "Sine and node 5 position", "window": 30,
         "series": [{"key": "raw/sine"}, {"key": "raw/node5_position"}]},
        {"type": "gauge", "x": 8, "y": 0, "w": 4, "h": 6, "title": "Sine", "key": "raw/sine", "min": 0, "max": 2000,
         "thresholds": [{"value": 1500, "color": "#d29922"}, {"value": 1750, "color": "#e11d48"}]},
        {"type": "number", "x": 0, "y": 6, "w": 4, "h": 3, "title": "Engine speed", "key": "dbc/Engine/EngineSpeed"},
        {"type": "number", "x": 4, "y": 6, "w": 4, "h": 3, "title": "Gear", "key": "dbc/Engine/Gear"},
        {"type": "lamp", "x": 8, "y": 6, "w": 4, "h": 3, "title": "Position > 0", "key": "raw/node5_position",
         "threshold": 0}]})
    time.sleep(4)                                    # the simulator's J1939 claims, DM1s and emergencies arrive
    api("/api/record/start", {"name": "demo", "format": "trc"})
    time.sleep(2)
    api("/api/record/stop", {})
    api("/api/nodes/scan", {"channel": 0, "seconds": 1.5})
    for _ in range(100):
        if api("/api/nodes/job").get("state") != "running":
            break
        time.sleep(0.2)
    api("/api/projects/save", {"name": "Demo rig"})
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

        async def run_js(js, sess):
            wrapped = f"(async () => {{ const sleep = (ms) => new Promise((r) => setTimeout(r, ms)); {js} }})()"
            r = await call("Runtime.evaluate", {"expression": wrapped, "awaitPromise": True}, sess)
            if "exceptionDetails" in r:
                ex = r["exceptionDetails"]
                raise RuntimeError(ex.get("exception", {}).get("description") or ex.get("text"))

        for name, js, wait, *after in SHOTS:
            if only and name not in only:
                continue
            target = (await call("Target.createTarget", {"url": "about:blank"}))["targetId"]
            sess = (await call("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
            await call("Emulation.setDeviceMetricsOverride",
                       {"width": W, "height": H, "deviceScaleFactor": 1, "mobile": False}, sess)
            await call("Page.navigate", {"url": f"{BASE}/api/about"}, sess)        # same origin: forget UI prefs
            await asyncio.sleep(0.5)
            await call("Runtime.evaluate", {"expression": "localStorage.clear()"}, sess)
            await call("Page.navigate", {"url": f"{BASE}/ui/"}, sess)
            await asyncio.sleep(3)
            await call("Runtime.evaluate", {"expression": scrub_js()}, sess)
            await run_js(js, sess)
            await asyncio.sleep(wait)
            await call("Runtime.evaluate", {"expression": scrub_js()}, sess)
            png = (await call("Page.captureScreenshot", {"format": "png"}, sess))["data"]
            (OUT / f"{name}.png").write_bytes(base64.b64decode(png))
            print("saved", name)
            if after:
                await run_js(after[0], sess)
            await call("Target.closeTarget", {"targetId": target})


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("only", nargs="*", help="shot names (default: all)")
    a = ap.parse_args()
    OUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        global HOME
        home = HOME = Path(tmp) / "CANopener"
        # the repository plays the part of a release's _internal folder (bundled plugins and files)
        PREFIXES[:] = [[str(home), SHOWN], [str(ROOT), SHOWN + chr(92) + "_internal"]]
        (home / "decode").mkdir(parents=True)
        for f in ("test.dbc", "test_node.eds"):                       # decode files as a user keeps them
            shutil.copy(ROOT / "tests" / "data" / f, home / "decode" / f)
        env = {**os.environ, "CANOPENER_HOME": str(home)}   # plugins: the bundled ones (ROOT/plugins)
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
