// node tests/js/core_test.js - checks of the DOM-free helpers in canopener/ui/js/core.js (run by tests/test_ui_js.py).
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm"), assert = require("assert");

const ctx = { window: { addEventListener() {} }, document: { addEventListener() {} }, console };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "..", "canopener", "ui", "js", "core.js"), "utf8"), ctx);
const CO = ctx.window.canopener;
CO.state.channels = [{ index: 0, name: "CAN1" }, { index: 1, name: "CAN2" }];
const parse = (text) => JSON.parse(JSON.stringify(CO.parseFrames(text)));     // objects of the other realm -> plain
const frame = (o) => ({ can_id: 0, ext: false, fd: false, brs: false, rtr: false, dlc: 0, data: "", channel: null, ...o });

// plain "ID data" lines, candump, 0x prefix, a DLC in front of the data, packed hex
assert.deepStrictEqual(parse("18FEF100 FF 80 20 00 00 00 00 FF\n123#11223344\n0x7E5 01 02\n100 8 11 22 33 44 55 66 77 88\n200 a1b2"), {
  frames: [
    frame({ can_id: 0x18FEF100, ext: true, dlc: 8, data: "FF 80 20 00 00 00 00 FF" }),
    frame({ can_id: 0x123, dlc: 4, data: "11 22 33 44" }),
    frame({ can_id: 0x7E5, dlc: 2, data: "01 02" }),
    frame({ can_id: 0x100, dlc: 8, data: "11 22 33 44 55 66 77 88" }),
    frame({ can_id: 0x200, dlc: 2, data: "A1 B2" }),
  ], bad: [] });

// .trc v1.1 rows (4-digit standard id, 8-digit extended id); ";" header lines are skipped
assert.deepStrictEqual(parse(";$FILEVERSION=1.1\n     1)        12.5  Rx         0123  8  11 22 33 44 55 66 77 88 \n     2)        13.0  Tx     00000123  1  05"), {
  frames: [frame({ can_id: 0x123, dlc: 8, data: "11 22 33 44 55 66 77 88" }), frame({ can_id: 0x123, ext: true, dlc: 1, data: "05" })], bad: [] });

// rows copied from the receive list (channel by name, "Tx" after the id) and the trace (channel number, RTR, error frame)
assert.deepStrictEqual(parse("Ch\tID\tType\tDLC\tData\tCycle ms\tCount\nCAN2\t18FEF100 Tx\tExt\t2\tFF 80\t100.0\t5"), {
  frames: [frame({ can_id: 0x18FEF100, ext: true, dlc: 2, data: "FF 80", channel: 1 })], bad: [] });
const tr = parse("#\tTime (s)\tCh\tDir\tID\tType\tDLC\tData\n1\t0.000000\t1\tRx\t701\tRTR\t1\tremote request\n2\t0.1\t1\tRx\t080\tError\t8\terror frame 00 01");
assert.deepStrictEqual(tr.frames, [frame({ can_id: 0x701, rtr: true, dlc: 1, channel: 0 })]);
assert.strictEqual(tr.bad.length, 1);

// transmit-list rows: FD with BRS, more than 8 bytes
assert.deepStrictEqual(parse("Ch\tID\tType\tDLC\tData\tCycle ms\tComment\nCAN1\t100\tFD BRS\t12\t00 01 02 03 04 05 06 07 08 09 0A 0B\tmanual\tx"), {
  frames: [frame({ can_id: 0x100, fd: true, brs: true, dlc: 12, data: "00 01 02 03 04 05 06 07 08 09 0A 0B", channel: 0 })], bad: [] });

// what is not a frame
const junk = parse("hello world\n123 11 2\n123 GG\n20000000 00\n\n");
assert.deepStrictEqual(junk.frames, []);
assert.strictEqual(junk.bad.length, 4);

// review cases: .trc 1.0 and 2.1 (CANopener's own recordings), candump forms, Ext from the Type column (Excel strips
// leading zeros), a DLC that disagrees with the bytes, words that only look like hex
const more = parse([
  "     1)      1841  0123  2  11 22",
  "      1        12.500 DT  1     0123 Rx -  3    01 02 03",
  "      2        13.000 FB  1 18FEF100 Tx -  12   00 01 02 03 04 05 06 07 08 09 0A 0B",
  "      3        14.000 RR  1     0701 Rx -  1    ",
  "(1700000000.5) can0 123#1122",
  "321#R2",
  "456##1AABB",
  "ID\tType\tDLC\tData",
  "701\tExt\t1\t05",
].join("\n"));
assert.deepStrictEqual(more.frames, [
  frame({ can_id: 0x123, dlc: 2, data: "11 22" }),
  frame({ can_id: 0x123, dlc: 3, data: "01 02 03" }),
  frame({ can_id: 0x18FEF100, ext: true, fd: true, brs: true, dlc: 12, data: "00 01 02 03 04 05 06 07 08 09 0A 0B" }),
  frame({ can_id: 0x701, rtr: true, dlc: 1 }),
  frame({ can_id: 0x123, dlc: 2, data: "11 22" }),
  frame({ can_id: 0x321, rtr: true, dlc: 2 }),
  frame({ can_id: 0x456, fd: true, brs: true, dlc: 2, data: "AA BB" }),
  frame({ can_id: 0x701, ext: true, dlc: 1, data: "05" }),
]);
const wrong = parse("100 8 11 22\ndead beef\nadd 12");
assert.deepStrictEqual(wrong.frames, []);
assert.strictEqual(wrong.bad.length, 3);

console.log("core.js helpers: ok");
