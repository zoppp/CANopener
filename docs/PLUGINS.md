# Writing CANopener plugins

A plugin is a folder with a `manifest.json` and a Python module. It runs inside CANopener with full access, so
treat it as code: review it like any other change. `plugins/heartbeat-monitor/` is a complete example (about
100 lines) that uses every part of the API.

A plugin can add:

- **live frame consumers**: an engine *tap* receives every frame of every channel without ever slowing the
  receive threads down;
- **REST routes** under `/api/x/<id>/`;
- **MCP tools** for Claude;
- **UI tabs** (JavaScript, loaded by the page).

## Where plugins are loaded from

At start-up, in this order (the first plugin with a given id wins):

1. `CANOPENER_PLUGIN_DIRS` (folders separated by `;` on Windows);
2. `plugins\` next to `canopener.exe` (from source: the repository's `plugins/`);
3. the plugins shipped inside the release.

There is no hot reload: restart CANopener after adding or changing a plugin. The **Plugins** tab lists what
loaded and what failed (with the error). A broken plugin never stops CANopener.

## Folder layout

```
my-plugin/
  manifest.json        {"id": "my-plugin", "title": "...", "version": "1.0", "description": "...", "module": "plugin.py"}
  plugin.py
  ui/                  optional; every .js file here is loaded (alphabetical) and served at /ui/x/my-plugin/
    my-plugin.js
```

`id` is lowercase letters, digits and dashes. It appears in URLs, so never change it.

## The module

```python
from canopener.core.channel import F_EXT, F_TX, Tap
from canopener.ext import Extension

class MyPlugin(Extension):
    title = "My plugin"

    def setup(self, svc):                  # once, after the app service exists
        self.tap = svc.engine.add_tap(Tap(maxlen=10_000, accept=lambda can_id, flags: can_id == 0x123))
        ...                                # start your own thread that calls self.tap.get(timeout) / drain()

    def routes(self, router, svc):         # FastAPI APIRouter, mounted at /api/x/<id>/
        @router.get("/state")
        def state():
            return {...}

    def mcp_tools(self, mcp, svc, run):
        @mcp.tool()
        def get_my_state() -> dict:        # get_/list_/read_ names: read-only, may be allowlisted
            """One sentence for Claude."""
            return {...}

    def shutdown(self, svc):               # stop your threads; svc.engine.remove_tap(self.tap)
        ...

EXTENSION = MyPlugin()
```

### Frames

A frame is a tuple `(ts, can_id, flags, dlc, data)`: `ts` is wall-clock seconds (`time.time()` base), `data`
is `bytes`, `flags` is a bit set from `canopener.core.channel`: `F_EXT` (29-bit id), `F_RTR`, `F_ERR` (error
frame), `F_FD`, `F_BRS`, `F_ESI`, `F_TX` (sent by CANopener). An engine tap yields `(channel_index, frame)`.

**The tap rules.** `Tap(maxlen, include_tx=True, accept=None)`:

- `accept(can_id, flags) -> bool` runs on the receive thread for every frame. Keep it to a comparison or two.
- The receive thread never waits for you. When your tap is full, the oldest frame is dropped and
  `tap.dropped` counts it (the Statistics tab shows dropped plugin frames).
- Do the work on your own thread: `tap.get(timeout)` returns one frame or `None`; `tap.drain()` returns all
  waiting frames.

### What to use from `svc`

| | |
|---|---|
| `svc.engine.channels` | `{index: Channel}`; `ch.stats()`, `ch.rows_snapshot()` (receive table), `ch.trace_tail(n)` |
| `svc.engine.send(index, can_id, data, ext=None, fd=False, brs=False, rtr=False)` | send one frame (also shows in the trace) |
| `svc.engine.tx` | the transmit list (`add`, `update`, `send_once`, `stop_all`) |
| `svc.engine.start_recording(path)` / `stop_recording()` | recording |
| `svc.engine.event(text, level)` | a line in the app's event log (and a toast for `warn`) |
| `svc.nodes` | the CANopen node service: `scan(index)`, `plan_custom(index, steps, vars)` -> `Plan`, `run_now(token)` |
| `svc.signals` | decoded values: `publish(key, ts, value, unit=...)` from your own thread, `list()`, `history(key, seconds)`. Keys are `<source>/<group>/<name>`; use your plugin id as the source. Dashboards can plot anything you publish. |
| `svc.decode` | the DBC / CANopen / J1939 decoders (`svc.decode.config()`) |

Anything that writes to a CANopen node must go through a plan (`svc.nodes.plan_*`), so the user sees the
frames first. Do not send node-changing frames from a plugin behind the user's back.

## UI scripts

Scripts in `ui/` run after the page has started and use `window.canopener` (also `CO`):

| | |
|---|---|
| `registerTab({id, title, order, render(el), onShow(el), onHide(el)})` | a top tab; `render` fills `el` once |
| `onProject(fn)` | a project was opened (reload what your tab shows) |
| `onFeed(fn)` | every 100 ms feed message: `{channels, rows, trace, events, ...}`; `{type: "open"/"closed"}` on (re)connect |
| `api(path, body?, method?)`, `tryApi(...)` | REST calls (`tryApi` shows errors as a toast) |
| `modal({title, body, buttons})`, `confirm(...)`, `toast(msg, kind)` | dialogs and messages |
| `esc`, `idText(id, flags)`, `typeText(flags)`, `fmtMs`, `fmtNum`, `chName(index)`, `F` (flags) | helpers |
| `state.channels`, `state.signals`, `receiveRows`, `traceBuffer()` | live state (`state.signals[key]` = latest decoded value) |

Escape everything you put into HTML with `esc()`. Use the existing CSS classes (`page`, `card`, `tbl`, `btn`,
`notice`, `badge`, `row`) and CSS variables, so the dark and light themes both work.

## MCP tool rules

- The name says whether it changes anything: `get_`, `list_`, `read_` (and `scan_`) only read. Any other verb transmits,
  writes or changes state and stays on approval, so the user sees each call.
- Never put a dry-run flag on a write tool (approval is per tool name): make a separate `get_..._preview` tool.
- Tool names are global across plugins: make them specific.
