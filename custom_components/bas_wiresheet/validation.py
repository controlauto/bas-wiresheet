"""Sheet validation for bas-wiresheet/1.

Pure Python (no Home Assistant imports) so it can be unit-tested directly.
Every issue names the exact block, wire, port or parameter it refers to.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import panels

SCHEMA = "bas-wiresheet/1"
_CATALOG_PATH = Path(__file__).parent / "runtime" / "blocks.json"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ENTITY_RE = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
T3_ONLY_BLOCKS = {"t3_read", "t3_write"}
MONITORING_ONLY_BLOCKS = {"entity_input", "data_freshness", "average", "rate", "ramp"}  # need a sample history / scan time a controller program does not have
T3_POINT_RE = re.compile(r"^(IN|OUT|VAR)[1-9]\d*$")
RELATIONS = {
    "limit": ("low", "high", "Low must be below High"),
    "scale": ("in_low", "in_high", "Input Low and Input High must be different"),
    "hysteresis": ("off_below", "on_above", "Off Below must be below On Above"),
    "pi": ("out_min", "out_max", "Output Min must be below Output Max"),
}
MAX_IMPORTED = 1500
MAX_BLOCKS = 200
MAX_WIRES = 400

# Equipment excluded from monitoring sheets, from the t3000: locked_equipment setting (exclude_from_sheets: true).
EXCLUDED_EQUIPMENT: dict[str, re.Pattern] = {}


def set_excluded_equipment(items: list[dict]) -> None:
    EXCLUDED_EQUIPMENT.clear()
    for i in items or []:
        if i.get("exclude_from_sheets"):
            EXCLUDED_EQUIPMENT[i.get("tag") or i["name"]] = re.compile(i["pattern"], re.I)


_catalog_cache: dict[str, Any] | None = None


def load_catalog() -> dict[str, Any]:
    """Cached. async_setup loads it in the executor so the event loop never reads the file."""
    global _catalog_cache
    if _catalog_cache is None:
        _catalog_cache = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    return _catalog_cache


def _issue(code: str, message: str, **where: Any) -> dict[str, Any]:
    out = {"severity": "error", "code": code, "message": message}
    out.update({k: v for k, v in where.items() if v is not None})
    return out


def _warn(code: str, message: str, **where: Any) -> dict[str, Any]:
    out = _issue(code, message, **where)
    out["severity"] = "warning"
    return out


def resolve_port(block: dict, port_def: dict, key: str) -> Any:
    """Resolve a port's type or unit, which may come from a block parameter."""
    raw = port_def.get(key)
    if isinstance(raw, dict) and "param" in raw:
        pdefault = None
        return (block.get("params") or {}).get(raw["param"], pdefault)
    return raw


def port_type(block: dict, port_def: dict, catalog: dict) -> str:
    t = resolve_port(block, port_def, "type")
    if t is None:
        # parameter not set: fall back to the parameter default
        raw = port_def.get("type")
        bdef = catalog["blocks"][block["type"]]
        for p in bdef["params"]:
            if p["id"] == raw.get("param"):
                return p.get("default") or "number"
    return t or "any"


def port_unit(block: dict, port_def: dict, entity_units: dict[str, str] | None) -> str:
    unit = resolve_port(block, port_def, "unit") or ""
    if not unit and block.get("type") == "entity_input" and entity_units:
        unit = entity_units.get((block.get("params") or {}).get("entity_id", ""), "") or ""
    return unit


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate_sheet(
    sheet: Any,
    *,
    catalog: dict | None = None,
    known_entities: dict[str, dict] | None = None,
) -> list[dict[str, Any]]:
    """Return a list of issues. An empty list (or warnings only) means valid.

    known_entities: optional {entity_id: {"unit": str}} taken from Home Assistant.
    Controller faceplates on T3000 sheets are checked as the T3000 Read / Write blocks they expand to.
    """
    if panels.has_panels(sheet) and sheet.get("mode") == "t3000":
        flat, origin, extra = panels.expand(sheet)
        return extra + panels.map_issues(_validate_sheet(flat, catalog=catalog, known_entities=known_entities), origin)
    return _validate_sheet(sheet, catalog=catalog, known_entities=known_entities)


def _validate_sheet(
    sheet: Any,
    *,
    catalog: dict | None = None,
    known_entities: dict[str, dict] | None = None,
) -> list[dict[str, Any]]:
    """Return a list of issues. An empty list (or warnings only) means valid.

    known_entities: optional {entity_id: {"unit": str}} taken from Home Assistant.
    """
    catalog = catalog or load_catalog()
    issues: list[dict[str, Any]] = []
    if not isinstance(sheet, dict):
        return [_issue("sheet_type", "Sheet must be a JSON object")]
    if sheet.get("schema") != SCHEMA:
        issues.append(_issue("schema", f"Unsupported schema {sheet.get('schema')!r}; expected {SCHEMA}"))
        return issues
    if not isinstance(sheet.get("id"), str) or not ID_RE.match(sheet["id"]):
        issues.append(_issue("sheet_id", "Sheet id must be 1-64 characters: letters, digits, '-' or '_'"))
    if not isinstance(sheet.get("name"), str) or not sheet["name"].strip():
        issues.append(_issue("sheet_name", "Sheet name is required"))
    mode = sheet.get("mode", "monitoring")
    if mode not in ("monitoring", "t3000"):
        issues.append(_issue("mode", "Sheet mode must be monitoring or t3000"))
    if mode == "t3000":
        issues.extend(_validate_t3000_config(sheet.get("t3000")))

    blocks = sheet.get("blocks")
    wires = sheet.get("wires", [])
    if not isinstance(blocks, list):
        issues.append(_issue("blocks", "blocks must be a list"))
        return issues
    if not isinstance(wires, list):
        issues.append(_issue("wires", "wires must be a list"))
        return issues
    imported = mode == "t3000" and isinstance(sheet.get("t3000"), dict) and sheet["t3000"].get("imported") is True
    # T3000 programs are limited by the controller's 2000-byte program (checked when compiling), not by the block count
    max_blocks, max_wires = (MAX_IMPORTED, MAX_IMPORTED * 3) if mode == "t3000" else (MAX_BLOCKS, MAX_WIRES)
    if len(blocks) > max_blocks:
        issues.append(_issue("too_many_blocks", f"A sheet can have at most {max_blocks} blocks"))
    if len(wires) > max_wires:
        issues.append(_issue("too_many_wires", f"A sheet can have at most {max_wires} wires"))

    entity_units = {k: (v or {}).get("unit", "") for k, v in (known_entities or {}).items()}
    by_id: dict[str, dict] = {}
    for b in blocks:
        if not isinstance(b, dict):
            issues.append(_issue("block_type", "Each block must be an object"))
            continue
        bid = b.get("id")
        if not isinstance(bid, str) or not ID_RE.match(bid):
            issues.append(_issue("block_id", f"Invalid block id {bid!r}", block=str(bid)))
            continue
        if bid in by_id:
            issues.append(_issue("duplicate_block", f"Duplicate block id {bid}", block=bid))
            continue
        by_id[bid] = b
        bdef = catalog["blocks"].get(b.get("type"))
        if not bdef:
            issues.append(_issue("unknown_block", f"Unknown block type {b.get('type')!r}", block=bid))
            continue
        pos = b.get("position") or {}
        if not (_is_number(pos.get("x")) and _is_number(pos.get("y"))):
            issues.append(_issue("position", "Block position must have numeric x and y", block=bid))
        issues.extend(_validate_params(b, bdef, known_entities))
        if b.get("type") in ("cb_local_read", "cb_local_write") and mode != "t3000":
            issues.append(_issue("t3_block_mode", "Local variables exist only in T3000 programs", block=bid))
        if b.get("type") == "cb_unmapped" and not imported:
            issues.append(_issue("t3_block_mode", "This block only exists on sheets generated from a program", block=bid))
        if b.get("type") in ("modbus_read", "modbus_write") and mode != "t3000":
            issues.append(_issue("t3_block_mode", f"{bdef['label']} runs on the controller: only allowed on T3000 program sheets", block=bid))
        if b.get("type") == "modbus_write" and (b.get("params") or {}).get("register_type") not in (None, "holding", "coil", "holding_float_abcd",
                                                                                                    "holding_float_cdab", "holding_float_badc", "holding_float_dcba"):
            issues.append(_issue("modbus_write_type", "Only holding registers and coils can be written", block=bid, param="register_type"))
        if (b.get("type") in T3_ONLY_BLOCKS or b.get("type") in panels.PANEL_TYPES) and mode != "t3000":
            issues.append(_issue("t3_block_mode", f"{bdef['label']} is only allowed on T3000 program sheets", block=bid))
        if mode == "t3000" and b.get("type") in MONITORING_ONLY_BLOCKS:
            issues.append(_issue("t3_block_mode", f"{bdef['label']} cannot run on a T3000 controller", block=bid))
        if b.get("type") in T3_ONLY_BLOCKS and not T3_POINT_RE.match(str((b.get("params") or {}).get("point", "")).upper()):
            issues.append(_issue("t3_point", "Point must look like IN5, OUT60 or VAR120", block=bid, param="point"))

    # wires
    wire_ids: set[str] = set()
    used_inputs: dict[tuple[str, str], str] = {}
    adj: dict[str, list[tuple[str, str]]] = {k: [] for k in by_id}
    for w in wires:
        if not isinstance(w, dict):
            issues.append(_issue("wire_type", "Each wire must be an object"))
            continue
        wid = w.get("id")
        if not isinstance(wid, str) or not ID_RE.match(wid):
            issues.append(_issue("wire_id", f"Invalid wire id {wid!r}", wire=str(wid)))
            continue
        if wid in wire_ids:
            issues.append(_issue("duplicate_wire", f"Duplicate wire id {wid}", wire=wid))
            continue
        wire_ids.add(wid)
        fr, to = w.get("from") or {}, w.get("to") or {}
        sb, tb = by_id.get(fr.get("block")), by_id.get(to.get("block"))
        if not sb or not tb:
            issues.append(_issue("wire_endpoint", "Wire connects to a missing block", wire=wid))
            continue
        sdef, tdef = catalog["blocks"].get(sb["type"]), catalog["blocks"].get(tb["type"])
        if not sdef or not tdef:
            continue
        op = next((p for p in sdef["outputs"] if p["id"] == fr.get("port")), None)
        ip = next((p for p in tdef["inputs"] if p["id"] == to.get("port")), None)
        if not op:
            issues.append(_issue("wire_port", f"{sdef['label']} has no output {fr.get('port')!r}", wire=wid, block=sb["id"]))
            continue
        if not ip:
            issues.append(_issue("wire_port", f"{tdef['label']} has no input {to.get('port')!r}", wire=wid, block=tb["id"]))
            continue
        key = (tb["id"], ip["id"])
        if key in used_inputs:
            issues.append(_issue("input_multiple", f"Input {ip['label']} of {tb['id']} already has a wire", wire=wid, block=tb["id"], port=ip["id"]))
            continue
        used_inputs[key] = wid
        st, tt = port_type(sb, op, catalog), port_type(tb, ip, catalog)
        if tt != "any" and st != tt:
            issues.append(_issue(
                "type_mismatch",
                f"Incompatible connection: {sdef['label']}.{op['label']} is {catalog['port_types'].get(st, {}).get('label', st)}, "
                f"{tdef['label']}.{ip['label']} expects {catalog['port_types'].get(tt, {}).get('label', tt)}",
                wire=wid, block=tb["id"], port=ip["id"]))
        adj[sb["id"]].append((tb["id"], wid))

    # required inputs
    for bid, b in by_id.items():
        bdef = catalog["blocks"].get(b.get("type"))
        if not bdef:
            continue
        for ip in bdef["inputs"]:
            if ip.get("required") and (bid, ip["id"]) not in used_inputs:
                issues.append(_issue("input_unconnected", f"Required input {ip['label']} is not connected", block=bid, port=ip["id"]))

    # unit consistency on Compare
    incoming: dict[tuple[str, str], tuple[dict, dict]] = {}
    for w in wires:
        if not isinstance(w, dict) or w.get("id") not in wire_ids:
            continue
        sb, tb = by_id.get((w.get("from") or {}).get("block")), by_id.get((w.get("to") or {}).get("block"))
        if not sb or not tb or sb.get("type") not in catalog["blocks"]:
            continue
        op = next((p for p in catalog["blocks"][sb["type"]]["outputs"] if p["id"] == w["from"].get("port")), None)
        if op:
            incoming[(tb["id"], w["to"].get("port"))] = (sb, op, w["id"])
    for bid, b in by_id.items():
        if b.get("type") != "compare":
            continue
        a, c = incoming.get((bid, "a")), incoming.get((bid, "b"))
        if a and c:
            ua, uc = port_unit(a[0], a[1], entity_units), port_unit(c[0], c[1], entity_units)
            if ua and uc and ua != uc:
                issues.append(_issue("unit_mismatch", f"Compare inputs use different units ({ua} vs {uc})", block=bid, wire=c[2], port="b"))

    # cycles (Tarjan-free DFS marking back edges)
    color: dict[str, int] = {k: 0 for k in by_id}
    stack: list[tuple[str, str | None]] = []

    def dfs(n: str) -> None:
        color[n] = 1
        for m, wid in adj.get(n, []):
            if color[m] == 1:
                issues.append(_issue("cycle", "Feedback loops are not supported; this wire closes a cycle", wire=wid, block=m))
            elif color[m] == 0:
                dfs(m)
        color[n] = 2

    for n in by_id:
        if color[n] == 0:
            dfs(n)
    return issues


def _validate_t3000_config(cfg: Any) -> list[dict[str, Any]]:
    if not isinstance(cfg, dict):
        return [_issue("t3_config", "T3000 sheets need a t3000 section (device_instance, program_slot, work_vars)")]
    out: list[dict[str, Any]] = []
    dev, slot, work = cfg.get("device_instance"), cfg.get("program_slot"), cfg.get("work_vars", [])
    if not isinstance(dev, int) or isinstance(dev, bool) or not 0 <= dev < 4194303:
        out.append(_issue("t3_config", "device_instance must be a BACnet device instance"))
    if not isinstance(slot, int) or isinstance(slot, bool) or not 1 <= slot <= 16:
        out.append(_issue("t3_config", "program_slot must be 1-16"))
    if not isinstance(work, list) or not all(isinstance(x, str) for x in work):
        out.append(_issue("t3_config", "work_vars must be a list like ['VAR125-128']"))
    return out


def _validate_params(b: dict, bdef: dict, known_entities: dict | None) -> list[dict]:
    out: list[dict] = []
    params = b.get("params")
    bid = b["id"]
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return [_issue("params", "params must be an object", block=bid)]
    allowed = {p["id"] for p in bdef["params"]}
    for k in params:
        if k not in allowed:
            out.append(_issue("unknown_param", f"Unknown parameter {k!r}", block=bid, param=k))
    for p in bdef["params"]:
        pid, kind = p["id"], p["kind"]
        v = params.get(pid, p.get("default"))
        empty = v is None or v == "" or v == []
        if p.get("required") and empty:
            out.append(_issue("param_required", f"{p['label']} is required", block=bid, param=pid))
            continue
        if empty:
            continue
        if kind == "enum" and v not in p["options"]:
            out.append(_issue("param_enum", f"{p['label']} must be one of {', '.join(p['options'])}", block=bid, param=pid))
        elif kind == "number":
            if not _is_number(v):
                out.append(_issue("param_number", f"{p['label']} must be a number", block=bid, param=pid))
            else:
                if "min" in p and v < p["min"]:
                    out.append(_issue("param_range", f"{p['label']} must be at least {p['min']}", block=bid, param=pid))
                if "max" in p and v > p["max"]:
                    out.append(_issue("param_range", f"{p['label']} must be at most {p['max']}", block=bid, param=pid))
        elif kind in ("string", "text") and not isinstance(v, str):
            out.append(_issue("param_string", f"{p['label']} must be text", block=bid, param=pid))
        elif kind == "list" and not isinstance(v, list):
            out.append(_issue("param_list", f"{p['label']} must be a list", block=bid, param=pid))
        elif kind == "entity":
            if not isinstance(v, str) or not ENTITY_RE.match(v):
                out.append(_issue("param_entity", f"{p['label']} must be an entity id like sensor.name", block=bid, param=pid))
            else:
                for tag, rx in EXCLUDED_EQUIPMENT.items():
                    if rx.search(v.lower()):
                        out.append(_issue("excluded_equipment", f"{tag} is out of scope for this pilot", block=bid, param=pid))
                if known_entities is not None and v not in known_entities:
                    out.append(_warn("entity_not_found", f"Entity {v} is not present in Home Assistant", block=bid, param=pid))
    if b.get("type") == "constant":
        t = params.get("value_type", "number")
        v = params.get("value")
        if t == "number" and not _is_number(v):
            out.append(_issue("param_number", "Value must be a number", block=bid, param="value"))
        if t == "boolean" and not isinstance(v, bool):
            out.append(_issue("param_boolean", "Value must be true or false", block=bid, param="value"))
    if b.get("type") in RELATIONS:
        d = {x["id"]: x.get("default") for x in bdef["params"]}
        low_id, high_id, strict_text = RELATIONS[b["type"]]
        lo, hi = params.get(low_id, d.get(low_id)), params.get(high_id, d.get(high_id))
        if _is_number(lo) and _is_number(hi) and (lo == hi if b["type"] == "scale" else lo >= hi):
            out.append(_issue("param_relation", strict_text, block=bid, param=high_id))
    if b.get("type") == "entity_input":
        lo, hi = params.get("valid_min"), params.get("valid_max")
        if _is_number(lo) and _is_number(hi) and lo >= hi:
            out.append(_issue("param_range", "Valid Min must be below Valid Max", block=bid, param="valid_max"))
        if params.get("rest_values") and params.get("sample_evidence") != "value_changes_each_sample":
            out.append(_warn("param_unused", "Rest Values only apply to value_changes_each_sample", block=bid, param="rest_values"))
    return out


def has_errors(issues: list[dict]) -> bool:
    return any(i.get("severity") == "error" for i in issues)
