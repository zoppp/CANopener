"""Shared low-level SDO value parsing/packing helpers (used by setid.py and
baud.py so the recipe-driven SDO writes are packed the same way everywhere).
"""

from __future__ import annotations

import struct
from typing import Tuple, Union

STRUCT_FORMATS = {
    "u8": "<B", "u16": "<H", "u32": "<I",
    "i8": "<b", "i16": "<h", "i32": "<i",
}

#: The magic value CANopen devices use for "save"/'evas' little-endian at
#: 0x1010 (ASCII "save" == 0x65766173, per CiA 301 store parameters).
STORE_SAVE_VALUE = 0x65766173


def parse_int(value: Union[int, str]) -> int:
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return int(text, 16) if text.lower().startswith("0x") else int(text)


def pack(type_name: str, value: int) -> bytes:
    try:
        fmt = STRUCT_FORMATS[type_name]
    except KeyError:
        raise ValueError(f"unknown SDO value type {type_name!r}; use one of {sorted(STRUCT_FORMATS)}")
    return struct.pack(fmt, value)


def parse_store_target(store: str) -> Tuple[int, int]:
    """Parse a `"store"` recipe field like `"0x1010:01"` into (index, subindex)."""
    index_str, sub_str = store.split(":")
    return parse_int(index_str), parse_int(sub_str)
