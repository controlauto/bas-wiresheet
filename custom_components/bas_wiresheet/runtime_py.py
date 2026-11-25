"""Python twin of runtime/runtime.js, for monitoring sheets that run continuously inside Home Assistant.

Same blocks, same value records ({v, q, unit, src, reported, changed, reason}), same snapshot shape. Parity with the
JavaScript runtime is checked by tests/test_runtime_parity.py, which runs both on the same scenarios.
Monitoring only: it computes values and never writes entities or calls services.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Any


def _macros():
    """macros.py, also when this file is loaded on its own (tests)."""
    try:
        from . import macros  # type: ignore[import-not-found]
        return macros
    except ImportError:
        import importlib.util
        import sys
        from pathlib import Path
        mod = sys.modules.get("bws_macros")
        if mod is None:
            spec = importlib.util.spec_from_file_location("bws_macros", Path(__file__).parent / "macros.py")
            mod = importlib.util.module_from_spec(spec)
            sys.modules["bws_macros"] = mod
            spec.loader.exec_module(mod)
        return mod

SCHEMA = "bas-wiresheet/1"
FRESH, STALE, UNAVAILABLE, UNKNOWN = "Fresh", "Stale", "Unavailable", "Freshness Unknown"
FUTURE_TOLERANCE_MS = 5000
NAN = float("nan")


# ---------- JavaScript-compatible helpers ----------

def js_num(x: Any) -> float:
    """Number(x) as JavaScript computes it, for the values sheets contain."""
    if x is None:          # a missing parameter (JavaScript undefined)
        return NAN
    if isinstance(x, bool):
        return 1.0 if x else 0.0
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    if s == "":
        return 0.0
    if "_" in s:
        return NAN
    try:
        if s.lower().startswith(("0x", "-0x", "+0x")):
            return NAN if s[0] in "+-" else float(int(s, 16))
        if s.lower() in ("infinity", "+infinity"):
            return math.inf
        if s.lower() == "-infinity":
            return -math.inf
        if s.lower() in ("inf", "+inf", "-inf", "nan"):
            return NAN
        return float(s)
    except ValueError:
        return NAN


def js_finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def js_str(x: Any) -> str:
    if x is None:
        return "null"
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, float):
        if math.isnan(x):
            return "NaN"
        if math.isinf(x):
            return "Infinity" if x > 0 else "-Infinity"
        if x == int(x) and abs(x) < 1e21:
            return str(int(x))
        r = repr(x)
        return r.replace("e+", "e+").replace("e-0", "e-")
    return str(x)


def to_fixed0(x: float) -> str:
    return str(int(math.floor(x + 0.5))) if x >= 0 else "-" + str(int(math.floor(-x + 0.5)))


def strict_eq(a: Any, b: Any) -> bool:
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b and not (isinstance(a, float) and math.isnan(a))
    return type(a) is type(b) and a == b


def val(v: Any, q: str, extra: dict | None = None) -> dict:
    o = {"v": v, "q": q, "unit": None, "src": None, "reported": None, "changed": None, "reason": None}
    if extra:
        o.update(extra)
    return o


def bad(q: str, reason: str, extra: dict | None = None) -> dict:
    o = val(None, q, extra)
    o["reason"] = reason
    return o


def is_good(x: Any) -> bool:
    return bool(x) and x.get("q") == "good"


def propagate(inputs: list, label: str) -> dict | None:
    for b in inputs:
        if not is_good(b):
            q = b["q"] if b and b.get("q") in ("no_telemetry", "unavailable") else "invalid"
            return bad(q, f"{label}: input {(b and (b.get('reason') or b.get('q'))) or 'undefined'}")
    return None


def parse_bool(s: Any) -> bool | None:
    t = js_str(s).lower() if not isinstance(s, str) else s.lower()
    if t in ("on", "true", "1", "open", "home"):
        return True
    if t in ("off", "false", "0", "closed", "not_home"):
        return False
    return None


def _empty(x: Any) -> bool:
    return x is None or x == ""


# ---------- block evaluators ----------

def _entity_input(b, inp, st, now, ctx):
    p = b.get("params") or {}
    s = ctx["samples"].get(p.get("entity_id"))
    unit = p.get("unit") or (s and (s.get("attributes") or {}).get("unit_of_measurement")) or None
    meta = {"unit": unit, "src": p.get("entity_id"), "reported": s["last_reported"] if s else None, "changed": s["last_changed"] if s else None}
    if not s:
        nt = bad("no_telemetry", "No telemetry received", meta)
        return {"value": nt, "sample": nt}
    sample = val(s, "good", meta)
    sample["evidence"] = p.get("sample_evidence") or "changes_only"
    sample["rest_values"] = p.get("rest_values") or []
    st0 = s["state"]
    if st0 is None or st0 in ("unavailable", "unknown"):
        un = bad("unavailable", f"Source state is {'missing' if st0 is None else st0}", meta)
        sample["state_quality"] = "unavailable"
        return {"value": un, "sample": sample}
    if (p.get("value_type") or "number") == "boolean":
        bv = parse_bool(st0)
        out = bad("invalid", f"Not a boolean state: {st0}", meta) if bv is None else val(bv, "good", meta)
    else:
        n = js_num(st0)
        if st0.strip() == "" or not js_finite(n):
            out = bad("invalid", f"Non-numeric state: {st0}", meta)
        elif not _empty(p.get("valid_min")) and n < js_num(p.get("valid_min")):
            out = bad("invalid", f"Below valid range ({js_str(n)} < {js_str(p.get('valid_min'))})", meta)
        elif not _empty(p.get("valid_max")) and n > js_num(p.get("valid_max")):
            out = bad("invalid", f"Above valid range ({js_str(n)} > {js_str(p.get('valid_max'))})", meta)
        else:
            out = val(n, "good", meta)
    sample["state_quality"] = out["q"]
    if out["q"] != "good":
        sample["state_reason"] = out["reason"]
    return {"value": out, "sample": sample}


def _constant(b, inp, st, now, ctx):
    p = b.get("params") or {}
    t = p.get("value_type") or "number"
    v = (p.get("value") is True or p.get("value") == "true") if t == "boolean" else js_num(p.get("value"))
    return {"value": val(v, "good", {"unit": (p.get("unit") or None) if t == "number" else None})}


def _cmp(op: str, x: Any, y: Any) -> bool:
    try:
        if op == ">":
            return x > y
        if op == ">=":
            return x >= y
        if op == "<":
            return x < y
        return x <= y
    except TypeError:
        return False


def _compare(b, inp, st, now, ctx):
    a, c = inp.get("a"), inp.get("b")
    e = propagate([a, c], "Compare")
    if e:
        return {"out": e}
    op = (b.get("params") or {}).get("op") or ">"
    if op in (">", ">=", "<", "<="):
        r = _cmp(op, a["v"], c["v"])
    elif op == "==":
        r = strict_eq(a["v"], c["v"])
    elif op == "!=":
        r = not strict_eq(a["v"], c["v"])
    else:
        return {"out": bad("invalid", f"Unknown operator {op}")}
    return {"out": val(bool(r), "good")}


def _logic_inputs(inp):
    return [inp[k] for k in ("a", "b", "c", "d") if k in inp]


def _and(b, inp, st, now, ctx):
    lst = _logic_inputs(inp)
    e = propagate(lst, "AND")
    return {"out": e} if e else {"out": val(all(x["v"] is True for x in lst), "good")}


def _or(b, inp, st, now, ctx):
    lst = _logic_inputs(inp)
    e = propagate(lst, "OR")
    return {"out": e} if e else {"out": val(any(x["v"] is True for x in lst), "good")}


def _not(b, inp, st, now, ctx):
    x = inp.get("in")
    e = propagate([x], "NOT")
    if e:
        return {"out": e}
    v = x["v"]
    return {"out": val(not (v and not (isinstance(v, float) and math.isnan(v))), "good")}


def _timer_bad(x, label, suffix):
    q = "unavailable" if x and x.get("q") == "unavailable" else "invalid"
    return bad(q, f"{label}: input {(x and x.get('reason')) or 'not valid'} ({suffix})")


def _on_delay(b, inp, st, now, ctx):
    x, d = inp.get("in"), js_num((b.get("params") or {}).get("delay_s")) * 1000
    if not is_good(x):
        st["since"] = None
        return {"out": _timer_bad(x, "On Delay", "timer reset")}
    if x["v"] is True:
        if st.get("since") is None:
            st["since"] = now
        el = now - st["since"]
        st["remaining_s"] = max(0, (d - el) / 1000) if not math.isnan(d) else NAN
        return {"out": val(el >= d if not math.isnan(d) else False, "good")}
    st["since"] = None
    st["remaining_s"] = None
    return {"out": val(False, "good")}


def _off_delay(b, inp, st, now, ctx):
    x, d = inp.get("in"), js_num((b.get("params") or {}).get("delay_s")) * 1000
    if not is_good(x):
        st.update({"held": False, "off_since": None, "remaining_s": None})
        return {"out": _timer_bad(x, "Off Delay", "timer reset")}
    if x["v"] is True:
        st.update({"held": True, "off_since": None, "remaining_s": None})
        return {"out": val(True, "good")}
    if not st.get("held"):
        st["remaining_s"] = None
        return {"out": val(False, "good")}
    if st.get("off_since") is None:
        st["off_since"] = now
    el = now - st["off_since"]
    if not math.isnan(d) and el >= d:
        st.update({"held": False, "off_since": None, "remaining_s": None})
        return {"out": val(False, "good")}
    st["remaining_s"] = (d - el) / 1000
    return {"out": val(True, "good")}


def _data_freshness(b, inp, st, now, ctx):
    p = b.get("params") or {}
    x = inp.get("sample")
    stale_ms = js_num(p.get("stale_after_s")) * 1000

    def res(status, reason, age=None):
        return {"status": val(status, "good", {"reason": reason, "age_s": age, "src": x and x.get("src"), "reported": x and x.get("reported"),
                                                "changed": x and x.get("changed")}),
                "fresh": bad("invalid", f"Freshness Unknown: {reason}") if status == UNKNOWN else val(status == FRESH, "good")}

    if not x or x.get("q") == "no_telemetry":
        return res(UNKNOWN, "No telemetry received")
    if x.get("q") != "good" or not x.get("v"):
        return res(UNKNOWN, x.get("reason") or "No sample")
    s = x["v"]
    if s["state"] is None or s["state"] in ("unavailable", "unknown"):
        return res(UNAVAILABLE, f"Source state is {'missing' if s['state'] is None else s['state']}")
    if s.get("last_reported") is None:
        return res(UNKNOWN, "No reliable sample timestamp (last_reported missing)")
    age_ms = now - s["last_reported"]
    if age_ms < -FUTURE_TOLERANCE_MS:
        return res(UNKNOWN, "Sample timestamp is ahead of the runtime clock")
    age = max(0, age_ms) / 1000
    if age_ms <= stale_ms:
        return res(FRESH, f"New sample {to_fixed0(age)} s ago", age)
    ev = x.get("evidence") or "changes_only"
    ts = ctx.get("ts_source") or {}
    if ev == "reports_every_sample" and not ts.get("ok", True):
        return res(UNKNOWN, f"Sample timestamp source unavailable: {ts.get('reason') or 'unknown'} (last report {to_fixed0(age)} s ago)", age)
    if ev == "reports_every_sample":
        return res(STALE, f"No new sample for {to_fixed0(age)} s (limit {js_str(p.get('stale_after_s'))} s)", age)
    if ev == "value_changes_each_sample":
        rest = [js_str(r) if not isinstance(r, str) else r for r in (x.get("rest_values") or [])]
        if str(s["state"]) in rest:
            return res(UNKNOWN, f"Value is at rest value {s['state']}; unchanged samples are not reported", age)
        return res(STALE, f"No new sample for {to_fixed0(age)} s (limit {js_str(p.get('stale_after_s'))} s)", age)
    return res(UNKNOWN, f"Source reports value changes only; arrival of unchanged samples cannot be verified (last report {to_fixed0(age)} s ago)", age)


def _math(label, keys, fn):
    def ev(b, inp, st, now, ctx):
        lst = [inp.get(k) for k in keys]
        e = propagate(lst, label)
        if e:
            return {"out": e}
        try:
            r = fn([x["v"] for x in lst], b.get("params") or {})
        except (TypeError, ZeroDivisionError, OverflowError):
            r = None
        if r is None or not js_finite(r):
            return {"out": bad("invalid", f"{label}: result is not a number")}
        return {"out": val(float(r), "good", {"unit": lst[0].get("unit") or None})}
    return ev


def _n(v):
    return float(v) if not isinstance(v, bool) else (1.0 if v else 0.0)


def _scale(v, p):
    il, ih, ol, oh = js_num(p.get("in_low")), js_num(p.get("in_high")), js_num(p.get("out_low")), js_num(p.get("out_high"))
    if il == ih:
        return None
    r = (_n(v[0]) - il) * (oh - ol) / (ih - il) + ol
    return r if p.get("clamp") == "no" else max(min(ol, oh), min(max(ol, oh), r))


def _hysteresis(b, inp, st, now, ctx):
    x, p = inp.get("in"), b.get("params") or {}
    if not is_good(x):
        st["on"] = False
        return {"out": _timer_bad(x, "Hysteresis", "state reset")}
    if _n(x["v"]) >= js_num(p.get("on_above")):
        st["on"] = True
    elif _n(x["v"]) <= js_num(p.get("off_below")):
        st["on"] = False
    return {"out": val(bool(st.get("on")), "good")}


def _select(b, inp, st, now, ctx):
    e = propagate([inp.get("s")], "Select")
    if e:
        return {"out": e}
    chosen = inp.get("a") if inp["s"]["v"] else inp.get("b")
    e2 = propagate([chosen], "Select")
    if e2:
        return {"out": e2}
    return {"out": val(chosen["v"], "good", {"unit": chosen.get("unit") or None})}


def _pi(b, inp, st, now, ctx):
    p = b.get("params") or {}
    lo, hi = js_num(p.get("out_min")), js_num(p.get("out_max"))
    e = propagate([inp.get("pv"), inp.get("sp")], "PI")
    if e:
        st["last"] = None
        return {"out": e}
    if "en" in inp:
        en = inp["en"]
        if not is_good(en):
            st["last"] = None
            return {"out": bad("invalid", f"PI: Enable {(en and en.get('reason')) or 'not valid'}")}
        if en["v"] is False:
            st["i"], st["last"] = 0, None
            return {"out": val(lo, "good", {"unit": inp["pv"].get("unit") or None})}
    pv, sp = _n(inp["pv"]["v"]), _n(inp["sp"]["v"])
    err = pv - sp if p.get("action") == "direct" else sp - pv
    P = js_num(p.get("kp")) * err
    dt = 0 if st.get("last") is None else (now - st["last"]) / 1000
    st["last"] = now
    st["i"] = max(lo - P, min(hi - P, (st.get("i") or 0) + js_num(p.get("ki")) * err * dt))
    return {"out": val(max(lo, min(hi, P + st["i"])), "good")}


def _bad_from(x, label):
    q = "unavailable" if x and x.get("q") == "unavailable" else "invalid"
    return bad(q, f"{label}: input {(x and x.get('reason')) or 'not valid'}")


def _xor(b, inp, st, now, ctx):
    e = propagate([inp.get("a"), inp.get("b")], "XOR")
    return {"out": e} if e else {"out": val((inp["a"]["v"] is True) != (inp["b"]["v"] is True), "good")}


def _latch(b, inp, st, now, ctx):
    e = propagate([inp.get("set"), inp.get("reset")], "Latch")
    if e:
        st["on"] = False
        return {"out": e}
    if inp["reset"]["v"] is True:
        st["on"] = False
    elif inp["set"]["v"] is True:
        st["on"] = True
    return {"out": val(bool(st.get("on")), "good")}


def _edge(b, inp, st, now, ctx):
    p, x = b.get("params") or {}, inp.get("in")
    if not is_good(x):
        st.update({"prev": None, "until": None, "remaining_s": None})
        return {"out": _bad_from(x, "Edge Pulse")}
    v, prev, kind = x["v"] is True, st.get("prev"), p.get("edge") or "rising"
    trig = prev is not None and v != prev and (kind == "both" or (kind == "rising" and v) or (kind == "falling" and not v))
    st["prev"] = v
    if trig:
        st["until"] = now + js_num(p.get("pulse_s")) * 1000
    on = st.get("until") is not None and now < st["until"]
    if not on:
        st["until"] = None
    st["remaining_s"] = (st["until"] - now) / 1000 if on else None
    return {"out": val(on, "good")}


def _counter(b, inp, st, now, ctx):
    x = inp.get("in")
    if "reset" in inp:
        r = inp["reset"]
        if not is_good(r):
            return {"out": bad("invalid", f"Counter: Reset {(r and r.get('reason')) or 'not valid'}")}
        if r["v"] is True:
            st["n"] = 0
            st["prev"] = (x["v"] is True) if is_good(x) else None
            return {"out": val(0, "good")}
    e = propagate([x], "Counter")
    if e:
        st["prev"] = None
        return {"out": e}
    v = x["v"] is True
    if st.get("prev") is False and v:
        st["n"] = (st.get("n") or 0) + 1
    st["prev"] = v
    return {"out": val(st.get("n") or 0, "good")}


def _windowed(st, x, now, window_ms):
    buf = st.setdefault("buf", [])
    buf.append([now, x["v"]])
    while buf and buf[0][0] < now - window_ms:
        buf.pop(0)
    return buf


def _average(b, inp, st, now, ctx):
    x = inp.get("in")
    e = propagate([x], "Moving Average")
    if e:
        return {"out": e}
    buf = _windowed(st, x, now, js_num((b.get("params") or {}).get("window_s")) * 1000)
    return {"out": val(sum(_n(v) for _, v in buf) / len(buf), "good", {"unit": x.get("unit") or None})}


def _rate(b, inp, st, now, ctx):
    x = inp.get("in")
    e = propagate([x], "Rate of Change")
    if e:
        return {"out": e}
    buf = _windowed(st, x, now, js_num((b.get("params") or {}).get("window_s")) * 1000)
    first, last = buf[0], buf[-1]
    if len(buf) < 2 or last[0] == first[0]:
        return {"out": bad("invalid", "Rate of Change: not enough samples in the window yet")}
    return {"out": val((_n(last[1]) - _n(first[1])) / ((last[0] - first[0]) / 60000), "good")}


def _ramp(b, inp, st, now, ctx):
    x = inp.get("in")
    e = propagate([x], "Rate Limit")
    if e:
        return {"out": e}
    if st.get("y") is None:
        st["y"], st["t"] = _n(x["v"]), now
    elif st.get("t") is None:
        st["t"] = now
    else:
        maxd = js_num((b.get("params") or {}).get("rate_per_min")) * (now - st["t"]) / 60000
        d = _n(x["v"]) - st["y"]
        st["y"] += max(-maxd, min(maxd, d))
        st["t"] = now
    return {"out": val(st["y"], "good", {"unit": x.get("unit") or None})}


def _range_check(b, inp, st, now, ctx):
    p, x = b.get("params") or {}, inp.get("in")
    e = propagate([x], "Range Check")
    if e:
        return {"out": e, "ok": e}
    outside = _n(x["v"]) < js_num(p.get("low")) or _n(x["v"]) > js_num(p.get("high"))
    return {"out": val(outside, "good"), "ok": val(not outside, "good")}


def _alarm(b, inp, st, now, ctx):
    p, x = b.get("params") or {}, inp.get("in")
    d = js_num(p.get("delay_s")) * 1000
    if not is_good(x):
        st["since"], st["remaining_s"] = None, None
        q = "unavailable" if x and x.get("q") == "unavailable" else "invalid"
        nb = bad(q, f"Alarm: condition {(x and x.get('reason')) or 'not valid'}")
        return {"out": nb, "_display": nb}
    active = False
    if x["v"] is True:
        if st.get("since") is None:
            st["since"] = now
        active = now - st["since"] >= d
        st["remaining_s"] = None if active else (d - (now - st["since"])) / 1000
    else:
        st["since"], st["remaining_s"] = None, None
    return {"out": val(active, "good"),
            "_display": val((p.get("label") or "Alarm") if active else "Normal", "good", {"alarm": active, "severity": p.get("severity") or "warning"})}


def _display(b, inp, st, now, ctx):
    return {"_display": inp.get("in") or bad("invalid", "Not connected")}


EVAL = {
    "entity_input": _entity_input, "constant": _constant, "compare": _compare, "and": _and, "or": _or, "not": _not,
    "on_delay": _on_delay, "off_delay": _off_delay, "data_freshness": _data_freshness, "status_display": _display,
    "add": _math("Add", ["a", "b"], lambda v, p: _n(v[0]) + _n(v[1])),
    "subtract": _math("Subtract", ["a", "b"], lambda v, p: _n(v[0]) - _n(v[1])),
    "multiply": _math("Multiply", ["a", "b"], lambda v, p: _n(v[0]) * _n(v[1])),
    "divide": _math("Divide", ["a", "b"], lambda v, p: None if _n(v[1]) == 0 else _n(v[0]) / _n(v[1])),
    "minimum": _math("Minimum", ["a", "b"], lambda v, p: min(_n(v[0]), _n(v[1]))),
    "maximum": _math("Maximum", ["a", "b"], lambda v, p: max(_n(v[0]), _n(v[1]))),
    "absolute": _math("Absolute", ["in"], lambda v, p: abs(_n(v[0]))),
    "limit": _math("Limit", ["in"], lambda v, p: max(js_num(p.get("low")), min(js_num(p.get("high")), _n(v[0])))),
    "scale": _math("Scale", ["in"], _scale), "hysteresis": _hysteresis, "select": _select, "pi": _pi,
    "xor": _xor, "latch": _latch, "edge": _edge, "counter": _counter, "average": _average, "rate": _rate, "ramp": _ramp,
    "range_check": _range_check, "alarm": _alarm,
}
KEEP = {"on_delay": ["since"], "off_delay": ["held", "off_since"], "hysteresis": ["on"], "pi": ["i"], "latch": ["on"],
        "counter": ["n", "prev"], "ramp": ["y"], "alarm": ["since"], "edge": ["prev", "until"]}


def topo_order(sheet: dict) -> list[str]:
    ids = [b["id"] for b in sheet["blocks"]]
    indeg = {i: 0 for i in ids}
    adj: dict[str, list[str]] = {i: [] for i in ids}
    for w in sheet.get("wires", []):
        if w["from"]["block"] in adj and w["to"]["block"] in indeg:
            adj[w["from"]["block"]].append(w["to"]["block"])
            indeg[w["to"]["block"]] += 1
    q, out = deque(i for i in ids if indeg[i] == 0), []
    while q:
        n = q.popleft()
        out.append(n)
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                q.append(m)
    if len(out) != len(ids):
        raise ValueError("Sheet contains a cycle; cycles are not supported")
    return out


def _strip_val(x):
    if not x:
        return {"v": None, "q": "invalid", "reason": "Not connected", "unit": None, "src": None, "reported": None, "changed": None}
    if isinstance(x.get("v"), dict) and x["v"].get("entity_id"):
        c = dict(x)
        c["v"] = x["v"]["state"]
        c["kind"] = "sample"
        return c
    return x


def _strip(o: dict) -> dict:
    r = {k: _strip_val(v) for k, v in o.items() if k != "_display"}
    if "_display" in o:
        r["_display"] = o["_display"]
    return r


class Runtime:
    def __init__(self, sheet: dict) -> None:
        if sheet.get("schema") != SCHEMA:
            raise ValueError(f"Unsupported sheet schema: {sheet.get('schema')}")
        sheet, _, self.macro_outputs, _ = _macros().expand(sheet)  # HVAC application blocks -> basic blocks
        self.sheet = sheet
        self.order = topo_order(sheet)
        self.by_id = {b["id"]: b for b in sheet["blocks"]}
        self.in_wires: dict[str, list[dict]] = {}
        for w in sheet.get("wires", []):
            self.in_wires.setdefault(w["to"]["block"], []).append(w)
        self.ctx: dict[str, Any] = {"samples": {}, "points": {}, "ts_source": {"ok": True, "reason": None}}
        self.state: dict[str, dict] = {}

    def set_timestamp_source(self, ok: bool, reason: str | None = None) -> None:
        self.ctx["ts_source"] = {"ok": bool(ok), "reason": reason}

    def ingest(self, entity_id: str, state: str | None, attributes: dict, last_reported: float | None, last_changed: float | None, received_at: float) -> None:
        self.ctx["samples"][entity_id] = {"entity_id": entity_id, "state": None if state is None else str(state), "attributes": attributes or {},
                                          "last_changed": last_changed, "last_reported": last_reported, "received_at": received_at}

    def entity_ids(self) -> list[str]:
        out: list[str] = []
        for b in self.sheet["blocks"]:
            e = (b.get("params") or {}).get("entity_id")
            if b["type"] == "entity_input" and e and e not in out:
                out.append(e)
        return out

    def tick(self, now: float) -> dict:
        outs: dict[str, dict] = {}
        blocks, displays, wires = {}, {}, {}
        for bid in self.order:
            b = self.by_id[bid]
            inp = {}
            for w in self.in_wires.get(bid, []):
                src = outs.get(w["from"]["block"])
                inp[w["to"]["port"]] = src.get(w["from"]["port"]) if src else None
            st = self.state.setdefault(bid, {})
            fn = EVAL.get(b["type"])
            o = fn(b, inp, st, now, self.ctx) if fn else {}
            outs[bid] = o
            if "_display" in o:
                displays[bid] = {"label": (b.get("params") or {}).get("label") or b.get("label") or bid, "value": o["_display"]}
            summary = {}
            if st.get("remaining_s") is not None:
                summary["remaining_s"] = st["remaining_s"]
            blocks[bid] = {"outputs": _strip(o), "state": summary}
        for mid, ports in self.macro_outputs.items():  # an HVAC block shows the outputs of its inner circuit
            blocks[mid] = {"outputs": {port: _strip_val((outs.get(ref[0]) or {}).get(ref[1])) for port, ref in ports.items()}, "state": {}}
        for w in self.sheet.get("wires", []):
            o = outs.get(w["from"]["block"])
            wires[w["id"]] = _strip_val(o.get(w["from"]["port"]) if o else None)
        return {"schema": SCHEMA, "sheet_id": self.sheet.get("id"), "revision": self.sheet.get("revision"), "now": now,
                "wires": wires, "blocks": blocks, "displays": displays}

    def export_state(self) -> dict:
        out = {}
        for bid, st in self.state.items():
            b = self.by_id.get(bid)
            keys = b and KEEP.get(b["type"])
            if keys:
                out[bid] = {"type": b["type"], **{k: st[k] for k in keys if k in st}}
        return out

    def import_state(self, saved: dict, saved_at: float, now: float) -> int:
        gap, n = max(0.0, float(now) - float(saved_at)), 0
        for bid, x in (saved or {}).items():
            b = self.by_id.get(bid)
            if not b or not x or x.get("type") != b["type"] or b["type"] not in KEEP:
                continue
            st = self.state[bid] = {k: x[k] for k in KEEP[b["type"]] if k in x}
            if isinstance(st.get("since"), (int, float)):
                st["since"] += gap
            if isinstance(st.get("off_since"), (int, float)):
                st["off_since"] += gap
            if isinstance(st.get("until"), (int, float)):
                st["until"] += gap
            if b["type"] == "pi":
                st["last"] = None
            n += 1
        return n
