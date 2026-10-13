# Changelog

What changed in each CANopener release, newest first. Versions are date codes (`YYYY.MM.DD.HHMM`).
"Unreleased" collects what is merged but not released yet; `tools/bump_version.py` turns it into the next
version's section.

## Unreleased

### Changed

- The README says CANopener is vibe coded: written with Claude Code, directed by its author, with commits made
  with Claude marked `Co-Authored-By: Claude`.

## 2026.10.13.0202 - 2026-10-13

### Fixed

- DBC and EDS files inside the CANopener folder (such as `decode\`) are saved in `settings.json` and projects
  relative to that folder, so moving the folder, or copying its settings into a new release folder, keeps the
  decoders working. Files elsewhere keep their full path. Settings saved by older versions still load.
- A scan on a listen-only channel no longer says PDO-range traffic has "no CANopen node behind it": it never
  asked. It now says those ids had no heartbeat and were not asked (they may be nodes whose PDOs use other ids).
- Closing the window no longer writes a harmless `ConnectionResetError [WinError 10054]` traceback to
  `logs\canopener.log`.

## 2026.10.12.1352 - 2026-10-12

### Added

- **EDS editor** (a plugin that ships with CANopener), a new tab:
  - creates EDS files from CiA 301, CiA 401 (generic I/O) and CiA 402 (drive) templates;
  - opens EDS, DCF and XDD/XDC files and saves EDS or DCF, keeping keys and sections it does not know;
  - an object tree with forms, a sub-index table and a problem list that checks values, data and access types,
    sub-index 0, PDO mappings (length, mappable, size, access), COB-ID clashes and CAN ids that CiA 301 keeps for
    other services;
  - a **PDO editor**: every RPDO and TPDO decoded (COB-ID flags, transmission type, inhibit time, event timer,
    SYNC start), the frame layout of each mapping, entries shown decoded and encoded, add and delete PDOs;
  - **Compare with node** and **Read PDOs from node** read a node over SDO (reads only) and can take its values
    into the file;
  - **Create from node** builds an EDS from what a node answers (reads only); guessed data types are listed for
    checking;
  - **To Nodes** turns DCF values into Nodes-tab steps (PDOs changed the CiA 301 way), which you preview and run
    there;
  - MCP tools for Claude: `get_eds_problems`, `list_eds_objects`, `get_eds_pdos`, `read_node_vs_eds`
    (read only) and `create_eds_from_node`.
- **Sniffer** (a plugin that ships with CANopener), a new tab for finding what unknown frames mean: every id's
  bits, lit when they change; **Notch** hides what is changing now, so the bits that follow one action on the
  machine stand out; guesses per byte (counter, 4-bit counter, checksum, constant); candidate bit ranges that
  become quick signals or DBC lines. It only reads frames. MCP tools: `get_sniffer_changes` (read only) and
  `set_sniffer_notch`.
- Plugins can be split into several modules (`from . import helpers`), and `CO.openCustomSequence` lets a
  plugin hand steps to the Nodes tab.
- This changelog; the release zip includes it, and each GitHub release shows its section.
- **Selecting, copying and right-click menus**: rows of the receive list, transmit list, trace, recordings,
  busiest IDs and heartbeats are selected with click, Shift+click, Ctrl+click or a mouse drag (Ctrl+A, Esc);
  the selection stays while the lists update; Ctrl+C copies the rows as tab-separated text with a header line.
  Right-click menus in the receive list, transmit list, trace, recordings, statistics, nodes, decode tables,
  dashboards, EDS editor, sniffer, event log, channel chips and heartbeats (copy rows / IDs / data / .trc lines,
  show or hide an ID in the trace, add a frame to the transmit list, send / edit / duplicate / delete transmit
  entries, copy node identities, values and paths). **Paste frames** (Ctrl+V) turns pasted "ID data" lines into manual transmit entries after a
  confirmation, without sending anything. Live views no longer redraw over text you are selecting. Plugins get
  `CO.contextMenu`, `CO.rowSelect` and `CO.copyText`.

### Changed

- The simulator's demo objects (0x2100, 0x2101, 0x2110) read back a value, like a real device.
- Docs: CANopener has not been tested with real adapters or devices yet, and the README and user guide now say
  so. The adapter table has a **Tested** column (all "Not yet"). Claims about hardware, throughput on a real bus
  and other tools (PCAN-View, Vector, candump) are marked untested or removed. Two descriptions that did not match
  the code were corrected: DCF writing skips values it cannot parse but not out-of-range numbers, and plugin ids
  also accept other letters and underscores.
- Docs: a "More adapters (help wanted)" section lists planned support (ESP32 with GVRET firmware, slcan over
  WiFi, ELM327/OBDLink dongles) that is not available or tested yet.

### Tests

- About 100 new tests on the virtual bus and simulator back the guide's software claims: closing stops all
  sending, listen-only refuses every transmit, scans and the EDS editor only read, MCP read tools and previews
  send nothing, replay speeds and filters, the trace buffer and live-view cap, broken plugins, the
  hardware-timestamp offset, and more.

## 2026.10.11.2110 - 2026-10-11

### Added

- A full user guide with screenshots (getting started, adapters, every tab, walkthroughs, Claude, command line,
  troubleshooting, FAQ). The release zip has it as `docs\user-guide.html`, and the About box opens it.
- The simulator also sends J1939 address claims, DM1 fault codes (one by BAM transport protocol) and CANopen
  emergency messages, so the decoding walkthroughs work without hardware.

### Changed

- Versions are date codes (`YYYY.MM.DD.HHMM`) instead of 0.x numbers.

## 0.2.1 - 2026-10-11

First public release.

- Receive / Transmit, Trace, Record & Replay (`.trc`, `.blf`, `.asc`, `.log`, `.csv`, `.canbin`), Statistics.
- CANopen nodes: scan, set node id and bit rate (LSS or recipes), custom sequences for unknown devices; every
  change is previewed as exact frames and confirmed first.
- Decoding: DBC (also transmit by signal value), CANopen EDS/DCF/XDD, J1939; dashboards and quick signals.
- Adapter backends through python-can: PEAK, Kvaser, CANable (slcan, candleLight), Vector, IXXAT, SocketCAN,
  virtual (not yet tested on real hardware).
- Plugins, and an MCP endpoint for Claude.
- A simulator with demo CANopen devices, for trying everything without hardware.
