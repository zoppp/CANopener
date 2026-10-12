"""Run the `canopen` library on top of a CANopener channel.

The channel's receive thread is the only reader of the adapter. A BridgeBus is a python-can bus whose
recv() takes frames from a Tap on the channel and whose send() goes through Channel.send(), so everything
the node workspace transmits also shows in the trace, the receive table and a running recording.
"""

from __future__ import annotations

from typing import Optional

import can
import canopen

from ..core.channel import F_ERR, Channel, Tap, frame_to_message


def _no_errors(can_id: int, flags: int) -> bool:
    return not flags & F_ERR


class BridgeBus(can.BusABC):
    def __init__(self, channel: Channel, **kwargs):
        self._ch = channel
        self._tap = channel.add_tap(Tap(maxlen=100_000, include_tx=False, accept=_no_errors))
        self.channel_info = f"CANopener {channel.name}"
        super().__init__(channel=channel.name, **kwargs)

    def _recv_internal(self, timeout: Optional[float]):
        fr = self._tap.get(timeout)
        if fr is None:
            return None, False
        ts, can_id, flags, dlc, data = fr
        return frame_to_message(can_id, flags, data, dlc, ts), False

    def send(self, msg: can.Message, timeout: Optional[float] = None) -> None:
        self._ch.send(msg)

    def shutdown(self) -> None:
        self._ch.remove_tap(self._tap)
        super().shutdown()


def open_network(channel: Channel) -> canopen.Network:
    """A connected canopen.Network on `channel`. Call network.disconnect() when done (the channel stays open)."""
    network = canopen.Network(bus=BridgeBus(channel))
    network.NOTIFIER_SHUTDOWN_TIMEOUT = 2.0
    network.connect()
    return network
