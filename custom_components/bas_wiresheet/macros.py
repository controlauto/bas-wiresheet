"""HVAC application blocks (Economizer, Heating Valve, Lead/Lag Pumps, …) are macros: each is defined once in
runtime/blocks.json as a small circuit of basic blocks and expanded before validation, compilation and running, so it
behaves the same in the browser, Node-RED, Home Assistant and on a T3000 controller. The same expansion exists in
runtime/runtime.js (expandMacros); keep them equal.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

SEP = "__"
_CATALOG: dict | None = None


def catalog() -> dict:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = json.loads((Path(__file__).parent / "runtime" / "blocks.json").read_text(encoding="utf-8"))["blocks"]
    return _CATALOG


def has_macros(sheet: Any, blocks: dict | None = None) -> bool:
    blocks = blocks or catalog()
    return isinstance(sheet, dict) and isinstance(sheet.get("blocks"), list) and any(
        isinstance(b, dict) and (blocks.get(b.get("type")) or {}).get("macro") for b in sheet["blocks"])


def _resolve(value: Any, params: dict) -> Any:
    if isinstance(value, str) and value.startswith("$"):
        return params.get(value[1:])
    return value


def _ref(macro_id: str, ref: str) -> tuple[str, str]:
    block, port = ref.split(".", 1)
    return f"{macro_id}{SEP}{block}", port


def expand(sheet: dict, blocks: dict | None = None) -> tuple[dict, dict[str, str], dict[str, dict], list[dict]]:
    """Return (flat sheet, origin {inner block id: macro id}, outputs {macro id: {port: (inner id, port)}}, issues)."""
    blocks = blocks or catalog()
    if not has_macros(sheet, blocks):
        return sheet, {}, {}, []
    macros = {b["id"]: b for b in sheet["blocks"] if isinstance(b, dict) and (blocks.get(b.get("type")) or {}).get("macro") and isinstance(b.get("id"), str)}
    flat = {k: v for k, v in sheet.items() if k not in ("blocks", "wires")}
    out_blocks = [copy.deepcopy(b) for b in sheet["blocks"] if not (isinstance(b, dict) and b.get("id") in macros)]
    out_wires: list[Any] = []
    origin: dict[str, str] = {}
    outputs: dict[str, dict] = {}
    issues: list[dict] = []
    wired_inputs: set[tuple[str, str]] = set()
    for w in sheet.get("wires") or []:
        try:
            if w["to"]["block"] in macros:
                wired_inputs.add((w["to"]["block"], w["to"]["port"]))
        except (KeyError, TypeError):
            pass
    for mid, m in macros.items():
        d = blocks[m["type"]]
        spec = d["macro"]
        params = {p["id"]: p.get("default") for p in d.get("params", [])}
        params.update({k: v for k, v in (m.get("params") or {}).items() if v is not None})
        pos = copy.deepcopy(m.get("position") or {"x": 0, "y": 0})
        name = m.get("label") or d.get("label") or m["type"]
        for ib in spec["blocks"]:
            bid = f"{mid}{SEP}{ib['id']}"
            out_blocks.append({"id": bid, "type": ib["type"], "label": f"{name} · {ib['id']}",
                               "params": {k: _resolve(v, params) for k, v in (ib.get("params") or {}).items()}, "position": copy.deepcopy(pos)})
            origin[bid] = mid
        for n, (src, dst) in enumerate(spec.get("wires", [])):
            fb, fp = _ref(mid, src)
            tb, tp = _ref(mid, dst)
            out_wires.append({"id": f"{mid}{SEP}w{n}", "from": {"block": fb, "port": fp}, "to": {"block": tb, "port": tp}})
        outputs[mid] = {port: _ref(mid, ref) for port, ref in spec.get("outputs", {}).items()}
        for ip in d.get("inputs", []):
            port = ip["id"]
            if (mid, port) in wired_inputs:
                continue
            if port in spec.get("defaults", {}):
                value = _resolve(spec["defaults"][port], params)
                cid = f"{mid}{SEP}in_{port}"
                vt = "boolean" if isinstance(value, bool) else "number"
                out_blocks.append({"id": cid, "type": "constant", "label": f"{name} · {port}",
                                   "params": {"value_type": vt, "value": value, "unit": ""}, "position": copy.deepcopy(pos)})
                origin[cid] = mid
                for k, ref in enumerate(spec["inputs"].get(port, [])):
                    tb, tp = _ref(mid, ref)
                    out_wires.append({"id": f"{mid}{SEP}in_{port}_{k}", "from": {"block": cid, "port": "value"}, "to": {"block": tb, "port": tp}})
            elif ip.get("required"):
                issues.append({"severity": "error", "code": "input_unconnected", "message": f"Input {ip.get('label') or port} is not connected",
                               "block": mid, "port": port})
    for w in sheet.get("wires") or []:
        try:
            src, dst = w["from"], w["to"]
            fb, fp, tb, tp = src["block"], src["port"], dst["block"], dst["port"]
        except (KeyError, TypeError):
            out_wires.append(copy.deepcopy(w))
            continue
        frm = {"block": fb, "port": fp}
        if fb in macros:
            ref = outputs[fb].get(fp)
            if ref is None:
                issues.append({"severity": "error", "code": "unknown_port", "message": f"Unknown output {fp}", "block": fb, "wire": w.get("id")})
                continue
            frm = {"block": ref[0], "port": ref[1]}
        if tb in macros:
            targets = blocks[macros[tb]["type"]]["macro"]["inputs"].get(tp)
            if not targets:
                issues.append({"severity": "error", "code": "unknown_port", "message": f"Unknown input {tp}", "block": tb, "wire": w.get("id")})
                continue
            for k, ref in enumerate(targets):
                ib, iport = _ref(tb, ref)
                out_wires.append({**copy.deepcopy(w), "id": w.get("id") if k == 0 else f"{w.get('id')}{SEP}{k}", "from": dict(frm), "to": {"block": ib, "port": iport}})
            continue
        out_wires.append({**copy.deepcopy(w), "from": frm})
    flat["blocks"], flat["wires"] = out_blocks, out_wires
    return flat, origin, outputs, issues


def map_issues(issues: list[dict], origin: dict[str, str]) -> list[dict]:
    """Issues found inside a macro are shown on the macro block (input-not-connected inside is reported once, on the macro)."""
    out = []
    for i in issues:
        mid = origin.get(i.get("block"))
        if mid is None:
            out.append(i)
            continue
        if i.get("code") == "input_unconnected":
            continue  # reported on the macro's own input
        i = {**i, "block": mid, "message": f"{i.get('message', '')} (inside)"}
        i.pop("param", None)
        i.pop("port", None)
        out.append(i)
    return out
