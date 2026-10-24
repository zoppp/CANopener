"""CAN adapters: which backends exist, what is plugged in, and how to open a python-can bus.

Every backend goes through python-can. This module only knows each backend's quirks:
which keyword turns on listen-only, how CAN FD is configured, what its default channel is,
and how to find devices that are plugged in (CANable boards in particular, which come with
either slcan firmware, a COM port, or candleLight firmware, a WinUSB device).
Two backends are CANopener's own python-can buses (not in python-can): GVRET (gvret.py) and OBDLink (obdlink.py).
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

import can

log = logging.getLogger(__name__)

#: Nominal bit rates offered in the UI (bit/s).
BITRATES = (10_000, 20_000, 50_000, 83_333, 100_000, 125_000, 250_000, 500_000, 800_000, 1_000_000)
#: CAN FD data-phase bit rates offered in the UI (bit/s).
DATA_BITRATES = (1_000_000, 2_000_000, 4_000_000, 5_000_000, 8_000_000)


@dataclass(frozen=True)
class Backend:
    name: str                      # python-can interface name
    title: str
    default_channel: Any
    listen_only: bool              # can be opened listen-only
    fd: bool                       # CAN FD possible with this backend
    needs: str = ""                # what must be installed for it to work
    bitrate_kwarg: bool = True     # False: the OS sets the bit rate (socketcan)


BACKENDS: dict[str, Backend] = {b.name: b for b in (
    Backend("pcan", "PEAK PCAN-USB", "PCAN_USBBUS1", True, True, "PEAK driver (PCANBasic.dll)"),
    Backend("kvaser", "Kvaser", 0, True, True, "Kvaser driver (canlib32.dll)"),
    Backend("slcan", "CANable / slcan (COM port)", "COM3", True, True, "serial port; FD data rate 2 or 5 Mbit/s"),
    Backend("gs_usb", "CANable / candleLight (USB)", 0, False, False, "candleLight firmware (WinUSB)"),
    Backend("vector", "Vector", 0, True, True, "Vector XL driver library"),
    Backend("ixxat", "IXXAT", 0, False, True, "IXXAT VCI driver"),
    Backend("gvret", "GVRET (ESP32RET, Macchina M2) - untested", "COM3", True, False,
            "COM port or socket://<ip>:23 for WiFi; USB-UART boards at 1 Mbaud"),
    Backend("obdlink", "OBDLink MX/LX/SX/EX (OBD-II) - untested", "COM3", True, False,
            "Bluetooth or USB COM port, or socket://<ip>:35000 for WiFi; sending pauses receiving"),
    Backend("socketcan", "Linux SocketCAN", "can0", False, True, "Linux only", bitrate_kwarg=False),
    Backend("virtual", "Virtual (no hardware)", "canopener", False, True, ""),
)}

SUPPORTED_INTERFACES = tuple(BACKENDS)
DEFAULT_CHANNELS = {name: b.default_channel for name, b in BACKENDS.items()}

# USB ids of CANable-style boards.
_SLCAN_USB_IDS = {(0x16D0, 0x117E): "CANable (slcan)", (0xAD50, 0x60C4): "CANable 2 (slcan)",
                  (0x0483, 0x5740): "STM32 virtual COM port (CANable clone with slcan?)"}
# Our own bus classes, opened directly: python-can's can.Bus() only accepts its registered interfaces.
_OWN_BUSES = {"gvret": ("canopener.core.gvret", "GvretBus"), "obdlink": ("canopener.core.obdlink", "ObdlinkBus")}
_GS_USB_IDS = {(0x1D50, 0x606F): "candleLight / gs_usb", (0x1209, 0x2323): "candleLight (budgetcan)",
               (0x1CD2, 0x606F): "CANable-MKS (gs_usb)", (0x16D0, 0x10B8): "CANtact Pro (gs_usb)"}


def enable_bundled_libusb() -> str | None:
    """Make pyusb find libusb-1.0.dll (shipped by the libusb-package wheel), for gs_usb on Windows.

    pyusb looks the library up with ctypes.util.find_library, which searches PATH on Windows."""
    try:
        import libusb_package
    except ImportError:
        return None
    try:
        path = libusb_package.get_library_path()
    except Exception:                              # no binary for this platform
        return None
    if not path:
        return None
    folder = os.path.dirname(str(path))
    if folder not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
    if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(folder)
        except OSError:
            pass
    return str(path)


@dataclass
class ChannelConfig:
    """Everything needed to open one channel."""
    interface: str = "virtual"
    channel: Any = None
    bitrate: int = 250_000
    fd: bool = False
    data_bitrate: int = 2_000_000
    listen_only: bool = False
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ChannelConfig":
        known = {k: d[k] for k in ("interface", "channel", "bitrate", "fd", "data_bitrate", "listen_only", "extra")
                 if k in d}
        from .transmit import parse_bool          # (transmit imports channel, which imports this module)
        for k in ("fd", "listen_only"):
            if k in known:
                known[k] = parse_bool(known[k], k)      # "false" must not open in FD or listen-only mode
        cfg = cls(**known)
        if cfg.channel in ("", None):
            cfg.channel = None
        return cfg


# Rates each backend's driver table accepts (python-can 4.6). A rate missing here is refused, never silently
# replaced (python-can's PCAN backend falls back to 500 kbit/s for an unknown rate). None: any rate works.
_BACKEND_RATES: dict[str, Optional[set]] = {
    "pcan": {5000, 10000, 20000, 33000, 47000, 50000, 83000, 95000, 100000, 125000, 250000, 500000, 800000,
             1000000},
    "slcan": {10000, 20000, 50000, 83300, 100000, 125000, 250000, 500000, 750000, 1000000},
    "ixxat": {10000, 20000, 50000, 100000, 125000, 250000, 500000, 666666, 800000, 1000000},
}
# The nominal 83.333 kbit/s rate under each backend's own name for it.
_RATE_ALIASES = {"pcan": {83_333: 83_000}, "kvaser": {83_333: 83_000}, "slcan": {83_333: 83_300}}
_KVASER_PRESETS = {1000000, 500000, 250000, 125000, 100000, 83000, 62000, 50000, 10000}


def backend_bitrate(interface: str, bitrate: int) -> int:
    """The rate value to hand to the backend, or ValueError if its driver cannot do it."""
    rate = _RATE_ALIASES.get(interface, {}).get(int(bitrate), int(bitrate))
    allowed = _BACKEND_RATES.get(interface)
    if allowed is not None and rate not in allowed:
        nice = ", ".join(f"{r // 1000 if r % 1000 == 0 else r / 1000}k" for r in sorted(allowed))
        raise ValueError(f"{BACKENDS[interface].title} cannot run at {bitrate} bit/s; it supports {nice}")
    return rate


def _classic_timing(f_clock: int, bitrate: int) -> can.BitTiming:
    """Bit timing for a rate without a driver preset, at the closest usual CANopen sample point."""
    for sp in (87.5, 85.0, 80.0, 75.0, 70.0, 68.0, 65.0):
        try:
            return can.BitTiming.from_sample_point(f_clock=f_clock, bitrate=bitrate, sample_point=sp)
        except ValueError:
            continue
    raise ValueError(f"no bit timing for {bitrate} bit/s on a {f_clock // 1_000_000} MHz clock")


def _channel_value(interface: str, channel: Any) -> Any:
    if channel is None or channel == "":
        return DEFAULT_CHANNELS[interface]
    if interface in ("kvaser", "gs_usb", "vector", "ixxat") and isinstance(channel, str) and channel.strip().isdigit():
        return int(channel.strip())
    return channel


def bus_kwargs(cfg: ChannelConfig) -> dict:
    """The keyword arguments for can.Bus() for this configuration (separate so tests can check them)."""
    if cfg.interface not in BACKENDS:
        raise ValueError(f"Unsupported interface {cfg.interface!r}; choose one of {', '.join(SUPPORTED_INTERFACES)}")
    be = BACKENDS[cfg.interface]
    kw: dict = {"interface": cfg.interface, "channel": _channel_value(cfg.interface, cfg.channel)}
    if cfg.fd and not be.fd:
        raise ValueError(f"{be.title} does not support CAN FD in this tool")
    if cfg.listen_only and not be.listen_only and cfg.interface != "virtual":
        raise ValueError(f"{be.title} cannot be opened listen-only")

    if be.bitrate_kwarg and cfg.interface != "virtual":
        kw["bitrate"] = backend_bitrate(cfg.interface, cfg.bitrate)
    i = cfg.interface
    if i == "pcan":
        if cfg.fd:
            kw.pop("bitrate", None)
            kw["timing"] = can.BitTimingFd.from_sample_point(
                f_clock=80_000_000, nom_bitrate=cfg.bitrate, nom_sample_point=80.0,
                data_bitrate=cfg.data_bitrate, data_sample_point=80.0)
        if cfg.listen_only:
            kw["state"] = can.BusState.PASSIVE
    elif i == "kvaser":
        if cfg.fd:                                 # any rate pair, not only canlib's FD presets
            kw.pop("bitrate", None)
            kw["timing"] = can.BitTimingFd.from_sample_point(
                f_clock=80_000_000, nom_bitrate=cfg.bitrate, nom_sample_point=80.0,
                data_bitrate=cfg.data_bitrate, data_sample_point=80.0)
        elif kw["bitrate"] not in _KVASER_PRESETS:  # 20k, 800k, ...: explicit timing on the 16 MHz clock
            kw.pop("bitrate")
            kw["timing"] = _classic_timing(16_000_000, cfg.bitrate)
        if cfg.listen_only:
            kw["driver_mode"] = False              # canlib DRIVER_MODE_SILENT
    elif i == "slcan":
        kw.setdefault("tty_baudrate", 115200)
        if cfg.fd:
            if cfg.data_bitrate not in (2_000_000, 5_000_000):
                raise ValueError("slcan (CANable 2) supports CAN FD data rates of 2 or 5 Mbit/s only")
            kw.pop("bitrate", None)
            kw["timing"] = can.BitTimingFd.from_sample_point(
                f_clock=80_000_000, nom_bitrate=cfg.bitrate, nom_sample_point=80.0,
                data_bitrate=cfg.data_bitrate, data_sample_point=80.0)
        if cfg.listen_only:
            kw["listen_only"] = True
    elif i == "gs_usb":
        enable_bundled_libusb()
    elif i == "vector":
        if cfg.fd:
            kw["fd"] = True
            kw["data_bitrate"] = cfg.data_bitrate
        if cfg.listen_only:
            kw["listen_only"] = True
        # no app_name: the channel is the driver's global channel index (as detect_devices() reports it),
        # so nothing has to be set up in Vector Hardware Config first
        kw.setdefault("app_name", None)
    elif i == "ixxat":
        if cfg.fd:
            kw["fd"] = True
            kw["data_bitrate"] = cfg.data_bitrate
    elif i == "socketcan":
        if cfg.fd:
            kw["fd"] = True
    elif i in ("gvret", "obdlink"):
        # "COM6@1": GVRET's second CAN port; "COM7@ms": OBDLink on the MS-CAN (or "sw") pins
        if isinstance(kw["channel"], str) and "@" in kw["channel"]:
            kw["channel"], sub = kw["channel"].rsplit("@", 1)
            if i == "gvret":
                if sub not in ("0", "1"):
                    raise ValueError(f"GVRET port after '@' must be 0 or 1, not {sub!r}")
                kw["can_bus"] = int(sub)
            else:
                kw["can_pins"] = sub.lower()
        if cfg.listen_only:
            kw["listen_only"] = True
    elif i == "virtual":
        kw["receive_own_messages"] = False
    kw.update(cfg.extra or {})
    return kw


def open_bus(cfg: ChannelConfig) -> can.BusABC:
    kw = bus_kwargs(cfg)
    if kw["interface"] in _OWN_BUSES:
        module, cls = _OWN_BUSES[kw.pop("interface")]
        return getattr(importlib.import_module(module), cls)(**kw)
    return can.Bus(**kw)


def build_bus(interface: str, channel: Any, bitrate: int, **extra) -> can.BusABC:
    """Open a classic-CAN bus from CLI-style options (the original command-line signature)."""
    return open_bus(ChannelConfig(interface=interface, channel=channel, bitrate=bitrate, extra=extra))


# ---------------------------------------------------------------------------------------------- detection

@dataclass
class Device:
    interface: str
    channel: Any
    label: str
    detail: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _serial_devices() -> list[Device]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    out = []
    for p in list_ports.comports():
        key = (p.vid, p.pid) if p.vid is not None else None
        desc = (p.description or "").lower()
        if key in _SLCAN_USB_IDS or "canable" in desc or "slcan" in desc:
            name = _SLCAN_USB_IDS.get(key, "slcan device")
            out.append(Device("slcan", p.device, f"{name} on {p.device}", p.description or ""))
    return out


def _gs_usb_devices() -> list[Device]:
    enable_bundled_libusb()
    try:
        import usb.core
    except ImportError:
        return []
    out = []
    try:
        found = list(usb.core.find(find_all=True,
                                   custom_match=lambda d: (d.idVendor, d.idProduct) in _GS_USB_IDS))
    except Exception as exc:                       # no libusb backend
        log.info("gs_usb scan skipped: %s", exc)
        return []
    for index, d in enumerate(found):
        name = _GS_USB_IDS.get((d.idVendor, d.idProduct), "gs_usb")
        out.append(Device("gs_usb", index, f"{name} #{index}", f"USB {d.idVendor:04X}:{d.idProduct:04X}"))
    return out


def _driver_devices(names: tuple[str, ...]) -> list[Device]:
    out = []
    for name in names:
        try:
            configs = can.detect_available_configs(interfaces=[name])
        except Exception as exc:                   # driver DLL missing
            log.info("%s detection failed: %s", name, exc)
            continue
        for c in configs:
            ch = c.get("channel_index", c.get("channel")) if name == "vector" else c.get("channel")
            dev = c.get("device_name")
            if dev:                                # Kvaser: "Kvaser Leaf Light v2" / "Kvaser Virtual CAN Driver"
                label = f"{dev} ch {c.get('dongle_channel', ch)} (#{ch})"
            else:
                label = f"{BACKENDS[name].title} {ch}"
            if c.get("supports_fd"):
                label += " (FD)"
            serial = c.get("serial")
            out.append(Device(name, ch, label, f"S/N {serial}" if serial else ""))
    return out


def detect_devices(include_virtual: bool = True) -> list[Device]:
    """Adapters that are plugged in right now. Never raises: a missing driver just finds nothing."""
    devices = _serial_devices() + _gs_usb_devices() + _driver_devices(("pcan", "kvaser", "vector", "ixxat"))
    if include_virtual:
        devices.append(Device("virtual", "canopener", "Virtual bus (no hardware)", "for trying things out"))
    return devices


def device_present(cfg: ChannelConfig) -> Optional[bool]:
    """Is the adapter behind this configuration still plugged in? None: cannot tell (then assume yes).

    python-can's gs_usb (and some PCAN/Vector paths) never raise when the adapter is pulled, they just stop
    delivering frames, so the receive thread asks this when the bus has been quiet for a while. Deliberately
    conservative: False only when the device is certainly gone."""
    i, ch = cfg.interface, _channel_value(cfg.interface, cfg.channel)
    try:
        if i == "slcan":
            from serial.tools import list_ports
            return str(ch).upper() in {p.device.upper() for p in list_ports.comports()}
        if i == "gs_usb":
            return int(ch) < len(_gs_usb_devices())
        if i in ("pcan", "kvaser", "vector", "ixxat"):
            return bool(can.detect_available_configs(interfaces=[i]))
    except Exception as exc:
        log.info("presence check of %s %s failed: %s", i, ch, exc)
    return None


def backends_info() -> list[dict]:
    return [asdict(b) for b in BACKENDS.values()]
