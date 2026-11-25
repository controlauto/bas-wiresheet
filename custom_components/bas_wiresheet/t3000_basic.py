"""Wire sheet -> T3000 Control Basic source (no Home Assistant imports).

The byte code is produced afterwards by T3000's own compiler (t3000_toolchain). This module only
writes Control Basic text and enforces the write policy.

Timing facts taken from T3000 decode.c: TIME-ON/TIME-OFF tables are updated at the start of a scan
from the previous scan's point value, and TIME-OFF also counts from program load. Timers therefore
use a dedicated work variable, and Off Delay uses a latch so it cannot turn on after a load.

T3000's listing drops parentheses around AND/OR groups, so "(A OR B) AND C" would change meaning if
the program were opened and saved in T3000. An AND/OR operand is therefore never itself an AND/OR
group: each inner group is first stored in a work variable. The listing also drops parentheses
around a divisor, so products and quotients are stored in a work variable before further use.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from . import panels

POINT_RE = re.compile(r"^(IN|OUT|VAR)([1-9]\d*)$")
POINT_LIMITS = {"IN": 64, "OUT": 64, "VAR": 128}
COMPARE_OPS = {">": ">", ">=": ">=", "<": "<", "<=": "<=", "==": "=", "!=": "<>"}
T3000_BLOCKS = {"t3_read", "t3_write", "modbus_read", "modbus_write", "constant", "compare", "and", "or", "not", "on_delay", "off_delay", "status_display", "xor", "latch", "range_check",
                "add", "subtract", "multiply", "divide", "minimum", "maximum", "absolute", "limit", "scale", "hysteresis", "select", "pi"}
# Modbus RTU points of any device on the controller's RS-485 port in Modbus Master mode. Control Basic remote point
# syntax <panel>.<slave id>.<type><register> (T3000 CM5 point type names; compiled and listed back by T3000's own code).
MODBUS_TYPES = {
    "holding": "MB_REG", "input_register": "MB_INPUTREG", "coil": "MB_COIL", "discrete_input": "MB_DISINPUT",
    "holding_float_abcd": "MB_REG_FLOAT_ABCD", "holding_float_cdab": "MB_REG_FLOAT_CDAB", "holding_float_badc": "MB_REG_FLOAT_BADC",
    "holding_float_dcba": "MB_REG_FLOAT_DCBA", "input_float_abcd": "MB_INPUT_FLOAT_ABCD", "input_float_cdab": "MB_INPUT_FLOAT_CDAB",
    "input_float_badc": "MB_INPUT_FLOAT_BADC", "input_float_dcba": "MB_INPUT_FLOAT_DCBA",
}
MODBUS_WRITABLE = {"holding", "coil", "holding_float_abcd", "holding_float_cdab", "holding_float_badc", "holding_float_dcba"}


def modbus_point(panel: int, params: dict, write: bool = False) -> tuple[str | None, str | None]:
    """(Control Basic point, error)."""
    rtype = params.get("register_type", "holding")
    if rtype not in MODBUS_TYPES:
        return None, f"Unknown register type {rtype!r}"
    if write and rtype not in MODBUS_WRITABLE:
        return None, "Only holding registers and coils can be written"
    try:
        slave, addr = int(params.get("slave")), int(params.get("address"))
    except (TypeError, ValueError):
        return None, "Slave ID and register must be whole numbers"
    if not 1 <= slave <= 254:
        return None, "Slave ID must be 1-254"
    if not 0 <= addr <= 65535:
        return None, "Register must be 0-65535"
    if not 1 <= int(panel or 0) <= 254:
        return None, "The controller's panel number is unknown (read it from the controller first)"
    return f"{int(panel)}.{slave}.{MODBUS_TYPES[rtype]}{addr}", None


SIMPLE_RE = re.compile(r"^(-?\d+(\.\d+)?|(IN|OUT|VAR)\d+)$")
MAX_FIXED = 2_147_483_647

Guard = Callable[[str, int], tuple[bool, str]]


def parse_point(text: Any) -> tuple[str, int] | None:
    m = POINT_RE.match(str(text or "").strip().upper())
    if not m:
        return None
    kind, idx = m.group(1), int(m.group(2))
    return (kind, idx) if idx <= POINT_LIMITS[kind] else None


def expand_points(items: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in items or []:
        m = re.match(r"^(IN|OUT|VAR)(\d+)(?:-(\d+))?$", str(raw).strip().upper())
        if not m:
            raise ValueError(f"Invalid point range {raw!r}")
        lo, hi = int(m.group(2)), int(m.group(3) or m.group(2))
        if lo < 1 or hi < lo or hi > POINT_LIMITS[m.group(1)]:
            raise ValueError(f"Point range out of limits: {raw!r}")
        out.extend(f"{m.group(1)}{i}" for i in range(lo, hi + 1))
    return out


def format_number(value: float) -> str:
    scaled = float(value) * 1000
    if abs(scaled - round(scaled)) > 1e-6:
        raise ValueError(f"{value} has more than 3 decimals (controller resolution is 0.001)")
    if abs(round(scaled)) > MAX_FIXED:
        raise ValueError(f"{value} is outside the controller's numeric range")
    text = f"{round(scaled) / 1000:.3f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


class _Gen:
    def __init__(self, sheet: dict, guard: Guard) -> None:
        self.sheet, self.guard = sheet, guard
        self.blocks = {b["id"]: b for b in sheet.get("blocks", [])}
        self.incoming = {(w["to"]["block"], w["to"]["port"]): (w["from"]["block"], w["from"]["port"]) for w in sheet.get("wires", [])}
        self.lines: list[str] = []
        self.state_map: dict[str, dict] = {}
        self.range_work: dict[str, str] = {}
        self.issues: list[dict] = []
        self.memo: dict[tuple[str, str], str] = {}
        self.reads: set[str] = set()
        self.modbus_reads: set[str] = set()
        try:
            self.panel = int((sheet.get("t3000") or {}).get("panel") or 0)
        except (TypeError, ValueError):
            self.panel = 0
        self.writes: dict[str, str] = {}
        self.work_used: list[str] = []
        self.work_free: list[str] = []
        self.groups: set[str] = set()

    def err(self, code: str, message: str, block: str | None = None, param: str | None = None) -> None:
        item: dict[str, Any] = {"severity": "error", "code": code, "message": message}
        if block:
            item["block"] = block
        if param:
            item["param"] = param
        self.issues.append(item)

    def emit(self, text: str) -> None:
        self.lines.append(text)

    def take_work(self, bid: str) -> str:
        if not self.work_free:
            self.err("t3_no_work_var", "Not enough work variables; add sandbox VARs to the sheet's work_vars", bid)
            return "VAR1"
        w = self.work_free.pop(0)
        self.work_used.append(w)
        return w

    def group(self, expr: str) -> str:
        self.groups.add(expr)
        return expr

    def atom(self, bid: str, expr: str) -> str:
        """Keep AND/OR operands free of inner groups so T3000's listing cannot change the logic."""
        if expr not in self.groups:
            return expr
        w = self.take_work(bid)
        self.emit(f"IF {expr} THEN {w} = 1 ELSE {w} = 0")
        return f"( {w} > 0 )"

    def simple(self, bid: str, expr: str) -> str:
        """Return a point or constant, storing any other expression in a work variable."""
        if SIMPLE_RE.match(expr):
            return expr
        w = self.take_work(bid)
        self.emit(f"{w} = {expr}")
        return w

    def number(self, bid: str, params: dict, key: str) -> str | None:
        try:
            return format_number(params[key])
        except (KeyError, TypeError, ValueError) as err:
            self.err("t3_number", str(err), bid, key)
            return None

    def input(self, bid: str, port: str) -> str:
        src = self.incoming.get((bid, port))
        if not src:
            self.err("input_unconnected", f"Input {port} is not connected", bid)
            return "0"
        return self.output(*src)

    def output(self, bid: str, port: str) -> str:
        if self.blocks[bid].get("type") == "range_check" and port == "ok":
            key = (bid, "ok")
            if key not in self.memo:
                self.output(bid, "out")
                self.memo[key] = f"( {self.range_work[bid]} = 0 )" if bid in self.range_work else "0"
            return self.memo[key]
        key = (bid, port if self.blocks[bid].get("type") == "range_check" else port)
        if key not in self.memo:
            self.memo[key] = self.build(self.blocks[bid])
        return self.memo[key]

    def build(self, b: dict) -> str:
        t, bid, p = b["type"], b["id"], b.get("params") or {}
        if t == "t3_read":
            parsed = parse_point(p.get("point"))
            if not parsed:
                self.err("t3_point", "Point must be IN1-64, OUT1-64 or VAR1-128", bid, "point")
                return "0"
            name = f"{parsed[0]}{parsed[1]}"
            self.reads.add(name)
            return f"( {name} <> 0 )" if p.get("value_type") == "boolean" else name
        if t == "modbus_read":
            name, error = modbus_point(self.panel, p)
            if error:
                self.err("modbus_point", error, bid, "address")
                return "0"
            self.modbus_reads.add(name)
            expr = name
            scale = p.get("scale", 1)
            if scale not in (None, "", 1, 1.0):
                try:
                    expr = f"( {name} * {format_number(scale)} )"
                except (TypeError, ValueError) as e:
                    self.err("t3_number", str(e), bid, "scale")
            return f"( {expr} <> 0 )" if p.get("value_type") == "boolean" else expr
        if t == "constant":
            if p.get("value_type") == "boolean":
                return "1" if p.get("value") is True else "0"
            try:
                return format_number(p.get("value", 0))
            except (TypeError, ValueError) as e:
                self.err("t3_number", str(e), bid, "value")
                return "0"
        if t == "compare":
            return f"( {self.input(bid, 'a')} {COMPARE_OPS[p.get('op', '>')]} {self.input(bid, 'b')} )"
        if t in ("and", "or"):
            parts = [self.atom(bid, self.input(bid, q)) for q in "abcd" if q in "ab" or (bid, q) in self.incoming]
            return self.group("( " + f" {t.upper()} ".join(parts) + " )")
        if t == "not":
            return f"( NOT {self.input(bid, 'in')} )"
        if t == "xor":
            # T3000's listing drops brackets around XOR operands, so each side is stored in a work variable first.
            wa, wb = self.take_work(bid), self.take_work(bid)
            self.emit(f"IF {self.input(bid, 'a')} THEN {wa} = 1 ELSE {wa} = 0")
            self.emit(f"IF {self.input(bid, 'b')} THEN {wb} = 1 ELSE {wb} = 0")
            return f"( {wa} <> {wb} )"
        if t == "latch":
            sset, sres = self.input(bid, "set"), self.input(bid, "reset")
            w = self.take_work(bid)
            self.state_map[bid] = {"kind": "var_bool", "var": w}
            self.emit(f"IF {sset} THEN {w} = 1")
            self.emit(f"IF {sres} THEN {w} = 0")
            return f"( {w} > 0 )"
        if t == "range_check":
            lo, hi = self.number(bid, p, "low"), self.number(bid, p, "high")
            x = self.simple(bid, self.input(bid, "in"))
            if lo is None or hi is None:
                return "0"
            w = self.take_work(bid)
            self.range_work[bid] = w
            self.emit(f"IF {x} < {lo} THEN {w} = 1 ELSE {w} = 0")
            self.emit(f"IF {x} > {hi} THEN {w} = 1")
            return f"( {w} > 0 )"
        if t in ("on_delay", "off_delay"):
            try:
                delay = format_number(p.get("delay_s", 60))
            except (TypeError, ValueError) as e:
                self.err("t3_number", str(e), bid, "delay_s")
                return "0"
            source = self.input(bid, "in")
            w = self.take_work(bid)
            self.emit(f"IF {source} THEN {w} = 1 ELSE {w} = 0")
            if t == "on_delay":
                self.state_map[bid] = {"kind": "on_delay", "timer": w, "delay_s": float(delay)}
                return self.group(f"( ( {w} > 0 ) AND ( TIME-ON( {w} ) >= {delay} ) )")
            latch = self.take_work(bid)
            self.state_map[bid] = {"kind": "off_delay", "timer": w, "latch": latch, "delay_s": float(delay)}
            self.emit(f"IF {w} > 0 THEN {latch} = 1")
            self.emit(f"IF ( {w} = 0 ) AND ( TIME-OFF( {w} ) >= {delay} ) THEN {latch} = 0")
            return f"( {latch} > 0 )"
        built = self.build_math(t, bid, p)
        if built is not None:
            return built
        self.err("t3_block", f"Block type {t} cannot run on a T3000 controller", bid)
        return "0"

    def build_math(self, t: str, bid: str, p: dict) -> str | None:
        defaults = {"limit": {"low": 0, "high": 100}, "scale": {"in_low": 0, "in_high": 10, "out_low": 0, "out_high": 100},
                    "hysteresis": {"on_above": 60, "off_below": 50},
                    "pi": {"kp": 2, "ki": 0.02, "out_min": 0, "out_max": 100}}.get(t, {})
        p = {**defaults, **p}
        if t in ("add", "subtract"):
            a, b = self.simple(bid, self.input(bid, "a")), self.simple(bid, self.input(bid, "b"))
            return f"( {a} {'+' if t == 'add' else '-'} {b} )"
        if t == "multiply":
            a, b = self.simple(bid, self.input(bid, "a")), self.simple(bid, self.input(bid, "b"))
            return f"( {a} * {b} )"
        if t == "divide":
            a, b = self.simple(bid, self.input(bid, "a")), self.simple(bid, self.input(bid, "b"))
            w = self.take_work(bid)
            self.emit(f"IF {b} <> 0 THEN {w} = {a} / {b}")
            return w
        if t in ("minimum", "maximum"):
            a, b = self.simple(bid, self.input(bid, "a")), self.simple(bid, self.input(bid, "b"))
            return f"{'MIN' if t == 'minimum' else 'MAX'}( {a} , {b} )"
        if t == "absolute":
            return f"ABS( {self.simple(bid, self.input(bid, 'in'))} )"
        if t == "limit":
            low, high = self.number(bid, p, "low"), self.number(bid, p, "high")
            x = self.simple(bid, self.input(bid, "in"))
            return "0" if low is None or high is None else f"MAX( {low} , MIN( {high} , {x} ) )"
        if t == "scale":
            vals = [self.number(bid, p, k) for k in ("in_low", "in_high", "out_low", "out_high")]
            x = self.simple(bid, self.input(bid, "in"))
            if None in vals:
                return "0"
            il, ih, ol, oh = (float(v) for v in vals)
            try:
                in_span, out_span = format_number(ih - il), format_number(oh - ol)
            except ValueError as err:
                self.err("t3_number", str(err), bid)
                return "0"
            w = self.take_work(bid)
            self.emit(f"{w} = {x} - {vals[0]}")
            self.emit(f"{w} = {w} * {out_span}")
            self.emit(f"{w} = {w} / {in_span}")
            self.emit(f"{w} = {w} + {vals[2]}")
            if p.get("clamp", "yes") != "no":
                self.emit(f"{w} = MAX( {format_number(min(ol, oh))} , MIN( {format_number(max(ol, oh))} , {w} ) )")
            return w
        if t == "hysteresis":
            on, off = self.number(bid, p, "on_above"), self.number(bid, p, "off_below")
            x = self.simple(bid, self.input(bid, "in"))
            if on is None or off is None:
                return "0"
            w = self.take_work(bid)
            self.state_map[bid] = {"kind": "hysteresis", "var": w}
            self.emit(f"IF {x} >= {on} THEN {w} = 1")
            self.emit(f"IF {x} <= {off} THEN {w} = 0")
            return f"( {w} > 0 )"
        if t == "pi":
            vals = {k: self.number(bid, p, k) for k in ("kp", "ki", "out_min", "out_max")}
            pv, sp = self.simple(bid, self.input(bid, "pv")), self.simple(bid, self.input(bid, "sp"))
            enable = self.input(bid, "en") if (bid, "en") in self.incoming else None
            if None in vals.values():
                return "0"
            lo, hi = vals["out_min"], vals["out_max"]
            err, prop, integ = self.take_work(bid), self.take_work(bid), self.take_work(bid)
            self.state_map[bid] = {"kind": "pi", "error": err, "proportional": prop, "integral": integ, "out_min": lo, "out_max": hi}
            self.emit(f"{err} = {pv} - {sp}" if p.get("action") == "direct" else f"{err} = {sp} - {pv}")
            self.emit(f"{prop} = {err} * {vals['kp']}")
            self.emit(f"IF INTERVAL( 00:00:01 ) THEN {integ} = {integ} + {err} * {vals['ki']}")
            clamp = f"MAX( {lo} - {prop} , MIN( {hi} - {prop} , {integ} ) )"
            if enable is None:
                self.emit(f"{integ} = {clamp}")
                return f"MAX( {lo} , MIN( {hi} , {prop} + {integ} ) )"
            out = self.take_work(bid)
            self.state_map[bid]["out"] = out
            self.emit(f"IF {enable} THEN {integ} = {clamp} ELSE {integ} = 0")
            self.emit(f"IF {enable} THEN {out} = MAX( {lo} , MIN( {hi} , {prop} + {integ} ) ) ELSE {out} = {lo}")
            return out
        if t == "select":
            s, a, b = self.input(bid, "s"), self.simple(bid, self.input(bid, "a")), self.simple(bid, self.input(bid, "b"))
            w = self.take_work(bid)
            self.emit(f"IF {s} THEN {w} = {a} ELSE {w} = {b}")
            return w
        return None

    def check_write(self, b: dict) -> str | None:
        bid = b["id"]
        parsed = parse_point((b.get("params") or {}).get("point"))
        if not parsed:
            self.err("t3_point", "Write point must be OUT1-64 or VAR1-128", bid, "point")
            return None
        kind, idx = parsed
        name = f"{kind}{idx}"
        if kind == "IN":
            self.err("t3_write_input", "Inputs cannot be written", bid, "point")
            return None
        allowed, reason = self.guard(kind, idx)
        if not allowed:
            self.err("t3_write_denied", f"{name} is not writable: {reason}", bid, "point")
            return None
        if name in self.writes:
            self.err("t3_write_twice", f"{name} is written by two blocks", bid, "point")
            return None
        self.writes[name] = bid
        return name

    def check_modbus_write(self, b: dict) -> str | None:
        name, error = modbus_point(self.panel, b.get("params") or {}, write=True)
        if error:
            self.err("modbus_point", error, b["id"], "address")
            return None
        if name in self.writes:
            self.err("t3_write_twice", f"{name} is written by two blocks", b["id"], "address")
            return None
        self.writes[name] = b["id"]
        return name

    def run(self) -> None:
        cfg = self.sheet.get("t3000") or {}
        try:
            work = expand_points(cfg.get("work_vars"))
        except ValueError as e:
            self.err("t3_work_vars", str(e))
            work = []
        for w in work:
            kind, idx = parse_point(w)  # type: ignore[misc]
            allowed, reason = self.guard(kind, idx)
            if kind != "VAR" or not allowed:
                self.err("t3_work_denied", f"Work variable {w} is not usable: {reason if not allowed else 'must be a VAR'}")
        writers = sorted((b for b in self.blocks.values() if b.get("type") in ("t3_write", "modbus_write")), key=lambda x: x["id"])
        if not writers:
            self.err("t3_no_write", "A T3000 program needs at least one T3000 Write or Modbus Write block")
        targets = [(b, self.check_modbus_write(b) if b.get("type") == "modbus_write" else self.check_write(b)) for b in writers]
        for b in self.blocks.values():
            if b.get("type") == "t3_read":
                parsed = parse_point((b.get("params") or {}).get("point"))
                if parsed:
                    self.reads.add(f"{parsed[0]}{parsed[1]}")
        self.work_free = [w for w in work if w not in self.writes and w not in self.reads]
        for w in work:
            if w in self.writes or w in self.reads:
                self.err("t3_work_conflict", f"Work variable {w} is also used by a read or write block")
        for b, name in targets:
            source = self.input(b["id"], "in")
            if not name:
                continue
            if b.get("type") == "modbus_write":
                scale = (b.get("params") or {}).get("scale", 1)
                if scale not in (None, "", 1, 1.0):
                    try:
                        source = f"( {source} * {format_number(scale)} )"
                    except (TypeError, ValueError) as e:
                        self.err("t3_number", str(e), b["id"], "scale")
            if (b.get("params") or {}).get("value_type") == "boolean":
                self.emit(f"IF {source} THEN {name} = 1 ELSE {name} = 0")
            else:
                self.emit(f"{name} = {source}")


def generate(sheet: dict, guard: Guard) -> dict[str, Any]:
    """Return {'source', 'issues', 'reads', 'writes', 'work_vars'}. Source is empty when there are errors."""
    sheet, origin, pin_issues = panels.expand(sheet)  # controller faceplates -> T3000 Read / Write blocks
    gen = _Gen(sheet, guard)
    gen.issues.extend(pin_issues)
    if (sheet.get("t3000") or {}).get("imported"):
        gen.err("t3_imported", "This sheet documents an existing controller program and cannot be compiled or loaded")
    else:
        gen.run()
    panels.map_issues(gen.issues, origin)
    errors = [i for i in gen.issues if i["severity"] == "error"]
    source = ""
    if not errors:
        numbered = [f"{(n + 1) * 10} {line}" for n, line in enumerate(gen.lines)]
        numbered.append(f"{(len(gen.lines) + 1) * 10} END")
        source = "\r\n".join(numbered) + "\r\n"
    return {"source": source, "issues": gen.issues, "reads": sorted(gen.reads), "writes": sorted(gen.writes), "work_vars": gen.work_used,
            "modbus_reads": sorted(gen.modbus_reads), "panel": gen.panel,
            "state_map": gen.state_map if not errors else {}}
