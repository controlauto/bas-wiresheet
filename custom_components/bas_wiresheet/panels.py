"""Controller faceplate blocks (Controller Inputs / Controller Outputs) for T3000 program sheets.

A faceplate shows the controller's physical inputs or outputs as terminals. A wire from an input terminal reads that
input; a wire into an output terminal writes that output. Before validation, compilation and simulation the faceplates
are expanded into ordinary T3000 Read / T3000 Write blocks (one per wired terminal), so the generated Control Basic is
exactly what those blocks produce. The same expansion exists in runtime/runtime.js (expandPanels); keep them equal.
"""
from __future__ import annotations

import copy
import re
from typing import Any

PANEL_TYPES = {"t3_panel_in": "IN", "t3_panel_out": "OUT"}
PIN_RE = re.compile(r"^(IN|OUT)([1-9]\d*)$")
SEP = "__"


def has_panels(sheet: Any) -> bool:
    return isinstance(sheet, dict) and isinstance(sheet.get("blocks"), list) and any(
        isinstance(b, dict) and b.get("type") in PANEL_TYPES for b in sheet["blocks"])


def _issue(code: str, message: str, **where: Any) -> dict[str, Any]:
    return {"severity": "error", "code": code, "message": message, **where}


def expand(sheet: dict) -> tuple[dict, dict[str, tuple[str, str]], list[dict]]:
    """Return (flat sheet, origin {virtual block id: (faceplate id, terminal)}, issues)."""
    if not has_panels(sheet):
        return sheet, {}, []
    panels = {b["id"]: b for b in sheet["blocks"] if isinstance(b, dict) and b.get("type") in PANEL_TYPES and isinstance(b.get("id"), str)}
    flat = {k: v for k, v in sheet.items() if k not in ("blocks", "wires")}
    blocks = [copy.deepcopy(b) for b in sheet["blocks"] if not (isinstance(b, dict) and b.get("id") in panels)]
    wires: list[Any] = []
    origin: dict[str, tuple[str, str]] = {}
    issues: list[dict] = []
    made: set[str] = set()

    def virtual(pid: str, pin: str, kind: str) -> str:
        vid = f"{pid}{SEP}{pin}"
        if vid not in made:
            made.add(vid)
            p = panels[pid]
            vt = ((p.get("params") or {}).get("pins") or {}).get(pin)
            vt = vt if vt in ("number", "boolean") else "number"
            params = {"point": pin, "value_type": vt}
            if kind == "t3_read":
                params["unit"] = ""
            blocks.append({"id": vid, "type": kind, "label": pin, "params": params, "position": copy.deepcopy(p.get("position") or {"x": 0, "y": 0})})
            origin[vid] = (pid, pin)
        return vid

    for w in sheet.get("wires") or []:
        try:
            src, dst = w["from"], w["to"]
            fb, fp, tb, tp = src["block"], src["port"], dst["block"], dst["port"]
        except (KeyError, TypeError):
            wires.append(copy.deepcopy(w))
            continue
        w = copy.deepcopy(w)
        bad = False
        if fb in panels:
            m = PIN_RE.match(str(fp or "").upper())
            if PANEL_TYPES[panels[fb]["type"]] != "IN" or not m or m.group(1) != "IN":
                issues.append(_issue("panel_pin", f"{fp} is not an input terminal of this controller", block=fb, port=fp, wire=w.get("id")))
                bad = True
            else:
                w["from"] = {"block": virtual(fb, m.group(0), "t3_read"), "port": "value"}
        if tb in panels:
            m = PIN_RE.match(str(tp or "").upper())
            if PANEL_TYPES[panels[tb]["type"]] != "OUT" or not m or m.group(1) != "OUT":
                issues.append(_issue("panel_pin", f"{tp} is not an output terminal of this controller", block=tb, port=tp, wire=w.get("id")))
                bad = True
            else:
                w["to"] = {"block": virtual(tb, m.group(0), "t3_write"), "port": "in"}
        if not bad:
            wires.append(w)
    flat["blocks"], flat["wires"] = blocks, wires
    return flat, origin, issues


def map_issues(issues: list[dict], origin: dict[str, tuple[str, str]]) -> list[dict]:
    """Point issues found on expanded blocks back at the faceplate terminal."""
    for i in issues:
        if i.get("block") in origin:
            pid, pin = origin[i["block"]]
            i["block"], i["port"] = pid, pin
            i.pop("param", None)
            i["message"] = f"{pin}: {i.get('message', '')}"
    return issues


def _models() -> dict:
    import json
    from pathlib import Path
    return json.loads((Path(__file__).parent / "runtime" / "blocks.json").read_text(encoding="utf-8"))["t3_models"]


def collapse(sheet: dict, model: str | None = None) -> dict:
    """The reverse of expand() for generated sheets: T3000 Read blocks of the controller's physical inputs and T3000 Write
    blocks of its physical outputs become wires from / to Controller Inputs and Controller Outputs faceplates.
    Variables, Modbus points and reads of outputs stay ordinary blocks. Unknown model: T3-BB (the faceplate default)."""
    models = _models()
    prof = models.get(model or "") or models["T3-BB"]
    inputs = set(range(1, prof["inputs"] + 1))
    outputs = set(range(prof["out_digital"][0], prof["out_analog"][1] + 1))
    taken = {b["id"] for b in sheet["blocks"]}

    def phys(b: dict) -> str | None:
        m = PIN_RE.match(str((b.get("params") or {}).get("point", "")).upper())
        if not m:
            return None
        n = int(m.group(2))
        if b["type"] == "t3_read" and m.group(1) == "IN" and n in inputs:
            return m.group(0)
        if b["type"] == "t3_write" and m.group(1) == "OUT" and n in outputs:
            return m.group(0)
        return None

    pin_of = {b["id"]: phys(b) for b in sheet["blocks"] if b.get("type") in ("t3_read", "t3_write")}
    pin_of = {k: v for k, v in pin_of.items() if v}
    if not pin_of:
        return sheet
    out = copy.deepcopy(sheet)
    rest = [b for b in out["blocks"] if b["id"] not in pin_of]
    xs = [b["position"]["x"] for b in rest] or [0]
    has_in = any(p.startswith("IN") for p in pin_of.values())
    has_out = any(p.startswith("OUT") for p in pin_of.values())
    shift = 620 if has_in else 0
    for b in rest:
        b["position"] = {"x": b["position"]["x"] - min(xs) + 40 + shift, "y": b["position"]["y"]}
    right = max(b["position"]["x"] for b in rest) + 420 if rest else 700

    def new_id(base: str) -> str:
        i, bid = 2, base
        while bid in taken:
            bid, i = f"{base}{i}", i + 1
        taken.add(bid)
        return bid

    cin = new_id("ctl_in") if has_in else None
    cout = new_id("ctl_out") if has_out else None
    pins: dict[str, str] = {}
    vtype = {b["id"]: (b.get("params") or {}).get("value_type", "number") for b in out["blocks"]}
    for w in out["wires"]:
        if w["from"]["block"] in pin_of:
            pin = pin_of[w["from"]["block"]]
            pins.setdefault(pin, vtype[w["from"]["block"]])
            w["from"] = {"block": cin, "port": pin}
        if w["to"]["block"] in pin_of:
            pin = pin_of[w["to"]["block"]]
            pins.setdefault(pin, vtype[w["to"]["block"]])
            w["to"] = {"block": cout, "port": pin}
    params = {"model": "Auto", "show": "All"}
    if cin:
        rest.insert(0, {"id": cin, "type": "t3_panel_in", "label": "Controller Inputs", "position": {"x": 40, "y": 40},
                        "params": {**params, "pins": {k: v for k, v in pins.items() if k.startswith("IN")}}})
    if cout:
        rest.append({"id": cout, "type": "t3_panel_out", "label": "Controller Outputs", "position": {"x": right, "y": 40},
                     "params": {**params, "pins": {k: v for k, v in pins.items() if k.startswith("OUT")}}})
    out["blocks"] = rest
    return out
