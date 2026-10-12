import struct

import pytest

from canopener.core.canbin import (
    CanbinError,
    CanbinFrame,
    CanbinHeader,
    canbin_to_trc,
    read_canbin,
    write_canbin,
)
from canopener.core.trc import format_row


def _frame(us, can_id, extended=False, dlc=None, rtr=False, data=b""):
    if dlc is None:
        dlc = len(data)
    return CanbinFrame(us=us, can_id=can_id, extended=extended, dlc=dlc, rtr=rtr, data=data)


def test_round_trip_write_then_read(tmp_path):
    out = tmp_path / "capture.canbin"
    header = CanbinHeader(count=0, bitrate=250_000, start_s=1_726_000_000, start_us=123_456, channel=1)
    frames = [
        _frame(us=0, can_id=0x0123, data=bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88])),
        _frame(us=12_500, can_id=0x18FF1234, extended=True,
               data=bytes([0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11])),
        _frame(us=41_000, can_id=0x080, rtr=True, dlc=0),
    ]

    write_canbin(str(out), header, frames)
    read_header, read_frames = read_canbin(str(out))

    assert read_header.count == len(frames)
    assert read_header.bitrate == 250_000
    assert read_header.start_s == 1_726_000_000
    assert read_header.start_us == 123_456
    assert read_header.channel == 1

    assert len(read_frames) == 3
    assert read_frames[0].us == 0
    assert read_frames[0].can_id == 0x0123
    assert read_frames[0].extended is False
    assert read_frames[0].rtr is False
    assert read_frames[0].dlc == 8
    assert read_frames[0].data[:8] == bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88])

    assert read_frames[1].us == 12_500
    assert read_frames[1].can_id == 0x18FF1234
    assert read_frames[1].extended is True
    assert read_frames[1].data[:8] == bytes([0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11])

    assert read_frames[2].us == 41_000
    assert read_frames[2].can_id == 0x080
    assert read_frames[2].rtr is True
    assert read_frames[2].dlc == 0


def test_converted_file_rows_match_format_row_byte_for_byte(tmp_path):
    in_path = tmp_path / "capture.canbin"
    out_path = tmp_path / "capture.trc"

    header = CanbinHeader(count=0, bitrate=250_000, start_s=0, start_us=0, channel=0)
    frames = [
        _frame(us=12_500, can_id=0x0123, data=bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88])),
        _frame(us=3_456_700, can_id=0x18FF1234, extended=True,
               data=bytes([0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11])),
        _frame(us=24_680_100, can_id=0x0080, dlc=0),
    ]
    write_canbin(str(in_path), header, frames)

    count = canbin_to_trc(str(in_path), str(out_path))
    assert count == 3

    lines = out_path.read_text(encoding="ascii").splitlines()
    data_rows = [line for line in lines if not line.startswith(";")]
    assert len(data_rows) == 3

    expected = [
        format_row(1, 12.5, 0x0123, False, 8, bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88])).rstrip("\n"),
        format_row(2, 3456.7, 0x18FF1234, True, 8, bytes([0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11])).rstrip("\n"),
        format_row(3, 24680.1, 0x0080, False, 0, b"").rstrip("\n"),
    ]
    assert data_rows == expected


def test_truncated_trailing_frame_is_dropped_with_warning_not_an_error(tmp_path):
    # A power-loss cut mid-frame: the one frame present is no longer
    # complete. This must not raise - it's read as zero frames plus a
    # warning about the dropped partial frame.
    out = tmp_path / "capture.canbin"
    header = CanbinHeader(count=1, bitrate=250_000, start_s=0, start_us=0, channel=0)
    frames = [_frame(us=0, can_id=0x100, data=b"\x01\x02")]
    write_canbin(str(out), header, frames)

    # Chop off the last 5 bytes so the one frame is no longer complete.
    data = out.read_bytes()
    out.write_bytes(data[:-5])

    warnings = []
    read_header, read_frames = read_canbin(str(out), warn=warnings.append)
    assert read_frames == []
    assert read_header.count == 0
    assert any("only 0 are present" in w for w in warnings)
    assert any("trailing byte(s)" in w for w in warnings)


def test_bad_magic_raises_clear_error(tmp_path):
    out = tmp_path / "capture.canbin"
    out.write_bytes(b"NOTCANBIN" + b"\x00" * 23)

    with pytest.raises(CanbinError, match="magic"):
        read_canbin(str(out))


def test_hand_built_52_byte_file_exact_bytes(tmp_path):
    """One header (32 bytes) + one frame (20 bytes), built by hand from the
    format spec, with every field asserted exactly."""
    out = tmp_path / "handbuilt.canbin"

    header_bytes = (
        b"CANBIN01"                    # magic
        + struct.pack("<I", 1)         # count = 1
        + struct.pack("<I", 250_000)   # bitrate
        + struct.pack("<I", 1_726_000_000)  # start_s
        + struct.pack("<I", 500_000)   # start_us
        + bytes([2])                   # channel
        + b"\x00" * 7                  # reserved
    )
    assert len(header_bytes) == 32

    can_id = 0x123
    frame_bytes = (
        struct.pack("<I", 12_345)                     # us
        + struct.pack("<I", can_id)                   # id, standard (bit 31 clear)
        + bytes([4])                                   # dlc
        + bytes([0])                                   # flags (no RTR)
        + b"\x00\x00"                                   # reserved
        + bytes([0xDE, 0xAD, 0xBE, 0xEF, 0, 0, 0, 0])   # data
    )
    assert len(frame_bytes) == 20

    out.write_bytes(header_bytes + frame_bytes)
    assert out.stat().st_size == 52

    header, frames = read_canbin(str(out))
    assert header.count == 1
    assert header.bitrate == 250_000
    assert header.start_s == 1_726_000_000
    assert header.start_us == 500_000
    assert header.channel == 2

    assert len(frames) == 1
    frame = frames[0]
    assert frame.us == 12_345
    assert frame.can_id == can_id
    assert frame.extended is False
    assert frame.dlc == 4
    assert frame.rtr is False
    assert frame.data[:4] == bytes([0xDE, 0xAD, 0xBE, 0xEF])


def _patch_header_count(path, count):
    """Overwrite just the header's `count` field (offset 8, UDINT) - used to
    simulate a capture that was never patched at close (still 0) or whose
    header disagrees with what's actually on disk."""
    data = path.read_bytes()
    path.write_bytes(data[:8] + struct.pack("<I", count) + data[12:])


def test_count_zero_reads_frames_to_end_of_file(tmp_path):
    # A capture cut short before the header could be patched: count stays 0
    # but the frames streamed to disk are all still there.
    out = tmp_path / "capture.canbin"
    header = CanbinHeader(count=0, bitrate=250_000, start_s=0, start_us=0, channel=0)
    frames = [
        _frame(us=0, can_id=0x100, data=b"\x01\x02"),
        _frame(us=1_000, can_id=0x200, data=b"\x03\x04\x05"),
        _frame(us=2_000, can_id=0x300, rtr=True, dlc=0),
    ]
    write_canbin(str(out), header, frames)
    _patch_header_count(out, 0)

    warnings = []
    read_header, read_frames = read_canbin(str(out), warn=warnings.append)

    assert read_header.count == 3
    assert len(read_frames) == 3
    assert [f.can_id for f in read_frames] == [0x100, 0x200, 0x300]
    assert warnings == []


def test_declared_count_higher_than_frames_present_reads_what_is_there(tmp_path):
    # Header says 10 frames but only 7 were actually written before the
    # capture was cut short.
    out = tmp_path / "capture.canbin"
    header = CanbinHeader(count=0, bitrate=250_000, start_s=0, start_us=0, channel=0)
    frames = [_frame(us=i * 1_000, can_id=0x100 + i, data=bytes([i])) for i in range(7)]
    write_canbin(str(out), header, frames)
    _patch_header_count(out, 10)

    warnings = []
    read_header, read_frames = read_canbin(str(out), warn=warnings.append)

    assert read_header.count == 7
    assert len(read_frames) == 7
    assert any("declares 10 frame(s) but only 7 are present" in w for w in warnings)


def test_trailing_partial_frame_ignored_with_warning(tmp_path):
    # count == 0 (unpatched header) plus two complete frames plus a few
    # extra bytes from a frame that was still being written when power was
    # lost.
    out = tmp_path / "capture.canbin"
    header = CanbinHeader(count=0, bitrate=250_000, start_s=0, start_us=0, channel=0)
    frames = [
        _frame(us=0, can_id=0x100, data=b"\x01\x02"),
        _frame(us=1_000, can_id=0x200, data=b"\x03\x04\x05"),
    ]
    write_canbin(str(out), header, frames)

    with open(out, "ab") as f:
        f.write(b"\x00" * 6)  # a partial 20-byte frame, only 6 bytes in

    warnings = []
    read_header, read_frames = read_canbin(str(out), warn=warnings.append)

    assert read_header.count == 2
    assert len(read_frames) == 2
    assert any("6 trailing byte(s)" in w for w in warnings)


def test_canbin_to_trc_passes_warnings_through(tmp_path):
    in_path = tmp_path / "capture.canbin"
    out_path = tmp_path / "capture.trc"

    header = CanbinHeader(count=0, bitrate=250_000, start_s=0, start_us=0, channel=0)
    frames = [_frame(us=0, can_id=0x100, data=b"\x01\x02")]
    write_canbin(str(in_path), header, frames)
    _patch_header_count(in_path, 5)

    warnings = []
    count = canbin_to_trc(str(in_path), str(out_path), warn=warnings.append)

    assert count == 1
    assert out_path.exists()
    assert any("declares 5 frame(s) but only 1 are present" in w for w in warnings)
