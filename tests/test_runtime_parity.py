"""The Python runtime (runs inside Home Assistant) must give the same results as runtime.js (panel, Node-RED).

Random monitoring sheets and random telemetry scenarios are run through both; every tick's block outputs, displays,
wires and timer state are compared (values, quality, remaining time).
"""
import importlib.util
import json
import math
import random
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "custom_components" / "bas_wiresheet"
spec = importlib.util.spec_from_file_location("bws_runtime_py", PKG / "runtime_py.py")
rp = importlib.util.module_from_spec(spec)
sys.modules["bws_runtime_py"] = rp
spec.loader.exec_module(rp)

NODE = r"""
const RT = require(process.argv[1]);
RT.setMacroCatalog(require(require('path').join(require('path').dirname(process.argv[1]), 'blocks.json')).blocks);
let data = ''; process.stdin.on('data', (c) => data += c); process.stdin.on('end', () => {
  const sc = JSON.parse(data); const rt = RT.createRuntime(sc.sheet); const out = [];
  for (const step of sc.steps) {
    for (const ev of step.events) rt.ingest(ev, step.t);
    if (step.ts !== undefined) rt.setTimestampSource(step.ts, step.ts ? null : 'clock lost');
    out.push(rt.tick(step.t));
  }
  process.stdout.write(JSON.stringify(out));
});
"""

NUM_BLOCKS = ["compare", "add", "subtract", "multiply", "divide", "minimum", "maximum", "absolute", "limit", "scale", "hysteresis", "select", "pi",
              "average", "rate", "ramp", "range_check"]
BOOL_BLOCKS = ["and", "or", "not", "on_delay", "off_delay", "xor", "latch", "edge", "counter", "alarm"]


def make_sheet(rng: random.Random) -> dict:
    blocks, wires = [], []
    nums, bools, samples = [], [], []
    wid = [0]

    def add(bid, typ, params):
        blocks.append({"id": bid, "type": typ, "position": {"x": 0, "y": 0}, "params": params})

    def wire(src, port, dst, dport):
        wid[0] += 1
        wires.append({"id": f"w{wid[0]}", "from": {"block": src, "port": port}, "to": {"block": dst, "port": dport}})

    for i in range(rng.randint(2, 4)):
        vt = rng.choice(["number", "number", "boolean"])
        ev = rng.choice(["changes_only", "reports_every_sample", "value_changes_each_sample"])
        p = {"entity_id": f"sensor.e{i}", "value_type": vt, "sample_evidence": ev, "rest_values": ["0"] if ev == "value_changes_each_sample" else []}
        if vt == "number" and rng.random() < 0.4:
            p["valid_min"], p["valid_max"] = -50, 150
        add(f"in{i}", "entity_input", p)
        (nums if vt == "number" else bools).append((f"in{i}", "value"))
        samples.append((f"in{i}", "sample"))
        if rng.random() < 0.7:
            add(f"fr{i}", "data_freshness", {"stale_after_s": rng.choice([5, 10, 30])})
            wire(f"in{i}", "sample", f"fr{i}", "sample")
            bools.append((f"fr{i}", "fresh"))
            add(f"dsf{i}", "status_display", {"label": f"F{i}"})
            wire(f"fr{i}", "status", f"dsf{i}", "in")
    add("k1", "constant", {"value_type": "number", "value": rng.choice([0, 5, 50, -3.5])})
    nums.append(("k1", "value"))
    add("kb", "constant", {"value_type": "boolean", "value": rng.choice([True, False])})
    bools.append(("kb", "value"))
    for j in range(rng.randint(4, 9)):
        bid = f"b{j}"
        if rng.random() < 0.55:
            t = rng.choice(NUM_BLOCKS)
            params = {"compare": {"op": rng.choice([">", ">=", "<", "<=", "==", "!="])}, "limit": {"low": 0, "high": 100},
                      "scale": {"in_low": 0, "in_high": 10, "out_low": 0, "out_high": 100, "clamp": rng.choice(["yes", "no"])},
                      "hysteresis": {"on_above": 60, "off_below": 50},
                      "pi": {"kp": 2, "ki": 0.05, "out_min": 0, "out_max": 100, "action": rng.choice(["reverse", "direct"])},
                      "average": {"window_s": rng.choice([5, 20])}, "rate": {"window_s": rng.choice([5, 20])},
                      "ramp": {"rate_per_min": rng.choice([1, 30, 600])}, "range_check": {"low": 10, "high": 90}}.get(t, {})
            add(bid, t, params)
            ins = {"compare": ["a", "b"], "add": ["a", "b"], "subtract": ["a", "b"], "multiply": ["a", "b"], "divide": ["a", "b"],
                   "minimum": ["a", "b"], "maximum": ["a", "b"], "absolute": ["in"], "limit": ["in"], "scale": ["in"], "hysteresis": ["in"],
                   "select": ["a", "b"], "pi": ["pv", "sp"], "average": ["in"], "rate": ["in"], "ramp": ["in"], "range_check": ["in"]}[t]
            for port in ins:
                s = rng.choice(nums)
                wire(s[0], s[1], bid, port)
            if t == "select":
                s = rng.choice(bools)
                wire(s[0], s[1], bid, "s")
            if t == "pi" and rng.random() < 0.5:
                s = rng.choice(bools)
                wire(s[0], s[1], bid, "en")
            (bools if t in ("compare", "hysteresis", "range_check") else nums).append((bid, "out"))
            if t == "range_check":
                bools.append((bid, "ok"))
        else:
            t = rng.choice(BOOL_BLOCKS)
            params = {"on_delay": {"delay_s": rng.choice([3, 5, 12])}, "off_delay": {"delay_s": rng.choice([3, 5, 12])},
                      "edge": {"edge": rng.choice(["rising", "falling", "both"]), "pulse_s": rng.choice([2, 6])},
                      "alarm": {"label": f"A{j}", "severity": "serious", "delay_s": rng.choice([0, 4])}}.get(t, {})
            add(bid, t, params)
            ins = {"and": ["a", "b"], "or": ["a", "b"], "not": ["in"], "on_delay": ["in"], "off_delay": ["in"], "xor": ["a", "b"],
                   "latch": ["set", "reset"], "edge": ["in"], "counter": ["in"] + (["reset"] if rng.random() < 0.5 else []), "alarm": ["in"]}[t]
            if t in ("and", "or") and rng.random() < 0.4:
                ins = ins + ["c"]
            for port in ins:
                s = rng.choice(bools)
                wire(s[0], s[1], bid, port)
            (nums if t == "counter" else bools).append((bid, "out"))
        if rng.random() < 0.5 and blocks[-1]["type"] != "alarm":
            add(f"d{j}", "status_display", {"label": f"D{j}"})
            wire(bid, "out", f"d{j}", "in")
    return {"schema": "bas-wiresheet/1", "id": "parity", "name": "parity", "mode": "monitoring", "revision": 1, "blocks": blocks, "wires": wires}


def make_steps(rng: random.Random, sheet: dict) -> list[dict]:
    ents = [b["params"] for b in sheet["blocks"] if b["type"] == "entity_input"]
    t0 = 1_800_000_000_000
    steps, cur = [], {}
    for k in range(90):
        t = t0 + k * 1000 + rng.choice([0, 0, 250])
        evs = []
        for p in ents:
            if rng.random() < 0.25:
                if p["value_type"] == "boolean":
                    s = rng.choice(["on", "off", "on", "unavailable", "maybe", "true", "0"])
                else:
                    s = rng.choice([str(rng.randint(-10, 120)), str(round(rng.uniform(-5, 200), 2)), "0", "unavailable", "unknown", "abc", " ", "55", "1e2"])
                prev = cur.get(p["entity_id"])
                changed = not prev or prev["state"] != s
                lr = None if rng.random() < 0.05 else t - rng.choice([0, 0, 2000, 40000])
                cur[p["entity_id"]] = ev = {"entity_id": p["entity_id"], "state": s, "attributes": {"unit_of_measurement": "°F"},
                                            "last_reported": lr, "last_changed": t if changed else (prev or {}).get("last_changed", t)}
                evs.append(ev)
        step = {"t": t, "events": evs}
        if rng.random() < 0.08:
            step["ts"] = rng.random() < 0.5
        steps.append(step)
    return steps


def run_js(sheet, steps):
    out = subprocess.run(["node", "-e", NODE, str(PKG / "runtime" / "runtime.js")], input=json.dumps({"sheet": sheet, "steps": steps}),
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def run_py(sheet, steps):
    rt, out = rp.Runtime(sheet), []
    for step in steps:
        for ev in step["events"]:
            rt.ingest(ev["entity_id"], ev["state"], ev["attributes"], ev["last_reported"], ev["last_changed"], step["t"])
        if "ts" in step:
            rt.set_timestamp_source(step["ts"], None if step["ts"] else "clock lost")
        out.append(json.loads(json.dumps(rt.tick(step["t"]), allow_nan=False, default=str)))
    return out


def same(a, b):
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and not isinstance(b, bool):
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    return a == b


def compare_val(js, py, where):
    for k in ("v", "q", "unit", "src", "reported", "changed", "reason", "age_s", "kind", "evidence", "state_quality", "alarm", "severity"):
        if k in js or k in py:
            assert same(js.get(k), py.get(k)), f"{where}.{k}: js={js.get(k)!r} py={py.get(k)!r}"


@pytest.mark.parametrize("seed", range(120))
def test_python_runtime_matches_javascript(seed):
    rng = random.Random(seed)
    sheet = make_sheet(rng)
    steps = make_steps(rng, sheet)
    js, py = run_js(sheet, steps), run_py(sheet, steps)
    assert len(js) == len(py)
    for n, (a, b) in enumerate(zip(js, py)):
        assert a["blocks"].keys() == b["blocks"].keys()
        for bid in a["blocks"]:
            ja, pa = a["blocks"][bid], b["blocks"][bid]
            assert ja["outputs"].keys() == pa["outputs"].keys(), f"tick {n} {bid} outputs"
            for port in ja["outputs"]:
                if port == "_display":
                    compare_val(ja["outputs"][port], pa["outputs"][port], f"tick {n} {bid}._display")
                    continue
                compare_val(ja["outputs"][port], pa["outputs"][port], f"tick {n} {bid}.{port}")
            assert same(ja["state"].get("remaining_s"), pa["state"].get("remaining_s")), f"tick {n} {bid} remaining"
        assert a["displays"].keys() == b["displays"].keys()
        for did in a["displays"]:
            compare_val(a["displays"][did]["value"], b["displays"][did]["value"], f"tick {n} display {did}")
        for wid in a["wires"]:
            compare_val(a["wires"][wid], b["wires"][wid], f"tick {n} wire {wid}")


def test_state_export_import_matches():
    rng = random.Random(7)
    sheet = make_sheet(rng)
    steps = make_steps(rng, sheet)
    rt = rp.Runtime(sheet)
    for step in steps[:40]:
        for ev in step["events"]:
            rt.ingest(ev["entity_id"], ev["state"], ev["attributes"], ev["last_reported"], ev["last_changed"], step["t"])
        rt.tick(step["t"])
    saved = rt.export_state()
    rt2 = rp.Runtime(sheet)
    assert rt2.import_state(saved, steps[39]["t"], steps[39]["t"] + 60000) == len(saved)
    for bid, x in saved.items():
        if "since" in x and x["since"] is not None:
            assert rt2.state[bid]["since"] == x["since"] + 60000


def test_unwired_blocks_match_between_runtimes():
    """Every catalog block with no wires: both runtimes give the same outputs and neither stops the sheet."""
    import json as _json
    cat = _json.loads((ROOT / "custom_components" / "bas_wiresheet" / "runtime" / "blocks.json").read_text())["blocks"]
    for t, d in cat.items():
        if t not in rp.EVAL:  # controller-only blocks (T3000, Modbus, faceplates) never run in Python; see test_t3000_compiler
            continue
        p = {x["id"]: x["default"] for x in d["params"] if x.get("default") is not None}
        sheet = {"schema": "bas-wiresheet/1", "id": "x", "name": "x", "blocks": [{"id": "b", "type": t, "params": p, "position": {"x": 0, "y": 0}}], "wires": []}
        steps = [{"t": 1000, "events": []}, {"t": 2000, "events": []}]
        js, py = run_js(sheet, steps), run_py(sheet, steps)
        for n, (a, c) in enumerate(zip(js, py)):
            ja, pa = a["blocks"]["b"], c["blocks"]["b"]
            assert ja["outputs"].keys() == pa["outputs"].keys(), (t, n, ja["outputs"].keys(), pa["outputs"].keys())
            for port in ja["outputs"]:
                compare_val(ja["outputs"][port], pa["outputs"][port], f"{t} tick {n} {port}")
            assert a["displays"].keys() == c["displays"].keys(), t


CAT_BLOCKS = json.loads((ROOT / "custom_components" / "bas_wiresheet" / "runtime" / "blocks.json").read_text())["blocks"]
MACROS = sorted(t for t, d in CAT_BLOCKS.items() if d.get("macro"))


def macro_sheet(typ: str, rng: random.Random) -> dict:
    d = CAT_BLOCKS[typ]
    blocks = [{"id": "m", "type": typ, "position": {"x": 0, "y": 0}, "params": {p["id"]: p["default"] for p in d["params"]}}]
    wires = []
    for n, ip in enumerate(d["inputs"]):
        if not ip.get("required") and rng.random() < 0.3:
            continue  # leave optional inputs to their defaults sometimes
        vt = "boolean" if ip["type"] == "boolean" else "number"
        blocks.append({"id": f"i{n}", "type": "entity_input", "position": {"x": 0, "y": 0},
                       "params": {"entity_id": f"sensor.m_{ip['id']}", "value_type": vt, "sample_evidence": "changes_only", "rest_values": []}})
        wires.append({"id": f"wi{n}", "from": {"block": f"i{n}", "port": "value"}, "to": {"block": "m", "port": ip["id"]}})
    for n, op in enumerate(d["outputs"]):
        blocks.append({"id": f"d{n}", "type": "status_display", "position": {"x": 0, "y": 0}, "params": {"label": op["id"]}})
        wires.append({"id": f"wo{n}", "from": {"block": "m", "port": op["id"]}, "to": {"block": f"d{n}", "port": "in"}})
    return {"schema": "bas-wiresheet/1", "id": "macro", "name": "macro", "mode": "monitoring", "revision": 1, "blocks": blocks, "wires": wires}


@pytest.mark.parametrize("typ", MACROS)
@pytest.mark.parametrize("seed", range(4))
def test_hvac_blocks_same_in_python_and_javascript(typ, seed):
    rng = random.Random(f"{typ}{seed}")
    sheet = macro_sheet(typ, rng)
    steps = make_steps(rng, sheet)
    js, py = run_js(sheet, steps), run_py(sheet, steps)
    for n, (a, b) in enumerate(zip(js, py)):
        assert a["blocks"].keys() == b["blocks"].keys(), (typ, n)
        assert a["blocks"]["m"]["outputs"].keys() == {o["id"] for o in CAT_BLOCKS[typ]["outputs"]}, "the HVAC block shows its outputs"
        for bid in a["blocks"]:
            for port in a["blocks"][bid]["outputs"]:
                compare_val(a["blocks"][bid]["outputs"][port], b["blocks"][bid]["outputs"][port], f"{typ} tick {n} {bid}.{port}")
        for did in a["displays"]:
            compare_val(a["displays"][did]["value"], b["displays"][did]["value"], f"{typ} tick {n} display {did}")
        for wid in a["wires"]:
            compare_val(a["wires"][wid], b["wires"][wid], f"{typ} tick {n} wire {wid}")


def test_hvac_expansion_same_in_python_and_javascript():
    import importlib.util as iu
    spec_ = iu.spec_from_file_location("bws_macros_t", PKG / "macros.py")
    mm = iu.module_from_spec(spec_)
    spec_.loader.exec_module(mm)
    rng = random.Random(3)
    for typ in MACROS:
        sheet = macro_sheet(typ, rng)
        flat, origin, outputs, issues = mm.expand(sheet)
        prog = ("const RT=require(process.argv[1]);const cat=require(require('path').join(require('path').dirname(process.argv[1]),'blocks.json')).blocks;"
                "const r=RT.expandMacros(JSON.parse(process.argv[2]),cat);console.log(JSON.stringify(r));")
        out = subprocess.run(["node", "-e", prog, str(PKG / "runtime" / "runtime.js"), json.dumps(sheet)], capture_output=True, text=True, check=True)
        j = json.loads(out.stdout)
        assert j["sheet"] == json.loads(json.dumps(flat)), typ
        assert j["origin"] == origin and j["outputs"] == {k: {p: list(v) for p, v in o.items()} for k, o in outputs.items()} and j["issues"] == issues, typ
