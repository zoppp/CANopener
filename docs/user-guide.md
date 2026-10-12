# CANopener user guide

CANopener is an open-source CAN engineering toolbox for Windows (and, from source, Linux). It does what
PCAN-View does (receive list, transmit list, trace, record and replay, bus statistics) for many adapters, and
adds CANopen node commissioning, DBC / CANopen / J1939 decoding, dashboards, plugins and an MCP endpoint, so
Claude can read the bus and, with your approval, act on it.

Everything in this guide can be tried without hardware: CANopener has a built-in simulator with three made-up
CANopen devices and some J1939 traffic. All screenshots were taken with it (plus a DBC file, an EDS file and a
few transmit entries).

![The Receive / Transmit tab with the simulator connected](img/receive.png)

## Contents

1. [Getting started](#getting-started)
2. [Connecting adapters](#connecting-adapters)
3. [The screen](#the-screen)
4. [Receive / Transmit](#receive--transmit)
5. [Trace](#trace)
6. [Record & Replay](#record--replay)
7. [Statistics](#statistics)
8. [Nodes (CANopen)](#nodes-canopen)
9. [Decode: DBC, CANopen, J1939](#decode-dbc-canopen-j1939)
10. [Dashboards](#dashboards)
11. [Projects and settings](#projects-and-settings)
12. [Task walkthroughs](#task-walkthroughs)
13. [Claude (MCP)](#claude-mcp)
14. [Plugins](#plugins)
15. [Command line](#command-line)
16. [Files and folders](#files-and-folders)
17. [Troubleshooting](#troubleshooting)
18. [FAQ](#faq)

## Getting started

### Download and start

1. Download `canopener-<version>-win64.zip` from the project's Releases page.
2. Unzip the whole folder somewhere you can write to, for example Documents or the Desktop. Not into
   `Program Files`: CANopener keeps its recordings, logs and settings next to the exe.
3. Double-click `canopener.exe`. The app opens in its own window.

The first time, Windows SmartScreen may say "Windows protected your PC", because the exe is not signed. Click
**More info**, then **Run anyway**. This happens once per downloaded version.

CANopener runs as a small local server with the window on top. Double-clicking `canopener.exe` again opens a
second window onto the same running CANopener, so both windows show the same bus. There is only ever one
CANopener per PC and port.

The window needs the Microsoft Edge WebView2 runtime, which Windows 10 and 11 already have. Without it,
CANopener opens in your web browser instead.

### Your first connection

1. Click **+ Connect** in the top bar (or press **Ctrl+K**).
2. Pick your adapter from **Detected adapters**. This fills in the interface and channel.
3. Choose the **Bit rate** of your bus. Tick **Listen-only** if you only want to watch a running machine.
4. Click **Connect**.

The adapter shows as a chip (**CAN1**) in the top bar, and frames start to appear in the receive list. The
status bar at the bottom shows the bus load and frames per second.

### Try it without hardware

1. Click **+ Connect**.
2. Choose the interface **Virtual (no hardware)**.
3. Leave **Start simulated devices on this virtual bus** ticked.
4. Click **Connect**.

The simulator puts these made-up devices on the virtual bus:

| Node | Name | What it does |
|---|---|---|
| 5 | Demo device A (SDO) | Answers SDO, sends heartbeats and a TPDO. Its node id is set with object 0x2100:00 and applied by a store (0x1010:01) plus an NMT reset. Its bit-rate recipe writes 0x2101:00; the simulator accepts it but keeps running (a virtual bus has no bit rate). CANopener has a recipe for it. |
| 10 | Demo device B (SDO, power cycle) | Like A, node id in 0x2110:01, but the new id only takes effect after a power cycle. The simulator cannot be switched off: it applies the stored id at the next NMT reset instead (Decode > CANopen > **Reset**). CANopener has a recipe for it. |
| 20 | Demo LSS device | Changes its node id and bit rate over LSS (CiA 305). |

There is also a fast free-running frame (0x0CF, 100 times a second), J1939 traffic (two ECUs claiming addresses,
DM1 trouble codes from both, one of them by BAM transport protocol every 30 s) and, every minute, an emergency
message from nodes 5 and 10. The
devices really change their node id when you commission them, so you can practise every node workflow safely.

The virtual bus exists only inside CANopener. It is not visible to other programs.

### Closing

Closing the first CANopener window stops every cyclic transmit and a running replay, then finishes a running
recording, saves the settings and closes the adapters: nothing keeps sending after CANopener has closed. A second
window (started while CANopener runs) only looks at the same CANopener; it closes when the first one does.

Without the WebView2 runtime CANopener opens in your browser instead; closing the browser does not stop it. Click
**OK** in the "CANopener is running in your browser" message to stop it.

## Connecting adapters

![The Connect dialog](img/connect.png)

The **Connect an adapter** dialog:

- **Detected adapters** lists what is plugged in right now. Picking an entry fills in the interface and
  channel. **Refresh** searches again (for example after plugging an adapter in).
- **Interface**: the adapter type. Choose it by hand if your adapter is not detected. The line under the
  dialog says what that interface needs (for example a driver).
- **Channel**: the adapter's channel. Leave it empty for the default, shown as the placeholder.
- **Bit rate**: the nominal bit rate, 10 kbit/s to 1 Mbit/s. A rate the adapter's driver cannot do is refused
  with a message that lists the rates it can do. It is never silently replaced by another rate.
- **CAN FD** and **FD data bit rate**: only for interfaces that support CAN FD.
- **Listen-only**: the adapter never acknowledges or transmits frames. See below.
- **Start simulated devices on this virtual bus**: only for the Virtual interface.

The dialog shows which channel the adapter will open as (**CAN1**, **CAN2**, ...). Your choices are remembered
and filled in next time.

### Adapters

| Adapter | Interface | Channel | Notes |
|---|---|---|---|
| PEAK PCAN-USB | PEAK PCAN-USB | `PCAN_USBBUS1`, `PCAN_USBBUS2`, ... | Needs the PEAK driver (installing PCAN-View installs it). Listen-only and CAN FD. |
| Kvaser | Kvaser | `0`, `1`, ... | Needs the Kvaser driver. Listen-only (silent mode) and CAN FD. The "Kvaser Virtual CAN Driver" channels are a loopback pair for testing. The Statistics tab also shows the adapter's own bus load figure. |
| CANable with slcan firmware | CANable / slcan (COM port) | the COM port, for example `COM5` | No driver needed. Listen-only. CAN FD on a CANable 2, with a data bit rate of 2 or 5 Mbit/s only. Clones that keep the chip maker's USB id are detected as "STM32 virtual COM port". |
| CANable with candleLight firmware | CANable / candleLight (USB) | `0` | A USB device; libusb is included. If it is not detected, bind the WinUSB driver to it once with Zadig. Classic CAN only, no listen-only. |
| Vector | Vector | the driver's global channel index, `0`, `1`, ... | Needs the Vector driver library. Nothing has to be set up in the Vector hardware configuration first. Listen-only and CAN FD. |
| IXXAT | IXXAT | `0`, `1`, ... | Needs the IXXAT VCI driver. CAN FD; no listen-only. |
| Linux SocketCAN | Linux SocketCAN | `can0`, ... | Linux only (from source). Set the bit rate in the operating system (`ip link`); the bit rate field is disabled. |
| Virtual | Virtual (no hardware) | any name, default `canopener` | A bus inside CANopener for trying things and for tests. |

Which CANable firmware do you have? If the board shows up in Device Manager as a COM port, it runs slcan. If it
shows up as a USB device (or as an unknown device), it runs candleLight. Both work; slcan needs no driver
setup, candleLight is faster on a busy bus.

The **Bit rate** list offers 10, 20, 50, 83.3, 100, 125, 250, 500, 800 and 1000 kbit/s. Not every driver can run
every one of them: slcan has no 800 kbit/s and IXXAT no 83.3 kbit/s, and CANopener says so when you connect.
PEAK, Kvaser, Vector and candleLight run every listed rate.

### Listen-only

A listen-only adapter never sends anything onto the bus, not even the acknowledge bit. Use it to watch a live
machine without any chance of disturbing it.

On a listen-only channel nothing is transmitted: sending and node changes are refused, cyclic entries stop, a
replay onto it sends nothing (each frame counts as a send error), and a node scan only listens.

A listen-only adapter that is the only other device on the bus sees nothing useful: a frame nobody
acknowledges is repeated by its sender and shows as errors. At least one other node must acknowledge.

### CAN FD

Tick **CAN FD** and choose the **FD data bit rate**. The nominal bit rate stays the arbitration rate. Received
FD frames show as type **FD** (or **FD BRS** with bit rate switching) in the receive list and the trace. To
send FD frames, tick **CAN FD** (and **Bit rate switch** if wanted) in the transmit entry.

### Several channels

Click **+ Connect** again to open another adapter, or another channel of the same adapter. Each opens as the
next free channel (CAN2, CAN3, ...) and gets its own chip and its own part of the status bar.

- The receive list shows a **Ch** column as soon as more than one channel is connected; the trace always has
  one. Both have a channel selector. Statistics shows one card per channel.
- A transmit entry has a **Channel**.
- A recording records every connected channel.

The **×** on a chip disconnects that channel.

## The screen

- **Top bar**: the CANopener logo; one chip per channel (state dot, name, adapter, channel and bit rate, `FD`
  and `LO` for listen-only; hover a red chip for its error); **+ Connect**; a red **REC** indicator while
  recording; **Project**; the light / dark switch (◑); **?** for the About box.
- **Tabs**: Receive / Transmit, Trace, Record & Replay, Statistics, Decode, Dashboards, Nodes (CANopen), the
  tabs of plugins (the shipped example adds **Heartbeats**), and Plugins.
- **Status bar**: per channel the bus load bar and percentage, frames per second, the bus state when it is not
  ACTIVE (warning, passive, bus off), and error counters (error frames / receive errors / send errors). **Log**
  opens the event log; its badge counts new events.

The **event log** lists what happened: connects and disconnects, adapter errors, recordings, node jobs.
Warnings and errors also show briefly as a message in the corner.

The **About** box (**?**) shows the version, the app folder, the MCP address for Claude, how many plugins loaded and
a **User guide** button that opens this guide.

![The About box with the User guide button](img/about.png)

## Receive / Transmit

The main tab, as in PCAN-View: the receive list on top, the transmit list below. Drag the bar between them to
resize.

![Receive / Transmit](img/receive.png)

### Receive list

One row per CAN ID (per channel):

| Column | Meaning |
|---|---|
| Ch | The channel (only with several channels). |
| ID | Hex; 3 digits for 11-bit, 8 digits for 29-bit IDs. `Tx` marks frames CANopener sent. |
| Type | Std, Ext, FD, FD BRS, RTR, Error. |
| DLC | Data length. |
| Data | The last data. Bytes that changed in the last 1.5 s are highlighted. |
| Cycle ms | The last interval between two frames of this ID. |
| Count | Frames received. |
| Min ms / Max ms | The shortest and longest interval seen. |
| Decoded | The decoded content, as soon as a decoder knows this ID (DBC, CANopen EDS, heartbeats, EMCY, J1939). |

- Click **ID**, **DLC**, **Cycle ms** or **Count** in the header to sort; click again to reverse.
- **ID filter** takes hex IDs and ranges, separated by commas: `100-1FF, 7E5`.
- The channel selector shows **All channels** or one.
- **show Tx** adds rows for frames CANopener itself sent.
- **Freeze** stops the view from updating (the button then says **Frozen**). Traffic keeps being counted.
- **Clear** resets the rows and counters of the selected channel (or of all channels).
- **To transmit** copies the selected row into a new transmit entry, with its data, flags and cycle time. A
  double-click on a row does the same.

### Transmit list

| Column | Meaning |
|---|---|
| On | Starts or stops a cyclic entry. Disabled for an entry without a cycle time. |
| ID, Type, DLC, Data | The frame. |
| Cycle ms | The cycle time, or `manual`. |
| Count | Frames sent. |
| Comment | Your note. An **error** badge shows if sending failed (hover it for the reason). |

- **+ New** adds an entry. **Stop all** stops every cyclic entry.
- **Send** sends the entry once. Or click a row to select it and press **Space**.
- **Edit** (or a double-click on the row) changes it. **×** deletes it.

The transmit dialog:

![Editing a transmit entry in DBC message mode](img/tx-dbc.png)

- **ID (hex)**, **Channel**, **Cycle time ms (0 = manual)**.
- **Extended (29-bit)**, **CAN FD**, **Bit rate switch** (FD only), **Remote request** (then a **Remote request
  DLC** field appears).
- **Data from**: appears once a DBC file is loaded (see [Decode](#decode-dbc-canopen-j1939)). Choose **raw
  bytes** to type the data yourself, or a **DBC message** to type signal values: one field per signal, with a
  list for signals that have named values. The ID and data fields are then not used: when you click **Add**
  (or **Save**), CANopener encodes the bytes with the DBC and the transmit row shows them.
- **Data (hex bytes)**: for example `11 22 33 44`.
- **Comment**.

The transmit list is saved automatically. After a restart the entries are back, but cyclic entries never start
by themselves: tick **On** again.

## Trace

Every frame in time order, received and sent, from all channels. The view scrolls with the newest frames;
scroll up to look at older ones (a **Jump to latest** button brings you back).

![The trace with a filter: the fast frames 0CF and 18FEF100 excluded](img/trace.png)

- **Pause** stops the live view (**Paused - resume** continues). Recording and counters keep running. A
  `paused` row marks the gap.
- **Clear** empties the view.
- **Filter**:
  - the channel selector;
  - **IDs**: hex IDs and ranges, `100-1FF, 7E5`; tick **exclude** to hide these IDs instead of showing only
    them;
  - **Rx + Tx**, **Rx only** or **Tx only**;
  - **data contains (hex)**: frames whose data contains these bytes, for example `2F 00 21`.

  The filter runs in the server, before the frames are sent to the window, so a rare ID is never lost among
  thousands of others.
- **Time**: **Time (s)** since the first frame shown, **Delta (ms)** to the previous frame shown, or **Clock**
  (wall-clock time).
- **Save...** writes everything the trace buffer holds to a file in the recordings folder: all channels,
  without the filter, about the last 260 000 frames per channel. Formats `.trc`, `.asc`, `.blf`, `.log` and
  `.csv`; the name is automatic unless you give one.

The info line on the right says how many frames are shown. The live view shows up to 25 000 frames per second.
If more arrive, it says how many were not shown live; they are still in the buffer, in a save and in any
recording. Use **Record** for long captures.

## Record & Replay

![Recording to fault-hunt.trc](img/record.png)

### Record

1. Optionally type a **File name** (default: the date and time).
2. Choose the **Format**.
3. Click **Start recording**.

Every connected channel is recorded, received and sent frames, by a writer thread of its own, so a busy bus
does not drop frames. The card shows the file, the frames written, the duration and the size; the top bar
shows **REC**. **Stop** finishes the file.

An existing file is never overwritten: `-1`, `-2` ... is added to the name instead.

| Format | Use |
|---|---|
| `.trc` | PCAN-View trace (version 2.1: CAN FD, Rx/Tx, several buses). |
| `.blf` | Vector binary logging format (CANalyzer, CANoe, ...). |
| `.asc` | Vector ASCII. |
| `.log` | Linux candump. |
| `.csv` | Spreadsheets. |
| `.canbin` | A compact binary format for loggers on a PLC (classic CAN). |

### Recordings

The **Recordings** card lists the files in the recordings folder (its path is shown next to the title), newest
first:

- **Open** loads the file for offline viewing (see below).
- **Replay...** sends it onto a bus.
- **Download** saves a copy where you choose.
- **Import file...** copies a `.trc`, `.asc`, `.blf`, `.log`, `.csv` or `.canbin` file from elsewhere into the
  recordings folder. **Refresh** reads the folder again.

### Open a recording offline

**Open** loads the file as a *file channel*: it shows as a chip named after the file, with the word `file`.

![A recording opened offline as a file channel](img/file-view.png)

- The receive list shows its IDs with the real cycle times from the recorded timestamps (pick the file in the
  channel selector to see only it).
- The trace shows its frames, from the start.
- The status bar shows how many frames it holds.

A file channel never transmits and needs no adapter. Close it with the **×** on its chip.

### Replay

![The replay dialog](img/replay.png)

**Replay...** asks:

- **Bus 1 of the file onto**: the connected channel to send on;
- **Speed**: 0.5x, 1x (recorded timing), 2x or 10x;
- **Frames**: all, recorded Rx only, or recorded Tx only;
- **Loop**: start over at the end.

Replay transmits every recorded frame onto the bus. Make sure that is safe for the machine. The Replay card
shows the progress, with **Pause** / **Resume** and **Stop**.

## Statistics

![Statistics](img/stats.png)

One card per channel:

- **State**: open, error or file, and the bus state (ACTIVE, warning, passive, bus off).
- **Bus load**: now and peak, estimated from the frames (with Kvaser also the adapter's own figure).
- **Frames/s**: now and peak.
- **Received**, **Transmitted**.
- **Error frames** (and per second), **Receive / send errors**, adapter overruns.
- **Distinct IDs**, and how long the channel has been **Connected**.

The chart shows bus load and frame rate over the last 10 minutes.

**Busiest IDs** ranks the periodic IDs by their average rate over the whole time they were seen: frames per
second, average cycle, minimum and maximum interval, count. IDs seen fewer than 5 times (single SDO
transfers, NMT commands) are left out.

## Nodes (CANopen)

This tab finds the CANopen nodes on a channel and changes their node ID or bit rate. Every change is a *plan*:
you first see every frame that will be sent, tick a box, and only then is anything sent. Every scan, plan and
run is written to `logs\nodes.log`.

![Nodes after a scan](img/nodes.png)

### Scan

Choose the **Channel**, the **listen** time (default 2.5 s) and click **Scan**.

1. CANopener listens: heartbeats, boot-ups and PDOs hint at node ids.
2. It pings every node id it has not already seen (heartbeat, boot-up) with an SDO read of 0x1000 (device
   type).
3. For each node found it reads the identity (0x1018: vendor id, product code, revision, serial number) and
   the device name (0x1008).

The table shows each node with its name, identity, how it was seen (heartbeat, pdo, sdo), its NMT state, and
the **recipe** CANopener will use to change its node ID and bit rate. Below the table: how many raw 11-bit
and extended IDs were seen, and PDO-range traffic that has no CANopen node behind it. **raw ids** lists them
all.

The scan only reads: it changes nothing on any node. On a listen-only channel it only listens. The job log
under the table shows each step; **Stop** cancels a running job. **Session log...** shows the end of
`logs\nodes.log`.

### Recipes

A recipe says how a device changes its node ID (and bit rate). CANopener picks it by the vendor id from 0x1018
(and the product code, when a recipe names one):

- **LSS** (CiA 305) is the default for every device without a recipe;
- **SDO recipes** write the device's own node-id object, store, and reset;
- **read-only** devices cannot be changed (**Set ID...** is disabled).

CANopener ships no recipes for real devices, only those of the simulator's demo devices. For your devices,
build the sequence in the [custom mode](#custom-mode-for-unknown-devices) and **Save as recipe**. From then on
**Set ID...** and **Bit rate...** use it automatically.

### Set ID

**Set ID...** on a node row asks for:

- **New node ID (1..127)**;
- **Method**: automatic (by vendor id), LSS, or a named recipe;
- **Verify timeout s**: how long to wait for the node to answer at its new id.

**Preview** then shows the plan:

![Set-ID preview of the demo LSS device: every frame of the LSS sequence](img/nodes-preview.png)

- the channel, the recipe and where it comes from, the number of frames;
- warnings: a **conflict** when the new id's COB-IDs (heartbeat, SDO, PDOs) are already used by any ID seen on
  the bus, not only by other CANopen nodes; that a power cycle is needed;
- every step with the **exact frames** that will be sent, including the conditional ones (the SDO abort after
  a timeout, the polls of the new id), and the reply each step expects.

Steps marked **always** also run after a failure (cleanup, for example LSS back to waiting). Steps marked
**optional** only warn when they fail.

Nothing is sent until you tick **I checked the frames above and want to send them** and click **Run**. The run
shows live in the job log, the result shows under it (verified at the new id, or "power cycle required"), and
the whole run is appended to `logs\nodes.log`.

A scan is needed first: the conflict check and the recipe come from it.

### Bit rate

**Bit rate...** on a node row changes that node's bit rate with its recipe (SDO or LSS). **Bit rate of all LSS
devices...** switches every LSS device on the bus at once. The dialog asks for:

- **New bit rate** (10 to 1000 kbit/s);
- **LSS switch delay ms**;
- **Verify timeout s**.

The preview shows the frames as for Set ID. The sequence is configure, store, then activate. After the switch
the adapter reopens at the new rate and waits for traffic. Nodes that did not switch stay at the old rate and
are no longer heard; the preview warns about this.

The button is disabled for a node whose recipe has no bit-rate method (use the custom mode).

### Custom mode for unknown devices

The **Custom sequence** card builds your own steps, for devices without a recipe, or for anything else you
want to send as a checked, logged sequence.

![The custom sequence editor with the "SDO: write node id, store, reset" template](img/nodes-custom.png)

- **Start from a template...** loads a starting point: SDO node id + store + reset; LSS node id for the only
  device on the bus; LSS bit rate of every LSS device; read identity and name; NMT start all; NMT all to
  pre-operational.
- **+ step** adds a step; the arrows move it, **×** removes it; **Clear** removes all.
- **Variables** (`$OLD_ID`, `$NEW_ID`, `$BAUD`, ...) are filled in above the steps. **+ variable** adds your
  own.
- **Custom** on a scanned node row puts its id into `$OLD_ID` and its identity into `$VENDOR`, `$PRODUCT`,
  `$REVISION`, `$SERIAL`.
- **Preview & run...** shows the same exact-frame preview as Set ID.
- **Save as recipe...** stores the sequence as the recipe for a vendor id (see the walkthrough
  [Commission an unknown device](#commission-an-unknown-device-and-save-a-recipe)).
- **Export JSON** / **Import JSON** share sequences as files.

The editor keeps your steps between sessions.

| Step | What it sends |
|---|---|
| `sdo_write` | SDO download: node, index, sub, type (u8, u16, u32, i8, i16, i32, u64, i64, f32, str, bytes), value. |
| `sdo_read` | SDO upload; with an **expect** value the step fails when the device answers something else. |
| `store` | Writes 'save' to 0x1010 (sub 1 by default). |
| `nmt` | start, stop, preop, reset_node, reset_comm, to one node or all (0). |
| `raw` | Any frame: id, data, 29-bit (ext), FD. |
| `lss` | switch_global (waiting / configuration), switch_selective (vendor, product, revision, serial), configure_node_id, configure_bit_timing, activate_bit_timing, store, inquire_node_id. |
| `delay` | A pause in ms. |
| `reopen` | Reopens the adapter at another bit rate (kbit/s). |
| `wait_node` | Checks that a node answers (heartbeat, boot-up or SDO), with a timeout. |
| `wait_traffic` | Checks that any frame arrives, with a timeout. |

Placeholders: `$OLD_ID`, `$NEW_ID`, `$BAUD` (kbit/s), `$VENDOR`, `$PRODUCT`, `$REVISION`, `$SERIAL`, and your
own variables. Per step, **always** runs it also after a failure, **optional** makes a failure only a warning.

## Decode: DBC, CANopen, J1939

The **Decode** tab has three views, **DBC**, **CANopen** and **J1939**. Decoded values appear:

- in the receive list's **Decoded** column;
- on this tab;
- as signals for [dashboards](#dashboards) and for Claude.

The decoder setup (DBC files, EDS per node, J1939 channels) is saved with the settings and in projects.

### DBC

![DBC view with live signal values](img/decode-dbc.png)

- Give the full path of a `.dbc` file and click **Load**, or **Upload...** one. Choose **All channels** or one
  channel. Several files can be loaded; **×** unloads one.
- The **Signals** table lists every message and signal with its live value, unit and range (and the named
  values). Type into the filter to find a message or signal.
- Multiplexed messages decode (the multiplexer and the signals of the active multiplexer value).
- J1939 messages of a DBC (frame format J1939PG) match by PGN on channels with J1939 on, so every source
  address decodes; the value column then shows one value per source address (`SA 0: ...`).

With a DBC loaded, the transmit dialog has the **DBC message** mode: see
[Decode with a DBC and transmit by signal value](#decode-with-a-dbc-and-transmit-by-signal-value).

### CANopen

![CANopen view: EDS on node 5, its object dictionary with values read from the device, and the EMCY log](img/decode-canopen.png)

- Set the **Node** id, the channel, and give the path of an `.eds`, `.dcf` or `.xdd` file: **Assign**, or
  **Upload...**.
- The node table lists the nodes with a file and the nodes seen sending heartbeats (marked "no EDS"), their
  PDOs and their NMT state from the heartbeats.
- PDOs decode from the file's mapping. **Read PDO mapping** reads the device's real mapping over SDO (uploads
  only) and decodes with that instead (marked **from device**).
- **Objects...** opens the object dictionary browser: index, sub, name, type, access, default, and the value.
  **Read** reads one object from the device. **Write...** asks for a value and opens the exact-frame preview.
  The filter finds objects by name or index.
- **Start**, **Stop**, **Pre-op** and **Reset** send NMT commands to the node, after the same preview.
- The **EMCY log** decodes emergency messages: the error code with its meaning, the error register bits and the
  vendor-specific bytes. **Clear** empties it (and the J1939 transport-protocol list).

Every write (SDO write, NMT) is a plan, as on the Nodes tab: nothing is sent before you have seen the frames
and confirmed.

### J1939

![J1939 view: address claims, DM1 trouble codes and a reassembled BAM transfer](img/decode-j1939.png)

Tick **J1939 decoding on** for each channel that carries J1939. Then:

- 29-bit IDs split into priority, PGN, destination and source address; the receive list's Decoded column shows
  the PGN name and addresses.
- **ECUs (address claims)** lists every address claim with its NAME decoded: identity number, manufacturer
  code, function, ECU and function instance, vehicle system, industry group, arbitrary address capability.
- **DM1 active diagnostic trouble codes** shows the lamps (MIL, RSL, AWL, PL; lit when on) and the active DTCs
  (SPN, FMI, occurrence count) per ECU.
- **Transport protocol messages** lists messages longer than 8 bytes, reassembled from RTS/CTS and BAM
  transfers (and aborted ones with the reason), with the decoded content.

### Signal names

| Source | Signal key |
|---|---|
| DBC | `dbc/<Message>/<Signal>` |
| DBC, J1939 message | `j1939/<SA>/<PGN>.<Signal>` |
| CANopen PDO | `canopen/<node>/<PDO>.<variable>`, for example `canopen/5/TPDO1.Position` |
| CANopen NMT state, EMCY | `canopen/<node>/NMT`, `canopen/<node>/EMCY.code`, `canopen/<node>/EMCY.register` |
| J1939 DM1 | `j1939/<SA>/DM1.ActiveDTCs` (number of active DTCs) |
| Quick signal | `raw/<name>` |

## Dashboards

![A dashboard with a time plot, a gauge, numeric tiles and a lamp](img/dashboards.png)

The **Dashboards** tab holds any number of named dashboards. Pick one in the list; **New**, **Rename** and
**Delete** manage them.

| Widget | Shows |
|---|---|
| Time plot | One or more signals over 10 s to 10 min. **Pause** freezes it; drag across the plot to zoom, double-click to zoom out; **CSV** saves the plotted values. |
| XY plot | One signal against another over a time window. |
| Gauge | A value on a dial with min, max, unit, number format and coloured threshold bands. |
| Numeric tile | A value with unit and format (auto, 0, 0.0, 0.00, 0.000, hex) and threshold colours. Named values show as text. |
| Bit lamp | On or off: when the value is not 0, when one bit is set, or when the value is at or above a threshold; with on and off colours. |

### Edit the layout

Click **Edit layout**. The button becomes **Done**, and **+ Widget** appears.

![Edit mode: move by the title bar, resize by the corner](img/dash-edit.png)

- **+ Widget** asks for the widget type, then its settings.
- Drag a widget by its title bar to move it; drag its bottom-right corner to resize. Widgets snap to a
  12-column grid and never overlap.
- **⚙** opens a widget's settings, **×** removes it.

![Widget settings with the signal picker](img/dash-widget.png)

In the settings, **Search signals** lists every signal that is being published right now, with its current
value. Click one to use it (for a time plot: to add it). You can also type a key. Gauges and tiles take
min, max and unit from the signal when you leave them empty.

Changes are saved as you make them.

### Quick signals (without a DBC)

**Quick signals** defines signals on raw frames, for when there is no DBC file.

![The quick signals editor with a live preview](img/quick-signals.png)

Per signal: **Name**, **Channel** (any or one), **ID** (hex; **Ext** for 29-bit, set automatically above
7FF), **Start bit**, **Length**, **Byte order** (Intel or Motorola), **Signed**, **Factor**, **Offset**,
**Unit**, **Min**, **Max**. The **Now** column shows the value decoded from the latest frame while you type.
**Save** publishes them as `raw/<name>`.

Bit numbering is as in DBC files: for Intel (little endian) the start bit is the least significant bit; for
Motorola (big endian) it is the most significant bit, and bit 7 of byte 0 is the first bit on the wire.
Value = raw × factor + offset.

Dashboards and quick signals are saved with the settings and in projects. Dashboards look the same in the light
theme:

![The same dashboard in the light theme](img/light-theme.png)

## Projects and settings

![The Projects dialog](img/projects.png)

**Project** in the top bar saves or opens a whole setup under a name. A project keeps:

- the transmit list;
- the adapter settings of the connected channels;
- the decoder setup (DBC files, EDS per node, J1939 channels);
- the dashboards and quick signals.

**Save** stores the current setup under the name you type. **Open** loads a saved project: cyclic transmits
stop, the transmit list, decoders and dashboards are replaced, and if no adapter is connected CANopener asks
whether to connect the project's adapters.

Without projects, CANopener remembers anyway (in `settings.json`): the last connection of each channel (the
Connect dialog is filled in with it), the transmit list, the decoder setup, dashboards and quick signals.
Adapters are never connected automatically at start.

View preferences (theme, last tab, sort order, the receive / transmit split, the trace filter, the custom
sequence editor) are kept per PC in the window's own storage.

## Task walkthroughs

### Watch a bus and find which ID floods it

1. Connect the adapter with **Listen-only** ticked (see [Listen-only](#listen-only)).
2. Look at the status bar: bus load and frames per second. The load bar changes colour above 50 % and turns
   red above 80 %.
3. Open **Statistics**. **Busiest IDs** ranks the periodic IDs by frames per second; the top rows are what
   loads the bus. **Min - max ms** shows whether an ID is regular or bursty.
4. On **Receive / Transmit**, click the **Count** header (twice for highest first) or **Cycle ms**. Type the
   suspect ID into the **ID filter** to watch only it.
5. To see the order of frames around a burst, open **Trace**, type the busy IDs into **IDs** and tick
   **exclude** to hide them, or leave **exclude** off to see only them. **Delta (ms)** shows the gaps.

Error frames and bus state warnings show in the status bar and on the Statistics card. Many error frames with
little traffic often mean a wrong bit rate or missing termination.

### Send a cyclic frame

1. Connect (not listen-only).
2. Under **Transmit**, click **+ New**.
3. Type the **ID (hex)**, the **Data (hex bytes)** and a **Cycle time ms** (for example 100).
4. Click **Add**.
5. Tick **On** in the new row. **Count** starts counting.

Untick **On** to stop it; **Stop all** stops every cyclic entry. To send a frame once, set the cycle time to
0 and click **Send** (or select the row and press Space). To copy a frame you see on the bus, select it in the
receive list and click **To transmit**.

### Record a fault and send the file to a colleague

1. Connect the channels you need (listen-only is fine).
2. On **Record & Replay**, type a **File name**, for example `fault-hunt`, and choose the format your
   colleague can open: `.trc` for PCAN-View, `.blf` or `.asc` for Vector tools, `.log` for candump, `.csv` for a
   spreadsheet.
3. Click **Start recording** before the fault happens. The top bar shows **REC** with the frame count.
4. When the fault has happened, click **Stop**.
5. In **Recordings**, click **Download** on the file to save a copy where you want, or take it from the
   `recordings` folder next to `canopener.exe` and attach it to an e-mail.

Missed the start? **Trace** > **Save...** writes the last few minutes the trace buffer still holds (about
260 000 frames per channel) to a file.

### Open a recording offline

1. If the file is not in the recordings folder yet, click **Import file...** in **Recordings** and pick it.
2. Click **Open** on the file. No adapter is needed.
3. The receive list shows the file's IDs with their recorded cycle times; choose the file channel in the
   channel selector to see only it. The trace shows the frames from the start.
4. Close the file with the **×** on its chip.

To play the file onto a real bus instead, use **Replay...**.

### Give a CANopen node a new ID

With the simulator you can try both ways safely: demo device A has an SDO recipe, the demo LSS device uses LSS.

**With an SDO recipe** (a device CANopener has a recipe for):

1. On **Nodes (CANopen)**, click **Scan**.
2. On the node's row (for example node 5, recipe `sim-demo-a`), click **Set ID...**.
3. Type the **New node ID** (for example 6) and click **Preview**.
4. Read the frames: the SDO write of the node-id object, the store to 0x1010:01, the NMT reset, and the check
   that node 6 answers.

   ![Set-ID preview of demo device A: SDO write, store, NMT reset, verify](img/nodes-preview-sdo.png)
5. Tick **I checked the frames above and want to send them** and click **Run**.
6. The job log shows each step; the result says whether the node answered at its new id. **Scan** again to see
   it in the table.

If the device needs a power cycle (demo device B), the preview warns, and the result says "power cycle
required". Switch the device off and on, then scan. (In the simulator, demo device B applies its new id at
the next NMT reset instead: Decode > CANopen > **Reset** on node 10.)

**With LSS** (CiA 305):

1. Scan, then **Set ID...** on the node (for example node 20, recipe `generic-lss`).
2. The preview selects the device by its identity (switch selective: vendor, product, revision, serial), so
   only this device is configured even when several LSS devices share the bus. Then configure node id, store,
   switch every device back to waiting (an **always** step), NMT reset communication, and the check of the new
   id.
3. Tick the box and **Run**.

For a single LSS device whose identity you do not know (it may not even have a valid node id), use the custom
sequence template **LSS: give the only device on the bus a node id**. It uses switch global, so connect only
that one device.

### Change bit rates

The whole bus has to move together. Plan it so you do not lose contact with devices halfway:

1. Scan, so CANopener knows the devices and their recipes.
2. For every LSS device at once: **Bit rate of all LSS devices...**, choose the new rate, **Preview**, check,
   **Run**. The devices store the new rate and switch after the switch delay; the adapter reopens at the new
   rate and waits for traffic.
3. For a device with an SDO bit-rate recipe (demo device A): **Bit rate...** on its row. A device that only
   switches after a power cycle (the preview says so) is reached again after you power it and reconnect at the
   new rate.
4. Afterwards, check the chip in the top bar shows the new rate, and **Scan** again.

Devices without LSS or a recipe stay at the old rate and are no longer heard; the preview warns about this.
Change those first (with their own tool or a custom sequence ending in a `reopen` step), and LSS devices last.

### Commission an unknown device and save a recipe

For a device CANopener has no recipe for. You need to know, from the device's manual or EDS, which object holds
its node id (and whether it needs a store and a reset).

1. **Scan**. The device shows with recipe `generic-lss`. If it does not support LSS, Set ID would fail.
2. Click **Custom** on its row: `$OLD_ID` and the identity variables are filled in.
3. **Start from a template...** > **SDO: write node id, store, reset**.
4. In the first step (`sdo_write`), set the **index**, **sub** and **type** of the device's node-id object.
   Leave the value as `$NEW_ID`.
5. Type the new id into `$NEW_ID`.
6. **Preview & run...**, check every frame, tick, **Run**. The result table shows each step's status.
7. When it worked, click **Save as recipe...**:
   - **Vendor id (hex)**: filled in from the scan;
   - **Name**: a short name;
   - **Used for**: changing the node ID (or the bit rate);
   - **New id takes effect**: immediately / after the reset in the steps, or after a power cycle;
   - **Note**: where the mechanism comes from (manual section, EDS, a trace).

   Keep `$OLD_ID`, `$NEW_ID` and `$BAUD` in the steps: the recipe fills them in each time.
8. From now on, **Set ID...** (and **Bit rate...**, for a bit-rate recipe) picks your recipe for every device
   with that vendor id. It is stored in `recipes\user-recipes.json`.

**Export JSON** saves the sequence to share it; a colleague loads it with **Import JSON** and saves it as a
recipe on their PC.

### Decode with a DBC and transmit by signal value

1. On **Decode** > **DBC**, give the path of the `.dbc` file and click **Load** (or **Upload...**).
2. The **Signals** table shows live values. The receive list's **Decoded** column shows the decoded messages
   (tick **show Tx** to see your own frames decoded too).
3. To send by signal value: **+ New** under Transmit. In **Data from**, choose **DBC message ...**.
4. Type the signal values (lists for named values). The ID comes from the DBC; the bytes are encoded when
   you click **Add** and show in the transmit row.
5. Set a cycle time if wanted, **Add**, and tick **On**.

To change a value while it is sending, click **Edit**, change the signal, **Save**.

### CANopen EDS: PDOs, SDO browser, NMT, EMCY

1. On **Decode** > **CANopen**, type the **Node** id, give the path of the device's `.eds` (or `.dcf`, `.xdd`)
   and click **Assign**.
2. The node's PDOs decode from the file: values appear in the receive list's Decoded column (for example
   `TPDO1 node 5 Position=485 Counter=2290`) and as `canopen/<node>/...` signals for dashboards.
3. If the device's mapping differs from the file's defaults, click **Read PDO mapping**: the real mapping is read
   over SDO and used instead.
4. **Objects...** opens the object dictionary. **Read** shows the value in the device. **Write...** asks for a
   value, then shows the SDO frames to confirm.
5. **Start**, **Stop**, **Pre-op**, **Reset**: NMT commands for the node, after the preview.
6. Emergencies of known nodes appear in the **EMCY log**, decoded.

### J1939: ECUs, DM1, transport messages

1. Connect at the bus rate (often 250 or 500 kbit/s), listen-only on a running vehicle.
2. On **Decode** > **J1939**, tick the channel.
3. **ECUs** fills as ECUs claim addresses. Address claims are sent at power-up and on request, so switch the
   ignition on after connecting, or wait.
4. **DM1** shows the active trouble codes and lamps of every ECU that sends them, single-frame and multi-packet
   (BAM) alike.
5. **Transport protocol messages** shows every reassembled long message.
6. Load a J1939 DBC on the DBC view to decode signals by PGN for every source address.

### Build a dashboard

1. **Dashboards** > **New**, type a name. The new dashboard opens in edit mode.
2. **+ Widget** > for example **Time plot**. Click signals in the picker to add them, choose the time window,
   **Add**.
3. Add a **Gauge** or **Numeric tile** for a single value, a **Bit lamp** for a status bit.
4. Move and resize the widgets, then click **Done**.

No DBC for the frame you want to watch? Use **Quick signals**:

1. Click **Quick signals**, **+ Signal**.
2. Name it, type the **ID**, the **Start bit** and **Length**, byte order and sign, and factor / offset if
   needed. Watch the **Now** column: it decodes the latest frame as you type, so you can find the right bits
   by trial.
3. **Save**. The signal appears as `raw/<name>` in the widget picker.

### Save and reuse a setup with projects

1. Connect the adapters, fill the transmit list, load the decoders, build the dashboards.
2. **Project** > type a name > **Save**.
3. Later, or on another day: **Project** > pick it in **Or open** > **Open**. CANopener offers to connect the
   project's adapters.

Project files are in the `projects` folder; copy one to a colleague's `projects` folder to share the setup.

## Claude (MCP)

While CANopener runs, it serves an MCP endpoint at `http://127.0.0.1:8775/mcp/` (the address is also shown in
the About box). Add it to Claude Code once:

```
claude mcp add --transport http canopener http://127.0.0.1:8775/mcp/
```

Claude then works on the same bus session as your window: it sees what you see, and you see what it does.
Channels are numbered from 0 for Claude (channel 0 is CAN1 in the window).

**What Claude can do without asking.** The read tools only read. Their names start with `get_`, `list_`,
`read_` or `scan_`, so you can allowlist them in Claude Code:

- `get_about`, `get_status`, `list_devices`, `get_bus_stats`;
- `get_receive_table`, `get_trace`, `get_signals`, `get_signal_history`, `get_decoded_frames`;
- `list_recordings`, `read_recording`;
- `scan_nodes`, `get_node_scan`, `get_node_job`, `list_recipes`;
- the previews: `get_set_node_id_preview`, `get_baud_preview`, `get_sequence_preview`,
  `get_sdo_write_preview`, `get_nmt_preview` (they return frames, they send nothing);
- `get_decode_config`, `list_dbc_messages`, `get_canopen_state`, `list_object_dictionary`, `read_sdo`,
  `get_j1939_state`;
- `list_dashboards`, `get_dashboard`;
- plugin read tools, for example `get_heartbeat_nodes` of the shipped example.

`scan_nodes` and `read_sdo` do send frames (SDO reads, and a listen period), but they change nothing on a node.

**What stays on approval.** Every other tool has another verb and transmits, records, replays, loads files or
changes a node: `connect_channel`, `disconnect_channel`, `send_frame`, `add_transmit_entry`,
`set_transmit_entry`, `stop_all_transmit`, `start_recording`, `stop_recording`, `start_replay`, `stop_replay`,
`open_recording_view`, `load_dbc_file`, `assign_eds_file`, `set_j1939_decoding`, `load_pdo_mapping_from_device`,
`save_dashboard`, `delete_dashboard`, `save_user_recipe` and `run_node_plan`. Keep these on approval, so Claude
Code asks you for each call.

**Node changes take two steps.** Claude first calls a preview tool (`get_set_node_id_preview`,
`get_baud_preview`, `get_sequence_preview`, `get_sdo_write_preview` or `get_nmt_preview`). It returns the exact
frames and a token, and Claude shows you the frames. Then it calls `run_node_plan(token, title)`, which runs
that plan unchanged. The title must be the preview's title, for example `Node 5 -> 6`, so the approval prompt
says what will run. The run is logged in `logs\nodes.log` as done by Claude.

Things to ask, for example:

- "What is on the bus? Which IDs are busiest?"
- "Scan for CANopen nodes and tell me what you find."
- "Show me the frames to change node 5 to node 6." (and, after reading them, "Run it.")
- "Decode the last DM1 of every ECU."
- "Open fault-hunt.trc and find when 0x185 stopped."

## Plugins

A plugin is a folder with a `manifest.json` and a Python module. It can add:

- tabs to the window;
- REST routes;
- MCP tools for Claude;
- live frame consumers, which never slow the receive threads down.

To install one, put its folder into the `plugins` folder next to `canopener.exe` and restart CANopener. The
**Plugins** tab lists what loaded, from which folder, and what failed (with the error). A broken plugin never
stops CANopener.

![The Plugins tab with the shipped example](img/plugins.png)

The shipped example is a CANopen heartbeat monitor: the **Heartbeats** tab shows, per node, the NMT state, the
heartbeat period, when it was last seen, the count and the boot-ups, and Claude gets `get_heartbeat_nodes`.

Plugins run with full access to your PC and the bus: install only what you have reviewed. To write one, see
[Writing plugins](PLUGINS.md).

## Command line

`canopener-cli.exe` (next to `canopener.exe`) runs the same program with a console. `canopener-cli.exe --help`
lists the commands, `canopener-cli.exe <command> --help` the options.

| Command | Does |
|---|---|
| `app` | The app window (the default when started without a command). `--port`. |
| `serve` | The server only; open `http://127.0.0.1:8775/` in a browser. `--port`, `--host`. Ctrl+C stops it. |
| `devices` | Lists the adapters that are plugged in. `--json`. |
| `scan` | Scans a CANopen bus and lists the nodes. `--seconds`, `--sdo-timeout`, `--json`. |
| `set` | Changes a node's node id: `--from`, `--to`, `--method` (lss, readonly, sdo:&lt;name&gt;), `--verify-timeout`, `--recipes`. |
| `baud` | Changes the bit rate: `--to` (kbit/s), `--node` (one node) or `--global` (every LSS device, the default), `--switch-delay-ms`. |
| `record` | Captures for `--seconds` to `--out`; the extension picks the format. `--listen-only`. |
| `trace` | Captures to `.trc` (or `.canbin` with `--canbin`); `trace convert in.canbin out.trc`. |
| `replay` | Plays a recorded file onto the bus: `--speed`, `--only` (all, rx, tx). |
| `convert` | Converts between recording formats, by extension. |

The bus commands take `--interface` (pcan, kvaser, slcan, gs_usb, vector, ixxat, socketcan, virtual; default
virtual), `--channel`, `--bitrate` (bit/s, default 250000), `--fd`, `--data-bitrate`, and `-v` to print every
step and frame. The command line does not start the simulator: without `--interface`, a `scan` finds an empty
virtual bus.

```
canopener-cli.exe devices
canopener-cli.exe scan    --interface kvaser --channel 0 --bitrate 250000
canopener-cli.exe set     --interface pcan --from 1 --to 72
canopener-cli.exe baud    --interface pcan --to 500 --node 72
canopener-cli.exe record  --interface pcan --seconds 60 --out capture.blf --listen-only
canopener-cli.exe replay  --interface pcan capture.blf --speed 1
canopener-cli.exe convert capture.canbin capture.trc
canopener-cli.exe serve   --port 8775
```

`set` (and `baud --node`) scan first. `set` and `baud` print the same exact-frame plan as the window and ask `Send this? [y/N]` before
sending anything. `--yes` skips the question (for scripts).

`serve --host 0.0.0.0` makes CANopener reachable from other PCs. There is no login: anyone who can reach the
port can use the bus. Only do this on a network you trust.

## Files and folders

Everything sits next to `canopener.exe` (from source: in the repository folder):

| Folder / file | Contents |
|---|---|
| `recordings\` | Recordings, trace saves and imported files. |
| `logs\canopener.log` | The app log. |
| `logs\nodes.log` | Every node scan, plan and run, with the frames. |
| `projects\` | Saved projects (one `.json` each). |
| `settings.json` | Last connections, transmit list, decoder setup, dashboards, quick signals. |
| `recipes\user-recipes.json` | Recipes saved from the custom mode. |
| `plugins\` | Your plugins. |
| `docs\user-guide.html` | This guide (open it by double-click; the About box opens it too). |
| `README.txt` | A one-page summary. |

To move CANopener to another PC, copy the whole folder. To start fresh, delete `settings.json`.

Environment variables for special setups: `CANOPENER_HOME` puts the app folder elsewhere,
`CANOPENER_PLUGIN_DIRS` adds plugin folders (separated by `;`), `CANOPENER_USER_RECIPES` points to another user
recipes file.

## Troubleshooting

**Nothing happens on double-click.** Run `canopener-cli.exe app` in a command prompt to see the message, and
read `logs\canopener.log`.

**"Port 8775 is in use by another program".** Another program uses CANopener's port. Close it, or start
CANopener on another port: `canopener-cli.exe app --port 8776` (the MCP address then changes too).

**Windows protected your PC.** See [Download and start](#download-and-start): **More info**, **Run anyway**.

**An adapter is not detected.**

- Install the adapter's driver (PEAK, Kvaser, Vector, IXXAT). Unplug and replug, then **Refresh**.
- CANable with candleLight firmware: bind WinUSB once with Zadig.
- CANable with slcan firmware: it is a COM port; check Device Manager for its number and type it as the channel.
- Close other programs that hold the adapter (another CAN tool, a second CANopener on another port).

**"... cannot run at ... bit/s".** That adapter's driver has no setting for that rate. The message lists the
rates it can do.

**"... cannot be opened listen-only" / "does not support CAN FD".** That interface has no listen-only mode or no
CAN FD in CANopener (see the adapter table).

**No frames at all.**

- Check the bit rate: a wrong rate shows as error frames or nothing.
- Check the wiring (CAN high, CAN low, ground) and the termination: 120 Ω at both ends of the bus, about 60 Ω
  measured between CAN high and CAN low with the power off.
- Something must acknowledge frames. A lone adapter that transmits, with no other node on the bus, sees its
  frames fail.

**Error frames, bus state "passive" or "bus off".** Usually a wrong bit rate, missing termination, or a bad
connection. A listen-only adapter cannot cause them.

**The trace says frames were "not shown live".** More than 25 000 frames per second arrived. They are all in
the buffer, in **Save...** and in any recording; only the live view skipped them.

**A scan finds no node.** Check the bit rate. The table note says whether there was other traffic. Devices that
are in LSS configuration state or have no valid node id (255) do not answer SDO: use LSS.

**A Set ID run failed.** Read the job log and the result table: which step failed and what the device answered
(an SDO abort code, a timeout). Steps marked **always** have already put LSS devices back to waiting. The run is
in `logs\nodes.log`.

**The Decoded column stays empty.** Load a DBC or assign an EDS on the Decode tab, and switch J1939 on for
J1939 channels. Your own frames are decoded too, but only show with **show Tx**.

**A plugin does not load.** The **Plugins** tab shows the error. Check `manifest.json` and restart CANopener
after every change.

**Claude does not see CANopener.** CANopener must be running. Check the address in the About box (another port
means another MCP address) and run `claude mcp list`.

## FAQ

**Does CANopener replace PCAN-View?** For everyday work, yes: receive list, transmit list, trace, recording in
the `.trc` format, statistics; with PEAK and many other adapters.

**Can it damage a device?** Watching cannot, especially in listen-only. Sending can, like any CAN tool:
transmits and replays go onto the bus as you set them up. Node changes are always shown as exact frames first
and need your confirmation.

**Can I use two CANopener windows?** Yes. Both show the same session. A second CANopener with its own adapters
needs another port: `canopener-cli.exe app --port 8776`.

**Does it run on Linux?** From source, with SocketCAN, slcan, candleLight and the other python-can backends.
The release download is for Windows.

**Where are my recordings?** In the `recordings` folder next to `canopener.exe`; the Record & Replay tab shows
the path.

**Why does my vendor's device show "generic-lss"?** CANopener ships no recipes for real devices. LSS is the
standard way; if the device uses its own object instead, build the sequence once in the custom mode and save it
as a recipe.

**Is my data sent anywhere?** No. CANopener runs locally and makes no connections to the internet. The MCP
endpoint is only reachable from your own PC (unless you start `serve --host`).
