# Changelog

What changed in each CANopener release, newest first. Versions are date codes (`YYYY.MM.DD.HHMM`).
"Unreleased" collects what is merged but not released yet; `tools/bump_version.py` turns it into the next
version's section.

## Unreleased

## 2026.10.23.2147 - 2026-10-23

### Added

- GVRET adapter: ESP32RET, Macchina M2 and other SavvyCAN-compatible boards, over USB serial or WiFi
  (`socket://<ip>:23`). Classic CAN, listen-only; `COM6@1` for the second CAN port of dual-CAN boards. Not
  tested on hardware yet.
- OBDLink adapter: OBDLink MX, MX+, LX, SX and EX OBD-II dongles (STN chip), over Bluetooth, USB or WiFi
  (`socket://<ip>:35000`). Receives with the dongle's monitor; each send pauses receiving and listens 20 ms for
  answers. Listen-only means no CAN ACKs. `COM7@ms` / `@sw` for MS-CAN and single-wire CAN. A monitor stopped
  by BUFFER FULL is restarted and counted; after a dongle reset it is set up again. Plain ELM327 clones are
  refused. Not tested on hardware yet.
- GVRET and OBDLink are marked "untested" in the adapter list until they have been on hardware.

### Fixed

- An adapter unplugged while connected is noticed (after 1.5 s of silence the channel checks it is still
  there) and reopened by itself when it is plugged back in; the channel shows an adapter error in between.
  Disconnecting an unplugged adapter no longer hangs. Checked on a Kvaser: recording, Disconnect/Connect and
  quick replugs all recover.
- The UI and `/api/status` no longer crawl (and show 0 % load) when the adapter driver is slow to report its
  bus state and load: the driver is asked once a second on its own thread, not on every UI update.

## 2026.10.18.2212 - 2026-10-18

### Added

- EDS folders and auto-assign (Decode > CANopen). **EDS folders...** sets folders of EDS / DCF / XDD files.
  **Auto-assign** gives each node without a file the one whose vendor id and product code (0x1018) fit. It
  reads them from the last scan or over SDO, and assigns only when one file clearly fits.
- **Assign EDS file...** in the right-click menu of the node rows (Decode > CANopen and the Nodes tab). It shows
  the node's identity and the files of the EDS folders, the ones that fit first, or takes any file by path,
  Browse or Upload. The Assign and Upload buttons stay as they were.
- Detail panels on the Decode tab: click a row for what it decodes, live.
  - **CANopen**: click a node for its PDOs (every mapped object with index:sub, bits, value, raw value, unit and
    range; per PDO the period, last seen and a **stopped** mark), its **SDO log** and its object dictionary. A
    node without an EDS shows the frames on the predefined PDO ids, undecoded. The **Objects...** button is gone:
    the object dictionary is in the panel.
  - **SDO log**: SDO traffic seen on the bus is decoded, from any master or from CANopener (marked by source).
    Expedited and segmented transfers are reassembled, aborts show the abort code and its meaning, block
    transfers are listed but not decoded. Values seen also fill the object dictionary's Value column, with how
    long ago and from whom. Only nodes with an EDS or a heartbeat are followed (and transfers CANopener starts).
  - **DBC**: the signal list is now a message list (period, last seen, last data); click a message for its
    signals with value and raw value.
  - **J1939**: the ECU list also shows source addresses without an address claim; click one for the PGNs it
    sends (period, last seen, data, signals from a J1939 DBC) and its transport-protocol messages.
  - Claude: `get_pdo_values`, `get_sdo_log`, `get_dbc_message_values` and `get_j1939_source`.

### Changed

- Kvaser is marked tested for sending as well as receiving, by replay at 250 kbit/s on a live machine bus, with
  the machine's controller counting frames as an independent check. Replays of 11-bit and 29-bit frames every 10 ms
  all arrived in order with none missing; replays at 500 and 1000 frames/s for a minute arrived without measurable
  loss. A listen-only recording matched the controller's own capture, and bursts of up to 64 back-to-back frames
  were recorded completely. The transmit list, CAN FD, other bit rates and whether listen-only really sends nothing
  are still not tested. The README and user guide say so.
- The README also says what decoding was checked on that live bus, by watching only (a test build of 2026-10-16,
  before this release's decoder fixes): heartbeats (NMT state and period matched the
  machine's known periods), PDOs decoded with an EDS file (matched a decode by hand) and the SDO log of the
  machine controller's expedited reads.
- The user guide notes that two programs on one Kvaser channel do not see each other's sends (seen on the
  live-bus test). To record what another program sends, record on a second adapter or channel.

### Fixed

- SDO log: a node's second SDO server can be followed. **SDO servers...** (Decode > CANopen) and Claude's
  `add_sdo_server` / `remove_sdo_server` set extra request / response COB-ID pairs per node, for example
  0x627 / 0x5A7 for node 7. The pairs are saved with the settings and in projects. Before, a tool talking to a
  node's second server was not logged at all, because those ids look like the default SDO ids of another node
  (here 39) that has no EDS or heartbeat. Nothing is followed on other ids without such a pair.
- SDO log: a reply whose request was not seen is logged when it means something alone. An expedited read reply
  was already logged with its value; a write acknowledge is now logged as "request not seen", with no value.
  This is what CANopener sees when another program sends the requests on the same Kvaser channel.
- SDO log: an object that is a single variable in the EDS (ObjectType 0x7, no subs) gets its name on sub 0 only.
  Before, every sub of it showed that name, for example a battery's 0x4900 subs 0xD2-0xD7 all as "Set
  Battery Mode". Other subs now show as unknown (index:sub) with their bytes.
- Heartbeat monitor: boot-ups are no longer counted as heartbeats. A node that only sends boot-ups (some motor
  drives) shows **boot-up only / no heartbeat** (`boot_only` for Claude) instead of a "period" taken from the
  time between boots, which hid it from the **lost** check. The gap across a node's reset no longer counts
  toward its heartbeat period either.
- Messages shown while a dialog is open (for example "Pick a file" when a dialog's input is missing) are now
  drawn on top of the dialog. Before, they appeared underneath it and could not be seen.
- CANopener opens in its own window again. Releases 2026.10.13.0202 and 2026.10.13.0225 were built without
  pywebview, so they opened in the browser instead. The build now stops if pywebview or pythonnet is missing.
- SDO read (Decode tab and the `read_sdo` tool for Claude) refuses node ids outside 1..127. Before, a negative or
  large number sent the request to another COB-ID, for example a node's RPDO or SYNC.
- A looped replay leaves one typical frame gap between passes. Before, each pass started right after the last
  frame, so a short file was sent back to back and flooded the bus. A file shorter than 10 ms is no longer looped
  (the Record & Replay tab says why).
- Stopping a replay no longer sends the next frame early. Before, Stop woke the wait for that frame and sent it
  at once.
- Disconnecting a channel stops a replay onto it. Before, a replay could carry on onto the next adapter connected
  at that channel number.
- Stop all, unticking a cyclic entry and deleting it now wait for a send already under way, and nothing is sent
  after them. Before, a frame that was due at the same moment could still go out after Stop.
- True/false settings sent to the API (transmit entries, Send, replay loop and pause, J1939 decoding, raw frames in
  custom sequences) only accept true/false, 1/0 and their text forms. Before, the text `"false"` counted as true:
  it started cyclic sending, or sent a 29-bit or remote frame.
- A cyclic transmit entry needs a cycle time of 0 (send on click) or at least 1 ms. Before, any positive value was
  accepted, and a tiny one sent the frame back to back.
- A recording name or path outside the recordings folder is refused by its text alone. Before, a `\\host\share`
  name was opened to check it, which on Windows connects to that host and hands it the login hash, and any web
  page could ask for it.
- The transmit list shows the bytes that are sent. Before, data typed as `0x11 0x22` showed as `01 10 22` (and
  `1 2 3` as `12 3`) while `11 22` (`01 02 03`) went out; Copy rows copied the wrong bytes too.
- Connect keeps your Listen-only choice when you pick an adapter that cannot open listen-only (CANable/candleLight,
  IXXAT, SocketCAN): the dialog says so, and Connect asks before opening it in normal mode, where it ACKs frames.
  Before, Listen-only was unticked without a word. The "Connect?" question after opening a project now shows
  listen-only and FD for each adapter.
- An SDO bit-rate change can no longer be stopped between the node's reset and the adapter's reopen (the LSS change
  already worked this way). Before, Stop at that point left the node at the new rate and the adapter at the old one.
- Set ID:
  - A new id that the scan found in use by another node is refused. Before it was only a warning, and the existing
    node's answer then "verified" the move.
  - Vendor and sequence recipes first read the device's identity (0x1018) at the old id and stop before any write
    if another device is there now.
  - The check at the new id needs the same identity. Before, any answer counted, and so did J1939 or free-form
    frames whose low bits happened to match the id.
  - Reconnecting the adapter makes the last scan stale, and a failed run also discards it.
- The Set ID and Bit rate previews warn when the device's recipe is picked by vendor id alone (it has no product
  code), so it would write the same objects into any product of that vendor.
- Any web page could make CANopener send on the bus: opening the EDS assign dialog read a node's identity
  (0x1018) over SDO through a GET request, which the cross-site check does not cover. It is a POST now.
- Replay speed must be a number above 0 and at most 100 (the app offers 0.5x to 10x). Before, the API and Claude
  accepted NaN, infinity or any large value, and a looped replay then sent its frames back to back. The
  shortest loop pass (10 ms) now counts the speed: a 40 ms file looped at 100x is refused.
- Editing a running cyclic transmit entry can no longer send one frame with some new and some old fields (for
  example the new id with the old data, or on the other channel).
- Stop all, Edit and Delete of transmit entries no longer hold up the app while a send is under way. A
  disconnect waits for a cyclic send under way, so it cannot go to the next adapter connected at that number.
- The API refuses true/false, fractions and out-of-range values where a number is needed (cycle time,
  channel, id, a remote frame's DLC 0..8, data bytes 0..255 given as a list). Connect parses CAN FD and
  Listen-only strictly: "false" no longer opened an adapter in CAN FD mode.
- A recipe saved with **Save as recipe...** no longer takes over the product code of the bundled recipe for
  that vendor. Before, it applied to that one model only, replacing its recipe there without the vendor-wide
  warning, and did not apply to the device it was made for.
- Bit rate with an SDO or sequence recipe first reads the node's identity (0x1018), as Set ID does, and stops
  before writing if another device answers at that id (a module swapped, or another machine since the scan).
- A node job that stops or fails after it wrote something now lists what was already sent, since a stored
  value can still take effect at the device's next reset or power cycle.
- A custom sequence that writes makes the last scan stale, so Set ID's "id already in use" check cannot rely
  on a scan from before a node moved.
- Set ID with a recipe chosen by name warns when the recipe was written for another vendor or product than the
  device reports.
- Assign EDS file... and Auto-assign work on a listen-only or disconnected channel: the files are listed and
  the identity shows why it could not be read. Before, the dialog did not open.
- A DBC loaded for one channel kept showing its messages as not seen when a second adapter on the same bus
  received the frames later; 11-bit and 29-bit ids with the same number are no longer mixed up.
- SDO log: a block download whose last data segment has sequence number 33 is no longer cut short and logged
  a second time; the first request after a block transfer that ended in silence is no longer lost.
- SDO log: the last value per object is capped (20 000 objects), and the node detail no longer writes into it.
- EDS folders stop after looking at 100 000 files and folders, so picking a whole drive cannot walk all of it.
- The build script shows its "pywebview/pythonnet missing" message in Windows PowerShell 5.1 (it stopped with
  an unrelated Python traceback instead).

## 2026.10.13.0225 - 2026-10-13

### Changed

- The README says CANopener is vibe coded: written with Claude Code, directed by its author, with commits made
  with Claude marked `Co-Authored-By: Claude`.
- Kvaser is marked tested in the adapter table, for receiving: it ran on a live 250 kbit/s machine bus (receiving,
  a node scan, CANopen decoding, about 2 hours connected). Sending, CAN FD, other bit rates and whether
  listen-only really sends nothing are not tested yet. The README and user guide say so.

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
