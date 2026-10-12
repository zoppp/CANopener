"""Saved dashboards: {id, name, widgets: [{id, type, x, y, w, h, title, ...}]} in svc.settings["dashboards"]
(and in projects). The server checks the frame (name, widget types, grid position); each widget's own options
(signal keys, colours, thresholds) are the UI's business and stored as given."""

from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Optional

from ..service import ServiceError, safe_name

log = logging.getLogger(__name__)
WIDGET_TYPES = ("plot", "xy", "gauge", "number", "lamp")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
COLS = 12                    # grid columns
MAX_WIDGETS = 100
MAX_BYTES = 200_000          # one dashboard as JSON


def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        v = int(v)
    except (TypeError, ValueError, OverflowError):
        v = default
    return max(lo, min(hi, v))


def normalize(d: dict) -> dict:
    if not isinstance(d, dict):
        raise ServiceError("a dashboard must be an object")
    name = safe_name(d.get("name", ""), "dashboard name")
    widgets = d.get("widgets") or []
    if not isinstance(widgets, list) or len(widgets) > MAX_WIDGETS:
        raise ServiceError(f"widgets must be a list of at most {MAX_WIDGETS}")
    out = []
    for w in widgets:
        if not isinstance(w, dict) or w.get("type") not in WIDGET_TYPES:
            raise ServiceError(f"unknown widget type {w.get('type') if isinstance(w, dict) else w!r}; "
                               f"use {', '.join(WIDGET_TYPES)}")
        w = dict(w)
        w["id"] = str(w.get("id") or uuid.uuid4().hex[:8])
        w["x"] = _int(w.get("x"), 0, 0, COLS - 1)
        w["w"] = _int(w.get("w"), 4, 1, COLS - w["x"])
        w["y"] = _int(w.get("y"), 0, 0, 1000)
        w["h"] = _int(w.get("h"), 4, 1, 40)
        out.append(w)
    did = str(d.get("id") or uuid.uuid4().hex[:8])
    if not _ID_RE.match(did) or any(not _ID_RE.match(w["id"]) for w in out):
        raise ServiceError("dashboard and widget ids are letters, digits, _ and - (up to 40)")
    dash = {"id": did, "name": name, "widgets": out}
    try:
        text = json.dumps(dash, allow_nan=False)
    except ValueError:
        raise ServiceError("a dashboard value is not a finite number (NaN or infinity)") from None
    if len(text) > MAX_BYTES:
        raise ServiceError("dashboard too large")
    return dash


def clean_list(dashes) -> list:
    """The valid dashboards of a settings or project value (anything else is dropped, with a log line)."""
    out, names = [], set()
    for d in dashes if isinstance(dashes, list) else []:
        try:
            dash = normalize(d)
        except ServiceError as exc:
            log.warning("dropped dashboard: %s", exc)
            continue
        if dash["name"] not in names:
            names.add(dash["name"])
            out.append(dash)
    return out


def list_all(svc) -> list[dict]:
    return list(svc.settings.get("dashboards") or [])


def find(svc, id_or_name: str, by_name: bool = True) -> Optional[dict]:
    """By id; by name only when no id matches and by_name is set (reads; writes and deletes use the id)."""
    for d in list_all(svc):
        if d.get("id") == id_or_name:
            return d
    if by_name:
        for d in list_all(svc):
            if d.get("name") == id_or_name:
                return d
    return None


def save(svc, d: dict) -> dict:
    """Create (no/unknown id) or replace a dashboard. Names are unique."""
    dash = normalize(d)
    others = [x for x in list_all(svc) if x.get("id") != dash["id"]]
    if any(x.get("name") == dash["name"] for x in others):
        raise ServiceError(f"there is already a dashboard named {dash['name']!r}")
    old = list_all(svc)
    idx = next((i for i, x in enumerate(old) if x.get("id") == dash["id"]), None)
    if idx is None:
        old.append(dash)
    else:
        old[idx] = dash
    svc.settings["dashboards"] = old
    svc.save_settings()
    return dash


def delete(svc, id_or_name: str, by_name: bool = True) -> None:
    d = find(svc, id_or_name, by_name)
    if d is None:
        raise ServiceError(f"no dashboard {id_or_name!r}")
    svc.settings["dashboards"] = [x for x in list_all(svc) if x is not d]
    svc.save_settings()
