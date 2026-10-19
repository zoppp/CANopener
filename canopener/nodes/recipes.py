"""Recipes: how to change a device's node ID and bit rate, picked by its vendor id (0x1018:01).

Two sources, merged (user wins):
- the bundled recipes.json (verified vendor recipes),
- a user recipes file, written by "Save as recipe" in the node workspace (CANOPENER_USER_RECIPES, or
  recipes/user-recipes.json next to the app).

A recipe is one of:
  {"method": "lss", ...}                                  CiA 305 LSS selective by identity
  {"method": "sdo", "steps": [...], "store": "none"|"0x1010:01"|..., "effect": "immediate"|"nmt_reset"|
   "power_cycle", "node_id_range": [lo, hi], "verify_on_new_id": bool, "baud_sdo": {...}}
  {"method": "sequence", "steps": [sequence steps], "baud_steps": [...], "node_id_range": ..., "effect": ...}
                                                          explicit steps, as saved from the custom mode
  {"method": "readonly", "note": "..."}

`recipes.json` sections: "default" (nothing else matches), "vendors" (keyed by vendor id, hex string), and
"unknown" (never picked automatically, only by name).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Union

from ..paths import app_dir

DEFAULT_RECIPES_PATH = Path(__file__).with_name("recipes.json")
PRIVATE_RECIPES_PATH = Path(__file__).with_name("recipes-private.json")   # private builds only (not published)


def user_recipes_path() -> Path:
    env = os.environ.get("CANOPENER_USER_RECIPES")
    return Path(env) if env else app_dir() / "recipes" / "user-recipes.json"


def load_user_recipes(path: Optional[Path] = None) -> dict:
    p = path or user_recipes_path()
    if not p.is_file():
        return {"vendors": {}}
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("vendors", {})
    return data


def load_recipes(path: Optional[Union[str, Path]] = None, include_user: bool = True) -> dict:
    p = Path(path) if path else DEFAULT_RECIPES_PATH
    with open(p, "r", encoding="utf-8") as f:
        recipes = json.load(f)
    if path is None and PRIVATE_RECIPES_PATH.is_file():
        with open(PRIVATE_RECIPES_PATH, "r", encoding="utf-8") as f:
            private = json.load(f)
        recipes.setdefault("vendors", {}).update(private.get("vendors", {}))
        recipes.setdefault("unknown", {}).update(private.get("unknown", {}))
    if include_user and path is None:
        try:
            user = load_user_recipes()
        except (OSError, ValueError):
            user = {"vendors": {}}
        vendors = dict(recipes.get("vendors", {}))
        for key, r in user.get("vendors", {}).items():
            # user fields win over the bundled recipe of the same vendor id ("0xca0" matches "0x0CA0"), so a
            # saved bit-rate sequence alone keeps the bundled node-id recipe
            base = {}
            for existing in list(vendors):
                if _int_key(existing) == _int_key(key):
                    base = vendors.pop(existing)
            merged = {**base, **r, "user": True}
            if "product_code" not in r:            # the bundled model's code must not carry the user's steps
                merged.pop("product_code", None)   # to that model only: vendor-wide, so the preview warns
            merged.setdefault("method", recipes.get("default", {}).get("method", "lss"))
            merged.setdefault("source", "user recipe")
            vendors[key] = merged
        recipes["vendors"] = vendors
    return recipes


def save_user_recipe(vendor_id: int, recipe: dict, path: Optional[Path] = None) -> Path:
    p = path or user_recipes_path()
    data = load_user_recipes(p)
    key = f"0x{vendor_id:X}"
    for existing in list(data["vendors"]):
        if _int_key(existing) == vendor_id:
            del data["vendors"][existing]
    data["vendors"][key] = recipe
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


def _int_key(key) -> int:
    if isinstance(key, int):
        return key
    return int(key, 16) if str(key).lower().startswith("0x") else int(key)


def _by_name(recipes: dict) -> dict:
    names = {}
    for key, recipe in recipes.get("vendors", {}).items():
        names[str(recipe.get("name", key)).lower()] = recipe
    for key, recipe in recipes.get("unknown", {}).items():
        names[key.lower()] = recipe
    return names


def resolve_recipe(recipes: dict, vendor_id: Optional[int], override: Optional[str] = None,
                   product_code: Optional[int] = None) -> dict:
    """Pick a recipe.

    :param vendor_id: value read from 0x1018:01, or None if unknown
    :param product_code: 0x1018:02; a recipe with a "product_code" field only applies to that product
    :param override: None for automatic (by vendor id), or one of
        "lss", "readonly", "sdo:<name>" where <name> is a recipe's "name"
        field or its key under "unknown" in recipes.json.
    """
    if override:
        if override == "lss":
            return {"method": "lss", "name": "lss (forced)"}
        if override == "readonly":
            return {"method": "readonly", "name": "readonly (forced)"}
        if override.startswith("sdo:") or override.startswith("recipe:"):
            name = override.split(":", 1)[1].strip().lower()
            by_name = _by_name(recipes)
            if name not in by_name:
                raise KeyError(f"no recipe named {name!r}")
            return by_name[name]
        raise ValueError(f"unrecognised --method {override!r}; use lss, readonly, or sdo:<name>")

    if vendor_id is not None:
        for key, recipe in recipes.get("vendors", {}).items():
            if _int_key(key) != vendor_id:
                continue
            want = recipe.get("product_code")
            if want is not None and _int_key(want) != product_code:
                continue                           # another product of this vendor
            return recipe

    return recipes.get("default", {"method": "lss", "name": "default-lss"})


def vendor_wide(recipe: dict) -> bool:
    """An SDO or sequence recipe without a "product_code": picked for every product of its vendor, while the
    objects it writes are those of one model (on another model they can mean something else)."""
    return recipe.get("method") in ("sdo", "sequence") and recipe.get("product_code") is None


def forced_fit_warning(recipes: dict, recipe: dict, info) -> str:
    """A recipe chosen by name (Method) for a device whose 0x1018 vendor or product is not the recipe's own."""
    if info is None or recipe.get("method") not in ("sdo", "sequence"):
        return ""
    for key, r in recipes.get("vendors", {}).items():
        if r.get("name") != recipe.get("name"):
            continue
        vid, want = _int_key(key), r.get("product_code")
        if info.vendor_id is not None and vid != info.vendor_id:
            return (f"Recipe {recipe.get('name')} is for vendor 0x{vid:X}; this device reports vendor "
                    f"0x{info.vendor_id:X}. Its objects can mean something else on this device.")
        if want is not None and info.product_code is not None and _int_key(want) != info.product_code:
            return (f"Recipe {recipe.get('name')} is for product 0x{_int_key(want):X}; this device reports product "
                    f"0x{info.product_code:X}. Its objects can mean something else on this model.")
        return ""
    return ""


def vendor_wide_warning(recipe: dict, info) -> str:
    product = f"0x{info.product_code:X}" if info is not None and info.product_code is not None else "unknown"
    return (f"Recipe {recipe.get('name', '?')} matches every product of vendor 0x{info.vendor_id or 0:X}, not one "
            f"model (this device reports product {product}). Check it is the model the recipe was written for: "
            "on another model these objects can mean something else.")


def recipe_list(recipes: dict) -> list[dict]:
    """Every recipe with its vendor key, for the UI."""
    out = [{"key": "default", **recipes.get("default", {})}]
    out += [{"key": k, **r} for k, r in recipes.get("vendors", {}).items()]
    out += [{"key": f"unknown:{k}", **r} for k, r in recipes.get("unknown", {}).items()]
    return out


# ------------------------------------------------------------------------------------------- compile

def identity_vars(info) -> dict:
    if info is None:
        return {}
    return {"VENDOR": info.vendor_id, "PRODUCT": info.product_code or 0, "REVISION": info.revision_number or 0,
            "SERIAL": info.serial_number}


def set_id_steps(recipe: dict, verify_timeout: float = 5.0) -> tuple[list[dict], str]:
    """(steps, effect) to change a node id with this recipe. Placeholders: $OLD_ID, $NEW_ID, identity."""
    method = recipe.get("method")
    if method == "readonly":
        raise ValueError(recipe.get("note", "this device cannot be re-addressed by this tool"))
    verify = recipe.get("verify_on_new_id", True)
    steps: list[dict] = []
    if method == "sdo":
        for s in recipe.get("steps", []):
            steps.append({"op": "sdo_write", "node": "$OLD_ID", "index": s["index"], "sub": s["sub"],
                          "type": s.get("type", "u8"), "value": s["value"]})
        store = recipe.get("store")
        if store and store != "none":
            steps.append({"op": "store", "node": "$OLD_ID", "target": store})
        effect = recipe.get("effect", "power_cycle")
        if effect != "power_cycle":
            steps.append({"op": "nmt", "command": "reset_node", "node": "$OLD_ID",
                          "note": "apply the new id"})
    elif method == "lss":
        steps = [
            {"op": "lss", "command": "switch_selective"},
            {"op": "lss", "command": "configure_node_id", "value": "$NEW_ID"},
            {"op": "lss", "command": "store"},
            {"op": "lss", "command": "switch_global", "state": "waiting", "always": True,
             "note": "never leave LSS devices in configuration state"},
            {"op": "nmt", "command": "reset_comm", "node": "$OLD_ID"},
        ]
        effect = recipe.get("effect", "immediate")
    elif method == "sequence":
        steps = [dict(s) for s in recipe.get("steps", [])]
        effect = recipe.get("effect", "immediate")
        verify = recipe.get("verify_on_new_id", False)
    else:
        raise ValueError(f"unknown recipe method {method!r}")
    if verify and effect != "power_cycle" and not any(s.get("op") == "wait_node" for s in steps):
        steps.append({"op": "wait_node", "node": "$NEW_ID", "timeout_s": verify_timeout, "optional": True,
                      "note": "verification"})
    return steps, effect


def baud_lss_steps(selective: bool, switch_delay_ms: int = 500, verify_timeout: float = 5.0,
                   reopen: bool = True) -> list[dict]:
    """LSS bit-rate change (CiA 305). Store comes before activate: after activate the slaves may ignore
    frames for the switch delay, so a later store could be lost."""
    first = ({"op": "lss", "command": "switch_selective"} if selective else
             {"op": "lss", "command": "switch_global", "state": "configuration",
              "note": "every LSS device on the bus"})
    steps = [
        first,
        {"op": "lss", "command": "configure_bit_timing", "kbit": "$BAUD"},
        {"op": "lss", "command": "store"},
        {"op": "lss", "command": "activate_bit_timing", "delay_ms": switch_delay_ms},
        # from here on the devices switch: a stop request must not keep the adapter at the old rate
        {"op": "delay", "ms": int(switch_delay_ms * 2 + 200), "note": "devices switch over", "nostop": True},
    ]
    if reopen:
        steps.append({"op": "reopen", "kbit": "$BAUD", "nostop": True})
    steps.append({"op": "lss", "command": "switch_global", "state": "waiting", "always": True,
                  "note": "never leave LSS devices in configuration state"})
    if reopen:
        steps.append({"op": "wait_traffic", "timeout_s": verify_timeout, "optional": True, "note": "verification"})
    return steps


def baud_sdo_value(spec: dict, new_kbit: int) -> int:
    unit = spec.get("unit", "kbit/s")
    if unit == "enum":
        enum_map = {int(k): v for k, v in spec.get("enum", {}).items()}
        if new_kbit not in enum_map:
            raise ValueError(f"{new_kbit} kbit/s isn't in this device's supported set {sorted(enum_map)}")
        return enum_map[new_kbit]
    if unit == "bps":
        return new_kbit * 1000
    if unit == "kbit/s":
        lo, hi = spec.get("min"), spec.get("max")
        if lo is not None and hi is not None and not (lo <= new_kbit <= hi):
            raise ValueError(f"{new_kbit} kbit/s is out of range {lo}..{hi} for this device")
        return new_kbit
    raise ValueError(f"unknown baud_sdo unit {unit!r}")


def baud_sdo_steps(spec: dict, new_kbit: int, verify_timeout: float = 5.0) -> tuple[list[dict], str]:
    value = baud_sdo_value(spec, new_kbit)
    steps = [{"op": "sdo_write", "node": "$OLD_ID", "index": spec["index"], "sub": spec["sub"],
              "type": spec["type"], "value": value, "note": f"{new_kbit} kbit/s"}]
    store = spec.get("store")
    if store and store != "none":
        steps.append({"op": "store", "node": "$OLD_ID", "target": store})
    effect = spec.get("effect", "power_cycle")
    if effect == "power_cycle":
        return steps, effect
    if effect == "nmt_reset":
        steps.append({"op": "nmt", "command": "reset_node", "node": "$OLD_ID"})
    # from here on the node runs at the new rate: a stop request must not keep the adapter at the old one
    steps += [{"op": "delay", "ms": 500, "nostop": True}, {"op": "reopen", "kbit": "$BAUD", "nostop": True},
              {"op": "wait_traffic", "timeout_s": verify_timeout, "optional": True, "note": "verification"}]
    return steps, effect
