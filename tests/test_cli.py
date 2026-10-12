import json

from fakeslave import FakeSlave

from canopener import cli
from canopener.core.canbin import CanbinFrame, CanbinHeader, write_canbin


def test_cli_trace_convert(tmp_path, capsys):
    in_path = tmp_path / "capture.canbin"
    out_path = tmp_path / "capture.trc"
    write_canbin(
        str(in_path),
        CanbinHeader(count=0, bitrate=250_000, start_s=0, start_us=0, channel=0),
        [CanbinFrame(us=100, can_id=0x123, extended=False, dlc=2, rtr=False, data=b"\x01\x02")],
    )

    rc = cli.main(["trace", "convert", str(in_path), str(out_path)])
    assert rc == 0

    out = capsys.readouterr().out
    assert "Wrote 1 frame(s)" in out
    lines = out_path.read_text(encoding="ascii").splitlines()
    data_rows = [line for line in lines if not line.startswith(";")]
    assert len(data_rows) == 1


def test_cli_scan_json_finds_node(channel, capsys):
    slave = FakeSlave(channel, node_id=42, heartbeat_ms=50)
    try:
        rc = cli.main([
            "scan", "--interface", "virtual", "--channel", channel,
            "--seconds", "0.3", "--sdo-timeout", "0.01", "--json",
        ])
        assert rc == 0

        out = capsys.readouterr().out
        data = json.loads(out)
        node_ids = [n["node_id"] for n in data["nodes"]]
        assert 42 in node_ids
        node = next(n for n in data["nodes"] if n["node_id"] == 42)
        assert node["device_name"] == "Demo device A (test)"
    finally:
        slave.close()
