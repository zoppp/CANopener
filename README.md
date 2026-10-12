# CANopener

An open-source CAN engineering toolbox: everything you use PCAN-View for, for many adapters, plus CANopen node
commissioning, plugins and an MCP endpoint for Claude.

- **Receive / Transmit**: one row per ID (data, count, cycle time, changed bytes highlighted); transmit list with
  single and cyclic frames.
- **Trace**: every frame, virtual scrolling, pause, server-side filter; keeps up with a full 1 Mbit/s bus.
- **Record & Replay**: `.trc` (PCAN-View), `.blf`/`.asc` (Vector), `.log` (candump), `.csv`, `.canbin`;
  offline file view; replay with original timing.
- **Statistics**: bus load, frame rate, error frames, busiest IDs.
- **CANopen nodes**: scan, change node ID and bit rate with LSS or your own recipes, and a custom mode for
  unknown devices (SDO steps, raw frames, raw LSS). Every change is previewed as exact frames and confirmed first,
  and logged.
- **Decoding**: DBC (decode, and transmit by signal value), CANopen EDS/DCF/XDD (PDOs, SDO browser, NMT, EMCY,
  heartbeats), J1939 (PGN/SA, address claim, transport protocol, DM1); a Decoded column in the receive list.
- **Dashboards**: time and XY plots, gauges, numbers and lamps from any decoded signal, or from quick signals
  defined on raw frames without a DBC.
- **Adapters**: PEAK PCAN, Kvaser, CANable (slcan and candleLight/gs_usb, libusb bundled), Vector, IXXAT,
  SocketCAN, virtual; CAN FD where the backend supports it; several channels at once.
- **Plugins**: tabs, REST routes, MCP tools and live frame consumers ([docs/PLUGINS.md](docs/PLUGINS.md)).
- **Claude**: MCP at `http://127.0.0.1:8775/mcp/`; read tools (and scans, which only send SDO reads) can be allowlisted; changes to nodes and your own frames stay on approval.

![Receive / Transmit](docs/img/receive.png)

| Exact-frame preview before any node change | Dashboards |
|---|---|
| ![Set ID preview](docs/img/nodes-preview.png) | ![Dashboards](docs/img/dashboards.png) |
| **CANopen decoding: object dictionary, EMCY** | **J1939: address claims, DM1, transport protocol** |
| ![CANopen decoding](docs/img/decode-canopen.png) | ![J1939 decoding](docs/img/decode-j1939.png) |

**[User guide](docs/user-guide.md)**: getting started, adapters, every tab, and walkthroughs (find the ID that
floods a bus, record a fault, give a CANopen node a new ID, commission an unknown device, decode with a DBC,
build a dashboard, use Claude). The release zip has it as `docs\user-guide.html` for offline reading, and the
app opens it from the About box.

## Download

Windows: download `canopener-<version>-win64.zip` from the Releases page, unzip, double-click `canopener.exe`.
Windows SmartScreen may warn the first time (the exe is not signed): **More info**, **Run anyway**. No adapter
at hand? Connect the **Virtual** interface with simulated devices ticked.

## From source

```
git clone https://github.com/zoppp/canopener
cd canopener
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[app,gs_usb,decode,dev]"
.venv\Scripts\python -m canopener.cli app        # the app window
.venv\Scripts\python -m canopener.cli serve      # or: server only, open http://127.0.0.1:8775/
.venv\Scripts\python -m pytest -q                # tests (virtual bus, no hardware needed)
```

Build the Windows release: `powershell -ExecutionPolicy Bypass -File packaging\build.ps1`.

Docs: `docs\screenshots.py` regenerates the screenshots from the simulator (headless Edge), and
`docs\build_guide.py` turns `docs/user-guide.md` and `docs/PLUGINS.md` into the offline HTML pages the release
ships.

## How it is built

- One process: a FastAPI server with a vanilla-JS UI (no build step), shown in a pywebview window; the same server
  serves MCP.
- `canopener/core`: adapters, one receive thread per channel (per-ID table, lock-free trace ring, bus statistics),
  transmit scheduler, recorder with its own writer thread, replay.
- `canopener/nodes`: CANopen scan, recipes, and the sequence engine that turns every node change into explicit,
  previewable steps.
- `canopener/decode`: DBC, CANopen and J1939 decoders publishing into one signal store.
- `canopener/dash`: dashboards and quick signals.
- `canopener/ui`: one script per workspace; plugins use the same API.

## Licence

MIT, see [LICENSE](LICENSE).
