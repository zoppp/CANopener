"""Byte-for-byte format checks of the PCAN-View v1.1 layout.

The column layout (widths, the trailing space after the last data byte) was checked against files saved by
PCAN-View v6.0.1.1047; the rows below use that exact layout with made-up ids and data.
"""

import can

from canopener.core.trc import TrcWriter, format_row

# Row 1: id 0x0123, DLC 8, 12.5ms after the capture started.
REFERENCE_ROW_1 = "     1)        12.5  Rx         0123  8  11 22 33 44 55 66 77 88 "
# Row 132: an extended (29-bit) id.
REFERENCE_ROW_132 = "   132)      3456.7  Rx     18FF1234  8  0A 0B 0C 0D 0E 0F 10 11 "
# Row 3727: DLC 0 - no data bytes, so no trailing byte-space beyond the fixed gap.
REFERENCE_ROW_3727 = "  3727)     24680.1  Rx         0080  0  "


def _read_trc_rows(path):
    """Tiny .trc reader: header comment lines start with ';'; everything
    else is a data row. Returns (header_lines, data_rows), line endings
    stripped but internal spacing intact."""
    header, rows = [], []
    with open(path, "r", encoding="ascii", newline="") as f:
        for raw_line in f:
            line = raw_line.rstrip("\r\n")
            (header if line.startswith(";") else rows).append(line)
    return header, rows


def test_format_row_matches_reference_capture_byte_for_byte():
    row = format_row(
        n=1, elapsed_ms=12.5, can_id=0x0123, extended=False, dlc=8,
        data=bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88]),
    )
    assert row.rstrip("\n") == REFERENCE_ROW_1


def test_format_row_matches_reference_extended_id_row():
    row = format_row(
        n=132, elapsed_ms=3456.7, can_id=0x18FF1234, extended=True, dlc=8,
        data=bytes([0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11]),
    )
    assert row.rstrip("\n") == REFERENCE_ROW_132


def test_format_row_matches_reference_zero_length_row():
    row = format_row(n=3727, elapsed_ms=24680.1, can_id=0x0080, extended=False, dlc=0, data=b"")
    assert row.rstrip("\n") == REFERENCE_ROW_3727


def test_written_file_round_trips_through_a_tiny_reader(tmp_path):
    out = tmp_path / "capture.trc"
    with TrcWriter(str(out)) as writer:
        writer._start_time = 0.0  # pin t=0 so elapsed_ms below is exact
        msg = can.Message(
            arbitration_id=0x0123, is_extended_id=False,
            data=bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88]),
            timestamp=0.0125,
        )
        writer.write_message(msg)

    header, rows = _read_trc_rows(str(out))
    assert header[0] == ";$FILEVERSION=1.1"
    assert any(line.startswith(";$STARTTIME=") for line in header)
    assert len(rows) == 1
    assert rows[0] == REFERENCE_ROW_1


def test_file_uses_crlf_line_endings(tmp_path):
    out = tmp_path / "capture.trc"
    with TrcWriter(str(out)):
        pass
    raw = out.read_bytes()
    assert b"\r\n" in raw
    assert raw.count(b"\n") == raw.count(b"\r\n")
