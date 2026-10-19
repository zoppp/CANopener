"""EDS folders: find the EDS/DCF/XDD files in folders the user picked, read the device identity each one describes
(vendor id, product code, revision of object 0x1018) and match them against the identity a node reports.

Only the identity is read here (a quick line scan), not the whole object dictionary, so a folder with hundreds of
files is indexed in a moment; results are cached per file (path, size, mtime).
"""

from __future__ import annotations

import hashlib
import io
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

SUFFIXES = (".eds", ".dcf", ".xdd")
MAX_FILES = 5000                  # a folder pick of C:\ must not take minutes
MAX_ENTRIES = 100_000             # nor the walk to find them: every file and folder looked at counts

_cache: dict[str, tuple[int, int, dict]] = {}


def _int(text) -> Optional[int]:
    """An EDS/XDD number: 0x.. hex or decimal (EDS decimals may have leading zeros)."""
    s = str(text or "").strip()
    try:
        return int(s, 16) if s.lower().startswith("0x") else int(s, 10)
    except ValueError:
        return None


def _eds_identity(text: str) -> dict:
    sections: dict[str, dict[str, str]] = {}
    cur = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            cur = sections.setdefault(line[1:-1].strip().lower(), {})
        elif cur is not None and "=" in line and not line.startswith(";"):
            k, v = line.split("=", 1)
            cur.setdefault(k.strip().lower(), v.strip())
    info = sections.get("deviceinfo", {})

    def pick(key: str, sub: int) -> Optional[int]:
        v = _int(info.get(key))
        return v if v is not None else _int(sections.get(f"1018sub{sub}", {}).get("defaultvalue"))
    return {"vendor": pick("vendornumber", 1), "product": pick("productnumber", 2),
            "revision": pick("revisionnumber", 3), "product_name": info.get("productname", ""),
            "vendor_name": info.get("vendorname", "")}


def _xdd_identity(data: bytes) -> dict:
    out = {"vendor": None, "product": None, "revision": None, "product_name": "", "vendor_name": ""}
    defaults: dict[int, Optional[int]] = {}
    in_1018 = False
    for ev, el in ET.iterparse(io.BytesIO(data), events=("start", "end")):
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "CANopenObject":
            in_1018 = ev == "start" and (el.get("index") or "").upper() == "1018"
        elif ev == "end" and tag == "CANopenSubObject" and in_1018:
            defaults[_int("0x" + (el.get("subIndex") or "x")) or 0] = _int(el.get("defaultValue"))
        elif ev == "end" and tag in ("vendorID", "productID", "vendorName", "productName"):
            key = {"vendorID": "vendor", "productID": "product", "vendorName": "vendor_name",
                   "productName": "product_name"}[tag]
            out[key] = _int(el.text) if key in ("vendor", "product") else (el.text or "").strip()
    for sub, key in ((1, "vendor"), (2, "product"), (3, "revision")):
        if out[key] is None:
            out[key] = defaults.get(sub)
    return out


def identity(path: Path) -> dict:
    """The identity a file describes: {path, name, vendor, product, revision, product_name, vendor_name, hash}
    (numbers None when the file does not give them), or {"error": ...} when it cannot be read."""
    st = path.stat()
    hit = _cache.get(str(path))
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    entry = {"path": str(path), "name": path.name}
    try:
        data = path.read_bytes()
        ident = _xdd_identity(data) if path.suffix.lower() == ".xdd" else _eds_identity(data.decode("latin-1"))
        entry.update(ident, hash=hashlib.sha1(data).hexdigest())
    except (OSError, ET.ParseError, ValueError) as exc:
        entry["error"] = str(exc)
    _cache[str(path)] = (st.st_mtime_ns, st.st_size, entry)
    return entry


def scan_folders(folders: list[str]) -> tuple[list[dict], list[str]]:
    """Every EDS/DCF/XDD file below the folders (subfolders too): (files, problems)."""
    files, problems, seen = [], [], set()
    for folder in folders:
        if not Path(folder).is_dir():
            problems.append(f"{folder}: folder not found")
            continue
        visited = 0
        for root, dirs, names in os.walk(folder):
            dirs.sort()
            visited += len(dirs) + len(names)
            if visited > MAX_ENTRIES:
                problems.append(f"{folder}: stopped after looking at {MAX_ENTRIES} files and folders; "
                                "pick narrower folders")
                break
            for n in sorted(names):
                p = Path(root) / n
                if p.suffix.lower() not in SUFFIXES or str(p).lower() in seen:
                    continue
                if len(files) >= MAX_FILES:
                    problems.append(f"stopped after {MAX_FILES} files; pick narrower folders")
                    return files, problems
                seen.add(str(p).lower())
                try:
                    e = identity(p)
                except OSError as exc:
                    problems.append(f"{p}: {exc}")
                    continue
                if "error" in e:
                    problems.append(f"{p.name}: {e['error']}")
                files.append(dict(e))          # a copy: callers add fields (the match)
    return files, problems


def match_kind(f: dict, dev: dict) -> str:
    """How well a file fits a device identity: exact (vendor + product + revision), product (vendor + product;
    the revision differs or one side does not give it), vendor (vendor only) or ""."""
    if f.get("vendor") is None or f.get("vendor") != dev.get("vendor"):
        return ""
    if f.get("product") is None or f.get("product") != dev.get("product"):
        return "vendor"
    if f.get("revision") is not None and f.get("revision") == dev.get("revision"):
        return "exact"
    return "product"


def best_match(files: list[dict], dev: dict) -> tuple[Optional[dict], list[dict]]:
    """The one file to assign to a device (None when there is none or the choice is not clear) and the files that
    fit vendor + product. Copies of the same file (same content) count once. A vendor-only fit is never picked:
    one vendor makes many products."""
    fits = [f for f in files if match_kind(f, dev) in ("exact", "product")]
    exact = [f for f in fits if match_kind(f, dev) == "exact"]
    for group in (exact, fits):
        if group:
            return (group[0] if len({f["hash"] for f in group}) == 1 else None), fits
    return None, fits


def clean_folders(folders) -> list[str]:
    out: list[str] = []
    for f in folders or []:
        s = os.path.normpath(str(f).strip()) if str(f).strip() else ""
        if s and s.lower() not in (x.lower() for x in out):
            out.append(s)
    return out
