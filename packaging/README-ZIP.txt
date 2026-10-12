CANopener - CAN toolbox (PCAN-View replacement)
================================================

The full user guide with screenshots: docs\user-guide.html (double-click it; also "User guide" in the
app's About box, the ? at the top right).

START
  1. Unzip the whole folder somewhere you can write to (Desktop, Documents - not Program Files).
  2. Double-click canopener.exe.
     First start only: Windows SmartScreen may say "Windows protected your PC" because the exe is not
     signed. Click "More info" -> "Run anyway".
  3. Click "+ Connect", pick your adapter from "Detected adapters", choose the bit rate, Connect.

  No adapter at hand? Connect "Virtual (no hardware)" with "Start simulated devices" ticked: you get three
  simulated CANopen nodes (they answer scans and really change their node ID) plus some J1939 traffic.

ADAPTERS
  Kvaser        needs the Kvaser driver (kvaser.com, "Kvaser Drivers for Windows").
  PEAK PCAN     needs the PEAK driver (peak-system.com; installing PCAN-View also installs it).
  CANable       slcan firmware: shows up as a COM port, works without a driver (CAN FD 2/5 Mbit/s data rate).
                candleLight firmware: shows up as a USB device; libusb is included. If it is not detected,
                bind WinUSB to it once with Zadig (zadig.akeo.ie).
  Vector/IXXAT  need the vendor driver; the channel is the driver's channel index.
  "Listen-only" in the connect dialog opens the adapter so it never ACKs or transmits - safe on a live machine.

WHAT IS WHERE
  Receive / Transmit  one row per CAN ID (data, count, cycle time; changed bytes light up) and the transmit
                      list (single or cyclic). Double-click a received row to copy it into the transmit list.
                      Select a transmit row and press Space to send it.
  Trace               every frame in time order; Pause; filter by ID ranges (e.g. 100-1FF, 7E5), direction,
                      data; Save writes the whole buffer to a file.
  Record & Replay     record to .trc (PCAN-View), .blf/.asc (Vector), .log (candump), .csv, .canbin;
                      open a recording to browse it, or replay it onto a bus.
  Statistics          bus load, frames/s, error frames, busiest IDs.
  Decode              load DBC files, assign EDS/XDD to CANopen nodes, switch J1939 on per channel; decoded
                      values show in the receive list's Decoded column. SDO browser, NMT, EMCY log, J1939 ECUs
                      and DM1 trouble codes. Transmit by signal value (DBC message mode in the transmit dialog).
  Dashboards          plots, XY plots, gauges, numbers and lamps from any decoded signal; "Quick signals" decode
                      bytes/bits of raw frames without a DBC.
  Nodes (CANopen)     Scan, Set ID and Bit rate (LSS, or recipes you saved), and a custom mode for unknown
                      devices (SDO steps, raw frames, raw LSS, delays). Nothing is sent before you have seen the
                      exact frames and ticked "I checked the frames". Every run is logged in logs\nodes.log.
  Plugins             loaded plugins (see below).
  Project (top right) saves the transmit list and adapter settings under a name.

FILES (all next to canopener.exe)
  recordings\   your recordings          logs\     canopener.log, nodes.log (node changes)
  projects\     saved projects           plugins\  extra plugins (restart after adding one)
  recipes\user-recipes.json   recipes saved from the custom mode ("Save as recipe")

COMMAND LINE
  canopener-cli.exe --help         all commands
  canopener-cli.exe devices        which adapters are plugged in
  canopener-cli.exe scan --interface kvaser --channel 0 --bitrate 250000
  canopener-cli.exe record --interface pcan --seconds 60 --out capture.blf
  canopener-cli.exe serve          run without the window and open http://127.0.0.1:8775/ in a browser

CLAUDE (MCP)
  While CANopener runs, Claude Code can use it:  claude mcp add --transport http canopener http://127.0.0.1:8775/mcp/
  Read tools (traffic, statistics, scans, previews) can be allowlisted in Claude Code; anything that
  changes a node or sends your frames stays on approval.

PROBLEMS
  Nothing happens on double-click: run canopener-cli.exe app from a command prompt and read the message, and
  look in logs\canopener.log. "Port 8775 in use": another CANopener is already running (it opens a window onto it).
