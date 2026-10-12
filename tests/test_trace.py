import re

from fakeslave import FakeSlave

from canopener.core import trc as trace_mod

ROW_RE = re.compile(
    r"^\s*(\d+)\)\s+([\d.]+)\s+Rx\s+([0-9A-F]{4,8})\s+(\d)\s+((?:[0-9A-F]{2}\s?)*)$"
)


def test_trc_header_and_row_format(channel, network, tmp_path):
    slave = FakeSlave(channel, node_id=15, heartbeat_ms=50)
    out = tmp_path / "capture.trc"
    try:
        count = trace_mod.capture_trace(network, seconds=0.3, out_path=str(out))
        assert count > 0

        lines = out.read_text(encoding="ascii").splitlines()

        header_lines = [line for line in lines if line.startswith(";")]
        assert header_lines, "expected PCAN-View header lines starting with ';'"
        assert any("$FILEVERSION=1.1" in line for line in header_lines)

        data_lines = [line for line in lines if not line.startswith(";")]
        assert len(data_lines) == count
        for line in data_lines:
            m = ROW_RE.match(line)
            assert m, f"row didn't match the expected trc format: {line!r}"

        # Message numbers are sequential starting at 1.
        numbers = [int(ROW_RE.match(line).group(1)) for line in data_lines]
        assert numbers == list(range(1, count + 1))

        # At least one row should be the heartbeat from node 15 (0x700 + 15).
        ids = [ROW_RE.match(line).group(3) for line in data_lines]
        assert "070F" in ids
    finally:
        slave.close()
