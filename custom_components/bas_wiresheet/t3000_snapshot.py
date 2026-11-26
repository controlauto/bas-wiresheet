"""Full controller backup ("snapshot") of a TEMCO T3 controller: read, compare and selective restore.

A snapshot holds the raw T3000 records, so it can be compared field by field and written back exactly:
programs (status + code), IN/OUT/VAR points, PID controllers, weekly routines (record, times, ON/OFF flags),
annual routines (record, days), controller settings and monitor definitions.
Settings are kept for reference only and are never restored (they hold the network configuration).
"""
from __future__ import annotations

import difflib
from datetime import UTC, datetime
from typing import Any, Callable

from . import t3000_bacnet as tb
from . import t3000_points as tp
from . import t3000_settings as ts
from . import t3000_tables as tt

SNAPSHOT_VERSION = 1
MONITOR_CMD, MONITOR_SIZE, MONITOR_COUNT = 10, 104, 12
POINT_KINDS = ("OUT", "IN", "VAR")
RESTORABLE = ("program", "OUT", "IN", "VAR", "pid", "weekly", "annual")


def _read_monitor(client, index: int) -> bytes:
    data = tb.read_checked(client, tb.header(tb.PRIVATE_LEN, MONITOR_CMD, index, MONITOR_SIZE),
                           lambda d: len(d) == tb.PRIVATE_LEN + MONITOR_SIZE and d[2] == MONITOR_CMD, f"reading monitor {index + 1}")
    return data[tb.PRIVATE_LEN:]


def _optional(fn):
    try:
        return fn().hex()
    except (tb.BacnetError, tb.UploadRefused):
        return None  # e.g. firmware without the schedule flag table


def count_steps() -> int:
    return 16 * 2 + len(POINT_KINDS) + 16 + 8 * 3 + 4 * 2 + 1 + 1


def take(client, instance: int, progress: Callable[[int], None] | None = None) -> dict[str, Any]:
    """Read everything. progress(n) is called with the number of records read so far."""
    if client.identify() != instance:
        raise tb.UploadRefused("Wrong controller at this address")
    n = 0

    def step(value):
        nonlocal n
        n += 1
        if progress:
            progress(n)
        return value

    snap: dict[str, Any] = {"version": SNAPSHOT_VERSION, "instance": instance, "taken_at": datetime.now(UTC).isoformat()}
    settings = step(ts.read_raw(client))
    snap["settings"] = settings.hex()
    snap["panel"] = settings[ts.PANEL_NUMBER_OFFSET]
    programs = []
    for i in range(16):
        status = step(client.read_point(i))
        code = client.read_code(i)
        used = tb.code_used_length(code)
        programs.append({"status": status.hex(), "code": step(code[:used]).hex()})
    snap["programs"] = programs
    snap["points"] = {k: [r.hex() for r in step(tp.read_all(client, k))] for k in POINT_KINDS}
    snap["pid"] = [step(tt.read_table(client, "pid", i)).hex() for i in range(1, 17)]
    snap["weekly"] = [{"record": step(tt.read_table(client, "weekly", i)).hex(),
                       "times": step(tt.read_table(client, "weekly_times", i)).hex(),
                       "flags": step(_optional(lambda i=i: tt.read_table(client, "weekly_flags", i)))} for i in range(1, 9)]
    snap["annual"] = [{"record": step(tt.read_table(client, "annual", i)).hex(),
                       "days": step(tt.read_table(client, "annual_days", i)).hex()} for i in range(1, 5)]
    snap["monitors"] = [r.hex() for r in step(tb.read_range(client, MONITOR_CMD, 0, MONITOR_COUNT, MONITOR_SIZE, 4))]
    return snap


# ---------------- summary and comparison ----------------

def _program_info(p: dict) -> dict:
    status = bytes.fromhex(p["status"])
    d = tb.describe_status(status)
    return {"used": len(bytes.fromhex(p["code"])), "on": bool(status[tb.ON_OFF_OFFSET]), "label": d.get("label") or "", "description": d.get("description") or ""}


def summary(snap: dict) -> dict:
    progs = [_program_info(p) for p in snap["programs"]]
    named = lambda k: sum(1 for h in snap["points"][k] if tp.decode(k, bytes.fromhex(h))["label"].strip())  # noqa: E731
    return {"programs_used": sum(1 for p in progs if p["used"]), "programs_on": sum(1 for p in progs if p["used"] and p["on"]),
            "points_labelled": {k: named(k) for k in POINT_KINDS}, "panel": snap.get("panel"),
            "pid_used": sum(1 for h in snap["pid"] if tt.decode_pid(bytes.fromhex(h)).get("used"))}


POINT_FIELDS = ("description", "label", "auto_manual", "digital_analog", "range", "value")
PID_FIELDS = ("input", "setpoint", "action", "proportional", "reset", "rate", "bias", "auto_manual")


def _listing(code: bytes, decompile) -> list[str]:
    if not code:
        return []
    try:
        return decompile(code).splitlines()
    except Exception:  # noqa: BLE001 - compiler unavailable (e.g. ARM host): fall back to a byte comparison
        return [f"<{len(code)} bytes, sha {code.hex()[:12]}…>"]


def compare(a: dict, b: dict, decompile=None) -> list[dict]:
    """Changes from snapshot a (older / reference) to b. Each: {item, section, field, before, after[, diff]}."""
    out: list[dict] = []
    for i, (pa, pb) in enumerate(zip(a["programs"], b["programs"]), start=1):
        ia, ib = _program_info(pa), _program_info(pb)
        if pa["code"] != pb["code"]:
            entry = {"item": f"program:{i}", "section": "Programs", "field": "code", "before": f"{ia['used']} bytes", "after": f"{ib['used']} bytes"}
            if decompile:
                entry["diff"] = list(difflib.unified_diff(_listing(bytes.fromhex(pa["code"]), decompile), _listing(bytes.fromhex(pb["code"]), decompile),
                                                          "before", "after", lineterm="", n=1))[2:]
            out.append(entry)
        if ia["used"] and ib["used"] and ia["on"] != ib["on"]:
            out.append({"item": f"program:{i}", "section": "Programs", "field": "on", "before": ia["on"], "after": ib["on"]})
    for k in POINT_KINDS:
        for i, (ha, hb) in enumerate(zip(a["points"][k], b["points"][k]), start=1):
            if ha == hb:
                continue
            da, db = tp.decode(k, bytes.fromhex(ha)), tp.decode(k, bytes.fromhex(hb))
            for f in POINT_FIELDS:
                if f == "value" and (k == "IN" or db["auto_manual"] == "auto"):
                    continue  # live values: only a manual value is configuration
                if da.get(f) != db.get(f):
                    out.append({"item": f"{k}:{i}", "section": {"OUT": "Outputs", "IN": "Inputs", "VAR": "Variables"}[k], "field": f, "before": da.get(f), "after": db.get(f)})
    for i, (ha, hb) in enumerate(zip(a["pid"], b["pid"]), start=1):
        if ha != hb:
            da, db = tt.decode_pid(bytes.fromhex(ha)), tt.decode_pid(bytes.fromhex(hb))
            for f in PID_FIELDS:
                if da.get(f) != db.get(f):
                    out.append({"item": f"pid:{i}", "section": "PID", "field": f, "before": da.get(f), "after": db.get(f)})
    for i, (wa, wb) in enumerate(zip(a["weekly"], b["weekly"]), start=1):
        ta = tt.decode_times(bytes.fromhex(wa["times"]), bytes.fromhex(wa["flags"]) if wa.get("flags") else None)
        tb_ = tt.decode_times(bytes.fromhex(wb["times"]), bytes.fromhex(wb["flags"]) if wb.get("flags") else None)
        if ta != tb_:
            for d, (x, y) in enumerate(zip(ta, tb_)):
                if x != y:
                    out.append({"item": f"weekly:{i}", "section": "Schedules", "field": tt.DAYS[d], "before": x, "after": y})
        ra, rb = tt.decode_weekly(bytes.fromhex(wa["record"])), tt.decode_weekly(bytes.fromhex(wb["record"]))
        for f in ("label", "description", "auto_manual"):
            if ra.get(f) != rb.get(f):
                out.append({"item": f"weekly:{i}", "section": "Schedules", "field": f, "before": ra.get(f), "after": rb.get(f)})
    for i, (wa, wb) in enumerate(zip(a["annual"], b["annual"]), start=1):
        if wa["days"] != wb["days"]:
            year = datetime.now(UTC).year
            da, db = set(tt.decode_days(bytes.fromhex(wa["days"]), year)), set(tt.decode_days(bytes.fromhex(wb["days"]), year))
            out.append({"item": f"annual:{i}", "section": "Holidays", "field": "days", "before": sorted(da - db), "after": sorted(db - da)})
    return out


# ---------------- restore ----------------

def restore_item(client, instance: int, snap: dict, item: str, allowed_slots: list[int], digest) -> dict:
    """Write one item of a snapshot back to the controller and verify it. item: 'program:3', 'OUT:7', 'pid:1', ..."""
    kind, _, num = item.partition(":")
    index = int(num)
    if kind not in RESTORABLE:
        raise tb.UploadRefused(f"{kind} cannot be restored")
    if client.identify() != instance or snap["instance"] != instance:
        raise tb.UploadRefused("This snapshot belongs to another controller")
    if kind == "program":
        return _restore_program(client, instance, snap["programs"][index - 1], index, allowed_slots, digest)
    if kind in POINT_KINDS:
        raw = bytes.fromhex(snap["points"][kind][index - 1])
        tp.write_struct(client, kind, index, raw)
        got = tp.read_struct(client, kind, index)
        want, have = tp.decode(kind, raw), tp.decode(kind, got)
        bad = [f for f in POINT_FIELDS if not (f == "value" and (kind == "IN" or want["auto_manual"] == "auto")) and want.get(f) != have.get(f)]
        if bad:
            raise tb.BacnetError(f"{kind}{index}: the controller did not keep {', '.join(bad)}")
        return {"ok": True, "item": item}
    if kind == "pid":
        raw = bytes.fromhex(snap["pid"][index - 1])
        tt.write_table(client, "pid", index, raw)
        want, have = tt.decode_pid(raw), tt.decode_pid(tt.read_table(client, "pid", index))
        bad = [f for f in PID_FIELDS if want.get(f) != have.get(f)]
        if bad:
            raise tb.BacnetError(f"PID{index}: the controller did not keep {', '.join(bad)}")
        return {"ok": True, "item": item}
    if kind == "weekly":
        w = snap["weekly"][index - 1]
        if w.get("flags"):
            tt.write_table(client, "weekly_flags", index, bytes.fromhex(w["flags"]))
        tt.write_table(client, "weekly_times", index, bytes.fromhex(w["times"]))
        tt.write_table(client, "weekly", index, bytes.fromhex(w["record"]))
        if tt.read_table(client, "weekly_times", index) != bytes.fromhex(w["times"]):
            raise tb.BacnetError(f"WR{index}: schedule times did not read back")
        return {"ok": True, "item": item}
    w = snap["annual"][index - 1]
    tt.write_table(client, "annual_days", index, bytes.fromhex(w["days"]))
    tt.write_table(client, "annual", index, bytes.fromhex(w["record"]))
    if tt.read_table(client, "annual_days", index) != bytes.fromhex(w["days"]):
        raise tb.BacnetError(f"AR{index}: holiday days did not read back")
    return {"ok": True, "item": item}


def _restore_program(client, instance: int, p: dict, slot: int, allowed_slots: list[int], digest) -> dict:
    code = bytes.fromhex(p["code"])
    want_on = bool(bytes.fromhex(p["status"])[tb.ON_OFF_OFFSET])
    current = client.read_code(slot - 1)
    if not code:
        if tb.code_used_length(current):
            tb.remove_program(client, instance, slot)
        return {"ok": True, "item": f"program:{slot}", "action": "emptied" if tb.code_used_length(current) else "already empty"}
    if tb.code_used_length(current):
        if digest(current) == digest(code):
            result = {"ok": True, "action": "same code"}
        else:
            plan, backup = tb.plan_replace(client, instance, slot, allowed_slots, code, digest)
            result = tb.apply_replace(client, plan, code, backup, plan.token, digest)
    else:
        plan, backup = tb.plan_upload(client, instance, slot, allowed_slots, code, allow_replace=True)
        result = tb.apply_upload(client, plan, code, backup, plan.token)
    if not result.get("ok"):
        return {**result, "item": f"program:{slot}"}
    import hashlib
    if want_on:
        tb.start_program(client, instance, slot, hashlib.sha256(code).hexdigest(), digest(code), digest)
    else:
        tb.stop_program(client, instance, slot)
    return {"ok": True, "item": f"program:{slot}", "action": result.get("action", "written"), "on": want_on}
