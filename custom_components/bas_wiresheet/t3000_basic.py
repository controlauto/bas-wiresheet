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

from . import macros, panels

POINT_RE = re.compile(r"^(IN|OUT|VAR)([1-9]\d*)$")
POINT_LIMITS = {"IN": 64, "OUT": 64, "VAR": 128}
COMPARE_OPS = {">": ">", ">=": ">=", "<": "<", "<=": "<=", "==": "=", "!=": "<>"}
LOCAL_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,19}$")
T3000_BLOCKS = {"t3_read", "t3_write", "edge", "counter", "alarm", "cb_local_read", "cb_local_write", "modbus_read", "modbus_write", "constant", "compare", "and", "or", "not", "on_delay", "off_delay", "status_display", "xor", "latch", "range_check",
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


SIMPLE_RE = re.compile(r"^(-?\d+(\.\d+)?|[A-Z_][A-Z0-9_]*)$")  # a number, a point or a (local) variable
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
        self.chains: dict[str, tuple[str, list[str]]] = {}
        self.cse: dict[tuple[str, str], str] = {}  # stored expression -> work variable (each is assigned once per scan)
        # Editable copies of controller programs: conditional assignments are rebuilt in place, in program order
        # (IF c THEN VAR5 = x), like the hand-written original, instead of one work variable per branch.
        self.inplace = bool((sheet.get("t3000") or {}).get("work_locals")) and any(b.get("assign") for b in self.blocks.values())
        self.stable: dict[tuple, str] = {}
        self.inplace_targets: set[str] = set()
        self.cond_users: dict[tuple, int] = {}
        self._node_stack: list[str] = []
        self.work_locals = bool((sheet.get("t3000") or {}).get("work_locals"))
        self.local_n = 0
        self.work_locals_used: list[str] = []
        self.local_names = {str((b.get("params") or {}).get("name", "")).upper() for b in self.blocks.values() if b.get("type") in ("cb_local_read", "cb_local_write")}

    def err(self, code: str, message: str, block: str | None = None, param: str | None = None) -> None:
        item: dict[str, Any] = {"severity": "error", "code": code, "message": message}
        if block:
            item["block"] = block
        if param:
            item["param"] = param
        self.issues.append(item)

    def emit(self, text: str) -> None:
        self.lines.append(text)

    def take_work(self, bid: str, timer: bool = False) -> str:
        """A work variable. With t3000.work_locals, values that are not timed are kept in program-local variables
        (they never collide with other programs); TIME-ON / TIME-OFF need a VAR point (T3000 does not time locals)."""
        if self.work_locals and not timer:
            self.local_n += 1
            name = f"WK{self.local_n}"
            while name in self.local_names:
                self.local_n += 1
                name = f"WK{self.local_n}"
            self.work_locals_used.append(name)
            return name
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
        if ("bool", expr) in self.cse:  # the same condition was already stored in this scan
            return self.cse[("bool", expr)]
        w = self.take_work(bid)
        self.emit(f"IF {expr} THEN {w} = 1 ELSE {w} = 0")
        self.cse[("bool", expr)] = f"( {w} > 0 )"
        return f"( {w} > 0 )"

    def simple(self, bid: str, expr: str) -> str:
        """Return a point or constant, storing any other expression in a work variable."""
        if SIMPLE_RE.match(expr):
            return expr
        if ("num", expr) in self.cse:
            return self.cse[("num", expr)]
        w = self.take_work(bid)
        self.emit(f"{w} = {expr}")
        self.cse[("num", expr)] = w
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
        if t == "cb_local_read":
            # a local variable of the program, as it was at the start of the scan (local writes are emitted last)
            name = self.local_name(b)
            return name or "0"
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
            # a chain of the same operator stays flat (a OR b OR c): its listing reads the same without brackets;
            # only a mixed AND/OR operand is stored first
            parts: list[str] = []
            for q in "abcd":
                if q in "ab" or (bid, q) in self.incoming:
                    x = self.input(bid, q)
                    if self.chains.get(x, ("", []))[0] == t:
                        parts.extend(self.chains[x][1])
                    else:
                        parts.append(self.atom(bid, x))
            # constant and complementary operands: x OR NOT x is always true (the line after an IF ... GOTO is
            # always reached), 1 in an AND and 0 in an OR change nothing
            if t == "or" and ("1" in parts or any(f"( NOT {q} )" in parts for q in parts)):
                return "1"
            if t == "and" and ("0" in parts or any(f"( NOT {q} )" in parts for q in parts)):
                return "0"
            parts = [q for q in parts if q != ("1" if t == "and" else "0")]
            if not parts:
                return "1" if t == "and" else "0"
            if len(parts) == 1:
                return parts[0]
            expr = self.group("( " + f" {t.upper()} ".join(parts) + " )")
            self.chains[expr] = (t, parts)
            return expr
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
            w = self.take_work(bid, timer=True)
            self.emit(f"IF {source} THEN {w} = 1 ELSE {w} = 0")
            if t == "on_delay":
                self.state_map[bid] = {"kind": "on_delay", "timer": w, "delay_s": float(delay)}
                return self.group(f"( ( {w} > 0 ) AND ( TIME-ON( {w} ) >= {delay} ) )")
            latch = self.take_work(bid)
            self.state_map[bid] = {"kind": "off_delay", "timer": w, "latch": latch, "delay_s": float(delay)}
            self.emit(f"IF {w} > 0 THEN {latch} = 1")
            self.emit(f"IF ( {w} = 0 ) AND ( TIME-OFF( {w} ) >= {delay} ) THEN {latch} = 0")
            return f"( {latch} > 0 )"
        if t == "alarm":
            # Same timing as On Delay: active once the condition has stayed true for delay_s (0 = immediately).
            try:
                delay = format_number(p.get("delay_s", 0))
            except (TypeError, ValueError) as e:
                self.err("t3_number", str(e), bid, "delay_s")
                return "0"
            w = self.take_work(bid, timer=True)
            self.emit(f"IF {self.input(bid, 'in')} THEN {w} = 1 ELSE {w} = 0")
            self.state_map[bid] = {"kind": "on_delay", "timer": w, "delay_s": float(delay)}
            return self.group(f"( ( {w} > 0 ) AND ( TIME-ON( {w} ) >= {delay} ) )")
        if t == "edge":
            # P = previous input (0 none yet, 1 OFF, 2 ON); T = 1 for the one scan with the edge; L = the pulse.
            # The pulse ends pulse_s after the last edge (TIME-OFF of T restarts on every edge, so a new edge extends it).
            try:
                pulse = float(p.get("pulse_s", 5))
            except (TypeError, ValueError):
                pulse = -1
            if pulse <= 0:
                self.err("t3_number", "Pulse length must be above 0 s", bid, "pulse_s")
                return "0"
            kind = p.get("edge", "rising")
            x = self.atom(bid, self.input(bid, "in"))
            wp, wt, wl = self.take_work(bid), self.take_work(bid, timer=True), self.take_work(bid)
            if kind == "falling":
                self.emit(f"IF ( {wp} = 2 ) AND ( NOT {x} ) THEN {wt} = 1 ELSE {wt} = 0")
            else:
                self.emit(f"IF ( {wp} = 1 ) AND {x} THEN {wt} = 1 ELSE {wt} = 0")
                if kind == "both":
                    self.emit(f"IF ( {wp} = 2 ) AND ( NOT {x} ) THEN {wt} = 1")
            self.emit(f"IF {x} THEN {wp} = 2 ELSE {wp} = 1")
            self.emit(f"IF {wt} > 0 THEN {wl} = 1")
            self.emit(f"IF ( {wt} = 0 ) AND ( TIME-OFF( {wt} ) >= {format_number(pulse)} ) THEN {wl} = 0")
            self.state_map[bid] = {"kind": "var_bool", "var": wl}
            return f"( {wl} > 0 )"
        if t == "counter":
            # N = count of rising edges, P = previous input (0 none yet, 1 OFF, 2 ON). Reset wins and sets 0.
            x = self.atom(bid, self.input(bid, "in"))
            wn, wp = self.take_work(bid), self.take_work(bid)
            self.emit(f"IF ( {wp} = 1 ) AND {x} THEN {wn} = {wn} + 1")
            self.emit(f"IF {x} THEN {wp} = 2 ELSE {wp} = 1")
            if (bid, "reset") in self.incoming:
                self.emit(f"IF {self.input(bid, 'reset')} THEN {wn} = 0")
            self.state_map[bid] = {"kind": "var_num", "var": wn}
            return wn
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
        if t == "select" and self.inplace and self.blocks[bid].get("assign"):
            blk = self.blocks[bid]
            name = self.target_name(blk)
            if name:
                if parse_point(name):
                    allowed, reason = self.guard(*parse_point(name))
                    if not allowed:
                        self.err("t3_write_denied", f"{name} is not writable: {reason}", bid)
                        return "0"
                return self.build_inplace_select(blk, name)
        if t == "select":
            s, a, b = self.input(bid, "s"), self.simple(bid, self.input(bid, "a")), self.simple(bid, self.input(bid, "b"))
            w = self.take_work(bid)
            self.emit(f"IF {s} THEN {w} = {a} ELSE {w} = {b}")
            return w
        return None

    def assigned(self, name: str) -> None:
        """name was changed in place: forget stored expressions that read it (they hold the old value)."""
        rx = re.compile(rf"(?<![A-Z0-9_.]){re.escape(name)}(?![A-Z0-9_])")
        for key in [k for k in self.cse if rx.search(k[1])]:
            del self.cse[key]

    def target_name(self, b: dict) -> str | None:
        target = str(b.get("assign") or "")
        if target.startswith("L:"):
            name = target[2:].upper()
            return name if LOCAL_RE.match(name) else None
        parsed = parse_point(target)
        return f"{parsed[0]}{parsed[1]}" if parsed else None

    def build_inplace_select(self, b: dict, name: str) -> str:
        """One conditional assignment of a point (or local) and the IFs nested in it, written in place, as in the
        original program: IF c THEN P = x [ELSE P = y]; IF reached AND c THEN P = x for nested ones. Branches that keep
        the value write nothing. A condition is stored before the first assignment only when several lines use it and
        it reads a point those lines change (in the original it is evaluated once)."""
        bid, keep = b["id"], b.get("keep")
        prev_src = self.incoming.get((bid, keep)) if keep in ("a", "b") else None
        if prev_src is not None:
            prev = self.output(*prev_src)  # the earlier assignments of this point come first
            if prev != name:
                self.emit(f"{name} = {prev}")
                self.assigned(name)
        self.emit_node(bid, name, prev_src, [])
        self.assigned(name)
        if parse_point(name):
            self.writes.setdefault(name, name)
        return name

    def nested(self, src, name: str, prev_src) -> bool:
        inner = self.blocks.get(src[0]) if src else None
        if not (inner and src[1] == "out" and inner.get("type") == "select" and inner.get("assign") and self.target_name(inner) == name):
            return False
        if (src[0], "out") in self.memo:
            return False  # already written (used elsewhere): read it as a value
        outer = self.blocks.get(self._node_stack[-1]) if self._node_stack else None
        if outer is not None and (inner.get("seq") or [None])[0] != (outer.get("seq") or [None])[0]:
            return False  # an earlier line's assignment: the point already holds it
        k = inner.get("keep")
        return self.incoming.get((src[0], k)) == prev_src if k in ("a", "b") else True

    def lines_of(self, bid: str, name: str, prev_src) -> int:
        """How many assignment lines this node writes (to know whether its condition is read more than once)."""
        self._node_stack.append(bid)
        try:
            return self._lines_of(bid, name, prev_src)
        finally:
            self._node_stack.pop()

    def _lines_of(self, bid: str, name: str, prev_src) -> int:
        blk, n = self.blocks[bid], 0
        for port in ("a", "b"):
            src = self.incoming.get((bid, port))
            if port == blk.get("keep") or src is None or src == prev_src:
                continue
            n += self.lines_of(src[0], name, prev_src) if self.nested(src, name, prev_src) else 1
        return n

    def emit_node(self, bid: str, name: str, prev_src, conds: list[str]) -> None:
        self._node_stack.append(bid)
        try:
            self._emit_node(bid, name, prev_src, conds)
        finally:
            self._node_stack.pop()

    def _emit_node(self, bid: str, name: str, prev_src, conds: list[str]) -> None:
        blk = self.blocks[bid]
        keep = blk.get("keep")
        csrc = self.incoming.get((bid, "s"))
        if csrc is None:
            self.err("input_unconnected", "Input s is not connected", bid)
            return
        multi = self.lines_of(bid, name, prev_src) > 1 or self.cond_users.get(csrc, 0) > 1
        c = self.cond(csrc, bid, store=multi)
        ports = [p for p in ("a", "b") if p != keep and self.incoming.get((bid, p)) is not None and self.incoming.get((bid, p)) != prev_src]
        leaf = {p: not self.nested(self.incoming[(bid, p)], name, prev_src) for p in ports}
        prefix = self.and_all(bid, conds)
        if ports == ["a", "b"] and leaf["a"] and leaf["b"]:
            va, vb = self.output(*self.incoming[(bid, "a")]), self.output(*self.incoming[(bid, "b")])
            if not conds and c in ("1", "0"):
                v = va if c == "1" else vb
                if v != name:
                    self.emit(f"{name} = {v}")
                return
            if not conds:
                if va == name:
                    self.emit(f"IF ( NOT {self.atom(bid, c)} ) THEN {name} = {vb}")
                elif vb == name:
                    self.emit(f"IF {c} THEN {name} = {va}")
                else:
                    self.emit(f"IF {c} THEN {name} = {va} ELSE {name} = {vb}")
                return
        for p in ports:
            here = conds + [c if p == "a" else f"( NOT {self.atom(bid, c)} )"]
            src = self.incoming[(bid, p)]
            if not leaf[p]:
                self.emit_node(src[0], name, prev_src, here)
                continue
            v = self.output(*src)
            if v == name:
                continue  # the point already holds this value
            cond = self.and_all(bid, here)
            if cond == "0":
                continue  # never runs
            self.emit(f"{name} = {v}" if cond == "1" else f"IF {cond} THEN {name} = {v}")
        del prefix

    def and_all(self, bid: str, conds: list[str]) -> str:
        conds = [c for c in conds if c != "1"]
        if not conds:
            return "1"
        if len(conds) == 1:
            return conds[0]
        return self.group("( " + " AND ".join(self.atom(bid, c) for c in conds) + " )")

    def cond(self, src, bid: str, store: bool) -> str:
        if src in self.stable:
            return self.stable[src]
        expr = self.output(*src)
        if store and any(re.search(rf"(?<![A-Z0-9_.]){re.escape(t)}(?![A-Z0-9_])", expr) for t in self.inplace_targets):
            w = self.take_work(bid)
            self.emit(f"IF {expr} THEN {w} = 1 ELSE {w} = 0")
            expr = f"( {w} > 0 )"
            self.stable[src] = expr
        return expr

    def local_name(self, b: dict) -> str | None:
        name = str((b.get("params") or {}).get("name", "")).strip().upper()
        if not LOCAL_RE.match(name) or POINT_RE.match(name) or name in ("IF", "THEN", "ELSE", "AND", "OR", "NOT", "END", "GOTO", "TIME"):
            self.err("t3_local", f"Local variable name {name or '(empty)'} is not valid (letters, digits and _, up to 20)", b["id"], "name")
            return None
        return name

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
        # local variable writes go last, so every read of a local in this scan sees the value from the start of the scan
        writers = sorted((b for b in self.blocks.values() if b.get("type") in ("t3_write", "modbus_write", "cb_local_write")),
                         key=lambda x: (x.get("type") == "cb_local_write", x["id"]))
        if not writers:
            self.err("t3_no_write", "A T3000 program needs at least one T3000 Write or Modbus Write block")
        targets = [(b, self.check_modbus_write(b) if b.get("type") == "modbus_write" else self.local_name(b) if b.get("type") == "cb_local_write"
                    else self.check_write(b)) for b in writers]
        for b in self.blocks.values():
            if b.get("type") == "t3_read":
                parsed = parse_point((b.get("params") or {}).get("point"))
                if parsed:
                    self.reads.add(f"{parsed[0]}{parsed[1]}")
        self.work_free = [w for w in work if w not in self.writes and w not in self.reads]
        for w in work:
            if w in self.writes or w in self.reads:
                self.err("t3_work_conflict", f"Work variable {w} is also used by a read or write block")
        if self.inplace:
            # program order: conditional assignments and writes as the original program ran them
            def seq(blk: dict) -> tuple:
                if blk.get("seq"):
                    return tuple(blk["seq"])
                src = self.incoming.get((blk["id"], "in"))
                return tuple(self.blocks[src[0]].get("seq") or (10 ** 9, 0)) if src else (10 ** 9, 0)
            assigns = [b for b in self.blocks.values() if b.get("type") == "select" and b.get("assign")]
            self.inplace_targets = {n for n in (self.target_name(b) for b in assigns) if n}
            self.cond_users = {}
            for b_ in assigns:
                src_ = self.incoming.get((b_["id"], "s"))
                if src_:
                    self.cond_users[src_] = self.cond_users.get(src_, 0) + 1
            # an IF nested in a conditionally reached line belongs to that line's assignment: it is written only
            # through it (with the line's condition), never on its own
            inner = set()
            for b_ in assigns:
                for port in ("a", "b"):
                    if port == b_.get("keep"):
                        continue  # the value the point had before: an earlier statement, written at its own place
                    src_ = self.incoming.get((b_["id"], port))
                    blk_ = self.blocks.get(src_[0]) if src_ else None
                    if (blk_ is not None and blk_.get("type") == "select" and blk_.get("assign") and self.target_name(blk_) == self.target_name(b_)
                            and (blk_.get("seq") or [None])[0] == (b_.get("seq") or [None])[0]):
                        inner.add(blk_["id"])  # an IF of the same line; an assignment of an earlier line is written at its own place
            roots = [b for b in assigns if b["id"] not in inner]
            order = sorted([("a", b) for b in roots] + [("w", b) for b, _ in targets], key=lambda x: (*seq(x[1]), x[0] == "w"))
            names = dict((b["id"], n) for b, n in targets)
            targets = []
            pending_rems = list(getattr(self, "rems", []))
            for kind, b in order:
                while pending_rems and tuple(pending_rems[0].get("seq") or (0, 0)) <= seq(b):
                    self.emit(_rem_text(pending_rems.pop(0).get("text", "")))
                if kind == "a":
                    self.output(b["id"], "out")
                else:
                    targets.append((b, names[b["id"]]))
                    self.emit_write(b, names[b["id"]])
            for r in pending_rems:
                self.emit(_rem_text(r.get("text", "")))
            return
        for b, name in targets:
            self.emit_write(b, name)

    def emit_write(self, b: dict, name: str | None) -> None:
        if True:
            source = self.input(b["id"], "in")
            if not name:
                return
            if b.get("type") == "modbus_write":
                scale = (b.get("params") or {}).get("scale", 1)
                if scale not in (None, "", 1, 1.0):
                    try:
                        source = f"( {source} * {format_number(scale)} )"
                    except (TypeError, ValueError) as e:
                        self.err("t3_number", str(e), b["id"], "scale")
            if source == name:
                return  # already assigned in place
            if (b.get("params") or {}).get("value_type") == "boolean":
                self.emit(f"IF {source} THEN {name} = 1 ELSE {name} = 0")
            else:
                self.emit(f"{name} = {source}")
            if self.inplace:
                self.assigned(name)


_GUARDED_RE = re.compile(r"^IF (.+) THEN ([A-Z_][A-Z0-9_]*) = (.+)$")


def _rem_text(text: str) -> str:
    """A comment line as T3000 accepts it: REM and printable ASCII."""
    body = re.sub(r"^REM\b\s*", "", str(text).strip(), flags=re.I)
    body = "".join(ch for ch in body.upper() if 32 <= ord(ch) < 127)[:200]
    return f"REM {body}".rstrip()


def _skip_runs(lines: list[str], min_run: int = 3) -> list[str]:
    """IF c THEN A = 1 / IF c THEN B = 2 / IF c THEN C = 3 -> IF NOT c THEN GOTO after; A = 1; B = 2; C = 3 (as programs
    are written by hand). Only when no assignment in the run changes something the condition reads."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = _GUARDED_RE.match(lines[i])
        if not m or " ELSE " in lines[i]:
            out.append(lines[i])
            i += 1
            continue
        cond, j, run = m.group(1), i, []
        while j < len(lines):
            mj = _GUARDED_RE.match(lines[j])
            if not mj or " ELSE " in lines[j] or mj.group(1) != cond:
                break
            if re.search(rf"(?<![A-Z0-9_.]){re.escape(mj.group(2))}(?![A-Z0-9_])", cond):
                break  # this assignment changes the condition: the rest must test it again
            run.append(f"{mj.group(2)} = {mj.group(3)}")
            j += 1
        if len(run) < min_run:
            out.append(lines[i])
            i += 1
            continue
        target = len(out) + 1 + len(run)
        out.append(f"IF ( NOT {cond} ) THEN GOTO @{target}")
        out.extend(run)
        i = j
    return out


def generate(sheet: dict, guard: Guard, rems: bool = True) -> dict[str, Any]:
    """Return {'source', 'issues', 'reads', 'writes', 'work_vars'}. Source is empty when there are errors."""
    sheet, morigin, _, macro_issues = macros.expand(sheet)  # HVAC application blocks -> basic blocks
    sheet, origin, pin_issues = panels.expand(sheet)  # controller faceplates -> T3000 Read / Write blocks
    pin_issues = macro_issues + pin_issues
    gen = _Gen(sheet, guard)
    gen.rems = sorted(((sheet.get("t3000") or {}).get("rems") or []) if rems else [], key=lambda r: tuple(r.get("seq") or (0, 0)))
    gen.issues.extend(pin_issues)
    if (sheet.get("t3000") or {}).get("imported"):
        gen.err("t3_imported", "This sheet documents an existing controller program and cannot be compiled or loaded")
    else:
        gen.run()
    panels.map_issues(gen.issues, origin)
    gen.issues = macros.map_issues(gen.issues, morigin)
    errors = [i for i in gen.issues if i["severity"] == "error"]
    source = ""
    if not errors:
        lines = _skip_runs(gen.lines) if gen.inplace else gen.lines
        numbered = [f"{(n + 1) * 10} {re.sub(r'GOTO @(\d+)', lambda m: f'GOTO {(int(m.group(1)) + 1) * 10}', line)}" for n, line in enumerate(lines)]
        numbered.append(f"{(len(lines) + 1) * 10} END")
        source = "\r\n".join(numbered) + "\r\n"
    return {"source": source, "issues": gen.issues, "reads": sorted(gen.reads), "writes": sorted(gen.writes), "work_vars": gen.work_used,
            "modbus_reads": sorted(gen.modbus_reads), "panel": gen.panel,
            "state_map": gen.state_map if not errors else {}}
