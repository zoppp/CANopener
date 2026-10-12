import time

from canopener.decode.signals import SignalStore


def test_publish_changed_history_clear():
    s = SignalStore()
    now = time.time()
    for i in range(10):
        s.publish("dbc/EEC1/EngineSpeed", now - 1 + i * 0.1, 800 + i, unit="rpm")
    s.publish("dbc/EEC1/Mode", now, "RUN")
    seq, ch = s.changed(0)
    assert {c["key"] for c in ch} == {"dbc/EEC1/EngineSpeed", "dbc/EEC1/Mode"}
    assert s.changed(seq)[1] == []
    assert len(s.history("dbc/EEC1/EngineSpeed", 5)) == 10 and s.history("dbc/EEC1/Mode") == []
    assert s.get("dbc/EEC1/EngineSpeed")["unit"] == "rpm"
    gen = s.generation
    s.clear("dbc/")
    assert s.list() == [] and s.generation == gen + 1


def test_history_thinning_keeps_newest():
    s = SignalStore()
    now = time.time()
    for i in range(1000):
        s.publish("k", now - 1 + i / 1000, i)
    pts = s.history("k", 10, max_points=100)
    assert len(pts) == 100 and pts[-1][1] == 999
