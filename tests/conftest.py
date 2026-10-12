import uuid

import can
import canopen
import pytest


@pytest.fixture
def channel():
    """A fresh virtual-bus channel name so tests don't see each other's traffic."""
    return f"canopener-test-{uuid.uuid4()}"


@pytest.fixture
def network(channel):
    """A connected canopen.Network for the tool side, sharing `channel` with
    whatever FakeSlave the test starts on it."""
    bus = can.Bus(interface="virtual", channel=channel, bitrate=250_000)
    net = canopen.Network(bus=bus)
    net.connect()
    yield net
    net.disconnect()


@pytest.fixture
def engine():
    """A CANopener Engine; tests connect channels on their own virtual channel names."""
    from canopener.core.engine import Engine
    eng = Engine()
    yield eng
    eng.shutdown()


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Logs, recordings and user recipes go to a temp folder, never into the repo."""
    monkeypatch.setenv("CANOPENER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CANOPENER_USER_RECIPES", str(tmp_path / "home" / "user-recipes.json"))
