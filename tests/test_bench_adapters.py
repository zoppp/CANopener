"""The hardware adapter bench (tests/hardware/bench_adapters.py) runs end to end on the virtual bus."""

import importlib.util
import json
import sys
from pathlib import Path

_PATH = Path(__file__).parent / "hardware" / "bench_adapters.py"
_spec = importlib.util.spec_from_file_location("bench_adapters", _PATH)
bench = sys.modules["bench_adapters"] = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)


def test_bench_passes_on_the_virtual_bus(tmp_path):
    code = bench.main(["--ref", "virtual:bench-self-test", "--dut", "virtual:bench-self-test", "--yes", "--fd",
                       "--rates", "125k,1M", "--load-seconds", "0.3", "--out", str(tmp_path)])
    report = json.loads(next(tmp_path.glob("*.json")).read_text(encoding="utf-8"))
    by_status = {}
    for r in report["results"]:
        by_status.setdefault(r["status"], []).append(r["check"])
    assert code == 0, by_status.get("FAIL")
    assert len(by_status["PASS"]) >= 12
    assert "listen-only sends nothing (no ACK)" in by_status["SKIP"]
    assert next(tmp_path.glob("*.md")).read_text(encoding="utf-8").startswith("# Adapter bench")


def test_specs_and_rates():
    assert bench.parse_spec("obdlink:socket://192.168.0.10:35000") == ("obdlink", "socket://192.168.0.10:35000")
    assert bench.parse_spec("kvaser") == ("kvaser", None)
    assert bench.parse_spec("none") is None
    assert [bench.parse_rate(x) for x in ("250k", "1M", "83.3k", "500000")] == [250_000, 1_000_000, 83_333, 500_000]
