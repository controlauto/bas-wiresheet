"""T3000 program byte code -> wire sheet (no Home Assistant imports).

Works from the byte code, not the listing, because T3000's listing drops some parentheses. One program scan becomes
a data-flow graph: a point read before it is assigned is a T3000 Read block, the final value of each assigned point
is a T3000 Write block, and IF/ELSE becomes Select blocks.

Anything that cannot be represented exactly becomes an "Unmapped Code" block holding the original line. A sheet from
this module is documentation of an existing program and cannot be loaded back to a controller.
"""
from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass, field
from typing import Any

from . import panels

ASSIGNAR, ASSIGN, IF, ELSE, REM, ENDPRG, GOTO, GOTOIF = 0x08, 0x09, 0x0E, 0x10, 0x1A, 0x17, 0x13, 0x2A
LOCAL_FLOAT, LOCAL_LONG = 0x83, 0x84
POINT, CONST = 0x9C, 0x9D
GE, LE, POW, MOD, MUL, DIV, NEG, PLUS, MINUS, LT, GT, EQ, NE, XOR, AND, OR, NOT = (
    0x60, 0x61, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6B, 0x6C, 0x6D, 0x6E, 0x71, 0x72, 0x73, 0x74, 0x75, 0x76)
ABS, MAX, MIN, TIME_ON, TIME_OFF = 0x32, 0x37, 0x38, 0x3C, 0x3D
POINT_KINDS = {1: "OUT", 2: "IN", 3: "VAR"}
COMPARE = {GT: ">", GE: ">=", LT: "<", LE: "<=", EQ: "==", NE: "!="}
BINARY = {PLUS: "add", MINUS: "subtract", MUL: "multiply", DIV: "divide"}
DELIMS = (0x01, 0xFE, 0xFF)
MAX_IMPORT_BLOCKS = 1500
COL_STEP, ROW_STEP, BAND_GAP = 270, 165, 70

# Saved T3000 .prog files, format version 8
PROG_MAGIC = b"\x55\xff\x08"
PROG_SIZE = 67184
PROG_STATUS_OFFSET = 3 + 64 * 46 + 64 * 45 + 128 * 39
PROG_CODE_OFFSET = 33956


class Unsupported(Exception):
    """This statement cannot be represented exactly."""


@dataclass
class Stmt:
    kind: str
    line: int = 0
    target: str = ""
    expr: Any = None
    then: list = field(default_factory=list)
    other: list = field(default_factory=list)


def _used(code: bytes) -> int:
    base = code[0] | code[1] << 8 if len(code) >= 2 else 0
    return 2 + base if 0 < base <= 2000 else 0


def _locals(code: bytes) -> dict[int, dict]:
    """Named local variables: [type][4-byte value][name NUL]. The byte code addresses the value."""
    base = code[0] | code[1] << 8
    pos = 2 + base + 3
    size = code[pos] | code[pos + 1] << 8
    start, i, out = pos + 2, 0, {}
    while i < size:
        kind = code[start + i]
        if kind not in (LOCAL_FLOAT, LOCAL_LONG, 0x85, 0x86):
            break
        width = {0x85: 2, 0x86: 1}.get(kind, 4)
        name_at = start + i + 1 + width
        end = code.find(b"\0", name_at, start + size)
        if end < 0:
            break
        raw = code[start + i + 1:name_at]
        value = struct.unpack("<i", raw)[0] / 1000 if width == 4 else None
        out[i + 1] = {"name": code[name_at:end].decode("latin-1"), "kind": kind, "initial": value}
        i = end - start + 1
    return out


def _local(code: bytes, pos: int, table: dict[int, dict]) -> tuple[str, int]:
    v = table.get(code[pos + 1] | code[pos + 2] << 8)
    if not v or v["kind"] not in (LOCAL_FLOAT, LOCAL_LONG):
        raise Unsupported("integer or unknown local variable")
    return "L:" + v["name"], pos + 3


def _timer_table(code: bytes) -> dict[int, Any]:
    base = code[0] | code[1] << 8
    pos = 2 + base + 3
    pos += 2 + (code[pos] | code[pos + 1] << 8)
    size = code[pos] | code[pos + 1] << 8
    start, out, i = pos + 2, {}, 0
    while i < size:
        expr, used = _expr(code, start + i, {}, _locals(code))
        i = used - start + 1  # past the delimiter
        out[i + 1] = expr  # the timer value sits after the one-byte state flag
        i += 5
    return out


def timer_key(expr: Any) -> str:
    """Stable name of a timer's watched expression: the point name, or a digest of a complex expression."""
    if isinstance(expr, tuple) and len(expr) == 2 and expr[0] == "point":
        return str(expr[1])
    return "expr:" + hashlib.sha256(repr(expr).encode()).hexdigest()[:12]


def program_state(code: bytes) -> dict:
    """Runtime state a running program keeps in its code area (verified on DA firmware, 2026-11-24).

    Timer entry: watched expression, 0xFF, one byte = the expression's state at the last scan, then a 32-bit
    count of milliseconds since that state last changed. TIME-ON is that count while the state is 1, TIME-OFF
    while it is 0. Named local variables hold their current value in place of the initial value.
    """
    if not _used(code):
        return {"timers": {}, "locals": {}}
    timers: dict[str, dict] = {}
    for key, expr in _timer_table(code).items():
        base = code[0] | code[1] << 8
        pos = 2 + base + 3
        pos += 2 + (code[pos] | code[pos + 1] << 8)
        at = pos + 2 + key - 1
        state = code[at]
        count = struct.unpack("<I", code[at + 1:at + 5])[0]
        timers[timer_key(expr)] = {"on": state != 0, "elapsed_s": round(count / 1000, 1)}
    local = {v["name"]: v["initial"] for v in _locals(code).values() if v["initial"] is not None}
    return {"timers": timers, "locals": local}


def _point(code: bytes, pos: int) -> tuple[str, int]:
    kind = POINT_KINDS.get(code[pos + 2])
    if not kind:
        raise Unsupported("point type")
    return f"{kind}{code[pos + 1] + 1}", pos + 3


def _expr(code: bytes, pos: int, timers: dict[int, Any], local: dict[int, dict]) -> tuple[Any, int]:
    """Postfix expression -> tree. Returns the tree and the position of the delimiter."""
    stack: list[Any] = []
    while code[pos] not in DELIMS:
        op = code[pos]
        if op == POINT:
            name, pos = _point(code, pos)
            stack.append(("point", name))
        elif op in (LOCAL_FLOAT, LOCAL_LONG, 0x85, 0x86):
            name, pos = _local(code, pos, local)
            stack.append(("point", name))
        elif op == CONST:
            stack.append(("const", struct.unpack("<i", code[pos + 1:pos + 5])[0] / 1000))
            pos += 5
        elif op in BINARY or op in COMPARE or op in (AND, OR):
            b, a = stack.pop(), stack.pop()
            stack.append(("bin", op, a, b))
            pos += 1
        elif op in (NOT, NEG, ABS):
            stack.append(("un", op, stack.pop()))
            pos += 1
        elif op in (MAX, MIN):
            n = code[pos + 1]
            args = [stack.pop() for _ in range(n)][::-1]
            stack.append(("fn", op, args))
            pos += 2
        elif op in (TIME_ON, TIME_OFF):
            off = struct.unpack("<h", code[pos + 1:pos + 3])[0]
            if off not in timers:
                raise Unsupported("timer")
            stack.append(("timer", op, timers[off]))
            pos += 3
        else:
            raise Unsupported(f"operator 0x{op:02x}")
    if len(stack) != 1:
        raise Unsupported("expression")
    return stack[0], pos


def _statements(code: bytes, pos: int, end: int, timers: dict, line: int, local: dict) -> list[Stmt]:
    out: list[Stmt] = []
    while pos < end:
        op = code[pos]
        if op == 0xFF:
            pos += 1
        elif op == 0x01:
            line = code[pos + 1] | code[pos + 2] << 8
            pos += 3
        elif op == 0xFE:
            pos += 1
        elif op == ENDPRG:
            out.append(Stmt("end", line))
            pos += 1
        elif op == REM:
            pos += 2 + code[pos + 1]
        elif op in (GOTO, GOTOIF):
            out.append(Stmt("goto", line, target=str(code[pos + 1] | code[pos + 2] << 8)))
            pos += 3
        elif op in (ASSIGN, ASSIGNAR):
            if code[pos + 1] == POINT:
                target, p = _point(code, pos + 1)
            elif code[pos + 1] in (LOCAL_FLOAT, LOCAL_LONG, 0x85, 0x86):
                target, p = _local(code, pos + 1, local)
            else:
                raise Unsupported("assignment target")
            expr, pos = _expr(code, p, timers, local)
            out.append(Stmt("assign", line, target, expr))
        elif op == IF:
            cond, p = _expr(code, pos + 1, timers, local)
            target = code[p + 1] | code[p + 2] << 8
            # A false condition jumps to the ELSE body, just after the 4-byte ELSE header [ELSE FF lo hi]
            has_else = target - 4 >= p + 3 and code[target - 4] == ELSE and code[target - 3] == 0xFF
            if has_else:
                after = code[target - 2] | code[target - 1] << 8
                if not target <= after <= end:
                    raise Unsupported("ELSE jump")
                then = _statements(code, p + 3, target - 4, timers, line, local)
                other = _statements(code, target, after, timers, line, local)
                pos = after
            else:
                then, other = _statements(code, p + 3, min(target, end), timers, line, local), []
                pos = target
            out.append(Stmt("if", line, expr=cond, then=then, other=other))
        else:
            raise Unsupported(f"statement 0x{op:02x}")
    return out


def _line_spans(code: bytes, listing: str) -> list[tuple[int, int, int, str]]:
    """(line, start, end, text) per program line, located through the numbers in T3000's listing."""
    rows = [(int(m.group(1)), l.strip()) for l in listing.splitlines() if (m := re.match(r"\s*(\d+)\b", l))]
    end, pos, spans = _used(code), 2, []
    for i, (num, text) in enumerate(rows):
        start = code.find(bytes([1]) + struct.pack("<H", num), pos, end)
        if start < 0:
            continue
        stop = end
        if i + 1 < len(rows):
            n = code.find(bytes([1]) + struct.pack("<H", rows[i + 1][0]), start + 3, end)
            stop = n if n >= 0 else end
        spans.append((num, start, stop, text))
        pos = start + 3
    return spans


class _Graph:
    def __init__(self, labels: dict[str, str]) -> None:
        self.timer_of: dict[str, dict] = {}
        self.labels, self.blocks, self.wires = labels, [], []
        self.env: dict[str, tuple] = {}
        self.reads: dict[str, tuple] = {}
        self.consts: dict[float, tuple] = {}
        self.n = 0
        self.notes: set[str] = set()
        self.initial: dict[str, float] = {}
        self.line = 0
        self.read_of: dict[str, str] = {}

    def block(self, typ: str, label: str | None = None, **params) -> str:
        self.n += 1
        bid = f"{typ.replace('_', '')}_{self.n}"
        # seq: program line and creation order, so a compiler can rebuild the program in its original statement order
        self.blocks.append({"id": bid, "type": typ, "label": label or typ.replace("_", " ").title(), "params": params, "position": {"x": 0, "y": 0},
                            "_line": self.line, "seq": [self.line, self.n]})
        return bid

    def wire(self, ref: tuple, block: str, port: str) -> None:
        self.wires.append({"id": f"w_{len(self.wires) + 1}", "from": {"block": ref[0], "port": ref[1]}, "to": {"block": block, "port": port}})

    def const(self, value: float) -> tuple:
        if value not in self.consts:
            self.consts[value] = (self.block("constant", f"{value:g}", value_type="number", value=value, unit=""), "value", "number")
        return self.consts[value]

    def read(self, point: str) -> tuple:
        if point not in self.reads and point.startswith("L:"):
            name = point[2:]
            self.reads[point] = (self.block("cb_local_read", name, name=name, initial=self.initial.get(name, 0.0)), "value", "number")
            self.read_of[self.reads[point][0]] = point
        if point not in self.reads:
            label = self.labels.get(point)
            self.reads[point] = (self.block("t3_read", f"{point} {label}" if label else point, point=point, value_type="number", unit=""), "value", "number")
            self.read_of[self.reads[point][0]] = point
        return self.reads[point]

    def value(self, point: str) -> tuple:
        return self.env.get(point) or self.read(point)

    def to_bool(self, ref: tuple) -> tuple:
        if ref[2] != "number":
            return ref
        b = self.block("compare", "Is True", op="!=")
        self.wire(ref, b, "a")
        self.wire(self.const(0.0), b, "b")
        return (b, "out", "boolean")

    def to_num(self, ref: tuple) -> tuple:
        if ref[2] == "number":
            return ref
        if ref[2] == "raw_bool":
            raise Unsupported("comparison used as a number (firmware value 0.001)")
        s = self.block("select", "True = 1")
        self.wire(ref, s, "s")
        self.wire(self.const(1.0), s, "a")
        self.wire(self.const(0.0), s, "b")
        return (s, "out", "number")

    def expr(self, e) -> tuple:
        k = e[0]
        if k == "point":
            return self.value(e[1])
        if k == "const":
            return self.const(e[1])
        if k == "timer":
            raise Unsupported("timer value used directly")
        if k == "un":
            if e[1] == NOT:
                b = self.block("not", "NOT")
                self.wire(self.to_bool(self.expr(e[2])), b, "in")
                return (b, "out", "raw_bool")
            x = self.to_num(self.expr(e[2]))
            if e[1] == ABS:
                b = self.block("absolute", "ABS")
                self.wire(x, b, "in")
                return (b, "out", "number")
            b = self.block("subtract", "Negate")
            self.wire(self.const(0.0), b, "a")
            self.wire(x, b, "b")
            return (b, "out", "number")
        if k == "fn":
            typ = "maximum" if e[1] == MAX else "minimum"
            refs = [self.to_num(self.expr(a)) for a in e[2]]
            cur = refs[0]
            for r in refs[1:]:
                b = self.block(typ, typ[:3].upper())
                self.wire(cur, b, "a")
                self.wire(r, b, "b")
                cur = (b, "out", "number")
            return cur
        op, left, right = e[1], e[2], e[3]
        if op in COMPARE and left[0] == "timer" and right[0] == "const" and op in (GE, GT):
            return self.timer(left, right[1])
        if op in (AND, OR):
            b = self.block("and" if op == AND else "or", "AND" if op == AND else "OR")
            self.wire(self.to_bool(self.expr(left)), b, "a")
            self.wire(self.to_bool(self.expr(right)), b, "b")
            return (b, "out", "boolean")
        a, c = self.to_num(self.expr(left)), self.to_num(self.expr(right))
        if op in COMPARE:
            b = self.block("compare", f"A {COMPARE[op]} B", op=COMPARE[op])
            self.wire(a, b, "a")
            self.wire(c, b, "b")
            return (b, "out", "raw_bool")
        if op == DIV:
            self.notes.add("Divide: T3000 returns 1 when the divisor is 0; the sheet shows Invalid Data in that case.")
        b = self.block(BINARY[op], BINARY[op].title())
        self.wire(a, b, "a")
        self.wire(c, b, "b")
        return (b, "out", "number")

    def timer(self, t, delay: float) -> tuple:
        if delay < 1 or delay > 86400:
            raise Unsupported("timer delay outside the On Delay range")
        src = self.to_bool(self.expr(t[2]))
        if t[1] == TIME_OFF:
            n = self.block("not", "NOT")
            self.wire(src, n, "in")
            src = (n, "out", "boolean")
        b = self.block("on_delay", f"{'TIME-ON' if t[1] == TIME_ON else 'TIME-OFF'} {delay:g} s", delay_s=delay)
        self.timer_of[b] = {"key": timer_key(t[2]), "off": t[1] == TIME_OFF}
        self.wire(src, b, "in")
        self.notes.add("Timers: T3000 updates timers at scan start, so controller timing can lag the sheet by up to 2 scans.")
        return (b, "out", "boolean")

    def both(self, a, b):
        """AND of two reachability conditions. True means always, False means never."""
        if a is False or b is False:
            return False
        if a is True:
            return b
        if b is True:
            return a
        g = self.block("and", "Reached")
        self.wire(a, g, "a")
        self.wire(b, g, "b")
        return (g, "out", "boolean")

    def either(self, a, b):
        if a is True or b is True:
            return True
        if a is False:
            return b
        if b is False:
            return a
        g = self.block("or", "Reached")
        self.wire(a, g, "a")
        self.wire(b, g, "b")
        return (g, "out", "boolean")

    def negate(self, a):
        if a is True or a is False:
            return not a
        g = self.block("not", "NOT")
        self.wire(a, g, "in")
        return (g, "out", "boolean")

    def guarded(self, stmt: Stmt, guard) -> None:
        """Run one plain statement, applying it only when this line is reached."""
        base = dict(self.env)
        self.run([stmt])
        if guard is True:
            return
        new, self.env = self.env, base
        for point, ref in new.items():
            if base.get(point) == ref:
                continue
            b = self.block("select", f"Reached → {point[2:] if point.startswith('L:') else point}")
            self.blocks[-1].update({"assign": point, "keep": "b"})  # IF reached THEN point = a (else it keeps b, its value so far)
            self.wire(guard, b, "s")
            self.wire(ref, b, "a")
            self.wire(base.get(point) or self.read(point), b, "b")
            self.env[point] = (b, "out", "number")

    def run(self, stmts: list[Stmt]) -> None:
        for s in stmts:
            if s.kind in ("goto", "end"):
                raise Unsupported("jump inside a compound statement")
            if s.kind == "assign":
                self.env[s.target] = self.to_num(self.expr(s.expr))
                continue
            cond = self.to_bool(self.expr(s.expr))
            base = dict(self.env)
            self.run(s.then)
            then_env = self.env
            self.env = dict(base)
            self.run(s.other)
            else_env = self.env
            self.env = dict(base)
            for point in sorted(set(then_env) | set(else_env)):
                t, e = then_env.get(point), else_env.get(point)
                if t == e and t is not None:
                    self.env[point] = t
                    continue
                prev = base.get(point) or self.read(point)
                b = self.block("select", f"IF → {point}")
                # a branch that does not assign keeps the value from before (its env still holds prev)
                self.blocks[-1].update({"assign": point, "keep": "a" if t is None or t == prev else "b" if e is None or e == prev else None})
                self.wire(cond, b, "s")
                self.wire(t or prev, b, "a")
                self.wire(e or prev, b, "b")
                self.env[point] = (b, "out", "number")


def _layout(blocks: list[dict], wires: list[dict]) -> None:
    """One horizontal band per program line, read left to right; bands wrap into columns."""
    by_id = {b["id"]: b for b in blocks}
    lines: dict[int, list[dict]] = {}
    for b in blocks:
        lines.setdefault(b["_line"], []).append(b)
    inside: dict[str, list[str]] = {b["id"]: [] for b in blocks}
    for w in wires:
        src, dst = by_id[w["from"]["block"]], by_id[w["to"]["block"]]
        if src["_line"] == dst["_line"]:
            inside[dst["id"]].append(src["id"])
    bands = []
    for num in sorted(lines):
        depth: dict[str, int] = {}

        def level(bid: str) -> int:
            if bid not in depth:
                depth[bid] = 1 + max((level(s) for s in inside[bid]), default=-1)
            return depth[bid]

        cols: dict[int, list[dict]] = {}
        for b in lines[num]:
            cols.setdefault(level(b["id"]), []).append(b)
        bands.append((cols, (max(cols) + 1) * COL_STEP, max(len(c) for c in cols.values()) * ROW_STEP + BAND_GAP))
    area = sum(w * h for _, w, h in bands) or 1
    limit = max(1800, int((area * 0.6) ** 0.5))
    x, y, widest = 40, 40, 0
    for cols, w, h in bands:
        if y > 40 and y + h > limit:
            x, y, widest = x + widest + 120, 40, 0
        for d, col in cols.items():
            for r, b in enumerate(col):
                b["position"] = {"x": x + d * COL_STEP, "y": y + r * ROW_STEP}
        widest = max(widest, w)
        y += h
    for b in blocks:
        b.pop("_line", None)


class _Paths:
    """Reachability of program lines as sets of disjoint path cases.

    Each conditional jump splits the current case set into two exclusive children (condition true / false), evaluated
    once at the jump. When both children of a split meet again (the branches of an IF … GOTO merge), they are replaced
    by their parent, so a line every path reaches is reached exactly when the split point was (always, at the top).
    Without this, a merge point carried an OR of every branch, which is always true, and every later line kept it.
    Conditions become blocks only when a line needs them."""
    ALWAYS = frozenset({0})
    NEVER: frozenset = frozenset()

    def __init__(self, graph):
        self.g = graph
        self.parent: dict[int, int] = {}
        self.kids: dict[int, tuple[int, int]] = {}
        self.cond: dict[int, tuple[Any, bool]] = {}  # node -> (condition ref, value on this side)
        self.comp: dict[int, frozenset] = {}  # node created for a set of several cases
        self.refs: dict[Any, Any] = {}
        self.n = 1

    def snapshot(self):
        return dict(self.refs)

    def restore(self, refs) -> None:
        self.refs = refs

    def _expand(self, node: int) -> frozenset:
        return self.comp.get(node, frozenset({node}))

    def norm(self, cases) -> frozenset:
        cases = set(cases)
        changed = True
        while changed:
            changed = False
            for p, (a, b) in self.kids.items():
                if a in cases and b in cases:
                    cases -= {a, b}
                    cases |= self._expand(p)
                    changed = True
        return frozenset(cases)

    def union(self, a: frozenset, b: frozenset) -> frozenset:
        return self.norm(a | b)

    def split(self, cases: frozenset, cond) -> tuple[frozenset, frozenset]:
        if not cases:
            return cases, cases
        if len(cases) == 1:
            p = next(iter(cases))
        else:
            p = self.n
            self.n += 1
            self.comp[p] = cases
        a, b = self.n, self.n + 1
        self.n += 2
        self.parent[a] = self.parent[b] = p
        self.kids[p] = (a, b)
        self.cond[a], self.cond[b] = (cond, True), (cond, False)
        return frozenset({a}), frozenset({b})

    def _node_ref(self, node: int):
        if node == 0:
            return True
        if node in self.comp:
            return self.ref(self.comp[node])
        key = ("n", node)
        if key not in self.refs:
            c, side = self.cond[node]
            self.refs[key] = self.g.both(self._node_ref(self.parent[node]), c if side else self.g.negate(c))
        return self.refs[key]

    def ref(self, cases: frozenset):
        """True (always), False (never) or the boolean block output of this case set."""
        if not cases:
            return False
        if cases == self.ALWAYS:
            return True
        if len(cases) == 1:
            return self._node_ref(next(iter(cases)))
        if cases not in self.refs:
            out = False
            for node in sorted(cases):
                out = self.g.either(out, self._node_ref(node))
            self.refs[cases] = out
        return self.refs[cases]


def code_to_sheet(code: bytes, listing: str, *, sheet_id: str, name: str, instance: int, slot: int, labels: dict[str, str] | None = None,
                  faceplates: bool = False, model: str | None = None) -> dict:
    """Return {'sheet', 'report'}. Never raises for unsupported code: those lines become Unmapped Code blocks.
    faceplates: physical inputs and outputs are drawn on Controller Inputs / Outputs faceplates (panels.collapse)."""
    if not _used(code):
        raise ValueError("The program is empty or its header is not valid")
    timers: dict = {}
    timer_error = None
    try:
        timers = _timer_table(code)
    except (Unsupported, IndexError, struct.error) as err:
        timer_error = str(err)
    graph = _Graph(labels or {})
    try:
        table = _locals(code)
    except (IndexError, struct.error):
        table = {}
    graph.initial = {v["name"]: v["initial"] for v in table.values() if v["initial"] is not None}
    spans, unmapped, mapped = _line_spans(code, listing), [], 0
    dirty: set[str] = set()
    index_at = {start: i for i, (_, start, _, _) in enumerate(spans)}
    paths = _Paths(graph)
    guard: frozenset = paths.ALWAYS
    pending: dict[int, list] = {}
    rems: list[dict] = []

    def jump(pos: int, here: int, cond: Any) -> None:
        to = index_at.get(pos)
        if to is None or to <= here:
            raise Unsupported("backward or unknown GOTO")
        pending.setdefault(to, []).append(cond)

    for idx, (num, start, stop, text) in enumerate(spans):
        for cond in pending.pop(idx, []):
            guard = paths.union(guard, cond)
        body = re.sub(r"^\d+\s*", "", text)
        if body.upper().startswith("REM"):
            rems.append({"seq": [num, graph.n], "text": body.rstrip()})  # comments come back in place in an editable copy
            continue
        if not guard:
            mapped += 1
            continue
        graph.line, graph.reads, graph.consts = num, {}, {}  # each line gets its own read and constant blocks
        saved = (dict(graph.env), len(graph.blocks), len(graph.wires), graph.n, dict(graph.reads), dict(graph.consts), guard, {k: list(v) for k, v in pending.items()}, paths.snapshot())
        try:
            for st in _statements(code, start, stop, timers, num, table):
                if not guard:
                    break
                if st.kind == "end":
                    guard = paths.NEVER
                elif st.kind == "goto":
                    jump(int(st.target), idx, guard)
                    guard = paths.NEVER
                elif st.kind == "if" and len(st.then) == 1 and st.then[0].kind in ("goto", "end") and not st.other:
                    c = graph.to_bool(graph.expr(st.expr))
                    taken, guard = paths.split(guard, c)
                    if st.then[0].kind == "goto":
                        jump(int(st.then[0].target), idx, taken)
                else:
                    graph.guarded(st, paths.ref(guard))
            if body.upper() != "END":
                mapped += 1
        except (Unsupported, IndexError, struct.error, KeyError) as err:
            graph.env, n_blocks, n_wires, graph.n, graph.reads, graph.consts, guard, pending, refs = saved
            paths.restore(refs)
            graph.blocks, graph.wires = graph.blocks[:n_blocks], graph.wires[:n_wires]
            graph.block("cb_unmapped", f"Line {num}", code=text)
            unmapped.append({"line": num, "text": text, "reason": str(err)})
            for m in re.finditer(r"\b([A-Z_][A-Z0-9_]*)\s*=(?!=)", body):
                if m.group(1) not in ("IF", "THEN", "ELSE"):
                    dirty.add(m.group(1))
    for point in sorted(graph.env):
        ref = graph.env[point]
        if graph.read_of.get(ref[0]) == point:
            continue
        graph.line = next(b["_line"] for b in graph.blocks if b["id"] == ref[0])
        if point.startswith("L:"):
            b = graph.block("cb_local_write", point[2:], name=point[2:])
        else:
            label = graph.labels.get(point)
            b = graph.block("t3_write", f"{point} {label}" if label else point, point=point, value_type="number")
        graph.wire(ref, b, "in")
    _layout(graph.blocks, graph.wires)
    notes = sorted(graph.notes)
    if timer_error:
        notes.append(f"Timer table could not be read: {timer_error}")
    if dirty:
        notes.append("Unmapped lines may also change: " + ", ".join(sorted(dirty)) + ". Values shown for those points can differ from the controller.")
    sheet = {"schema": "bas-wiresheet/1", "id": sheet_id, "name": name, "mode": "t3000",
             "description": f"Generated from T3000 program {slot} of controller {instance}. Documentation of the existing program; it cannot be loaded back.",
             "equipment": [], "t3000": {"device_instance": instance, "program_slot": slot, "work_vars": [], "imported": True,
                                                     "timers": dict(graph.timer_of), "logic": logic_digest(code), "rems": rems},
             "blocks": graph.blocks, "wires": graph.wires}
    if faceplates:
        sheet = panels.collapse(sheet, model)
    total = mapped + len(unmapped)
    return {"sheet": sheet, "report": {"lines": total, "mapped": mapped, "unmapped": unmapped, "complete": not unmapped and not timer_error,
                                       "blocks": len(sheet["blocks"]), "wires": len(sheet["wires"]), "notes": notes,
                                       "too_large": len(graph.blocks) > MAX_IMPORT_BLOCKS}}


def logic_digest(code: bytes) -> str:
    """Digest of the program logic. Local values and timer states change while a program runs, so they are excluded."""
    if not _used(code):
        return ""
    base = code[0] | code[1] << 8
    names = sorted((k, v["kind"], v["name"]) for k, v in _locals(code).items())
    pos = 2 + base + 3
    pos += 2 + (code[pos] | code[pos + 1] << 8)
    timers = code[pos] | code[pos + 1] << 8
    return hashlib.sha256(code[2:2 + base] + repr(names).encode() + struct.pack("<H", timers)).hexdigest()


def _walk_targets(stmts: list[Stmt], out: set[str]) -> None:
    for s in stmts:
        if s.kind == "assign":
            out.add(s.target)
        elif s.kind == "if":
            _walk_targets(s.then, out)
            _walk_targets(s.other, out)


def program_targets(code: bytes, listing: str) -> dict:
    """Points a program writes, and the lines whose effect could not be determined."""
    table = _locals(code)
    try:
        timers = _timer_table(code)
    except (Unsupported, IndexError, struct.error):
        timers = {}
    targets: set[str] = set()
    unknown: list[str] = []
    for num, start, stop, text in _line_spans(code, listing):
        body = re.sub(r"^\d+\s*", "", text).strip()
        if body.upper().startswith("REM") or body.upper() == "END":
            continue
        try:
            _walk_targets(_statements(code, start, stop, timers, num, table), targets)
        except (Unsupported, IndexError, struct.error, KeyError):
            unknown.append(body)
    return {"targets": sorted(t for t in targets if not t.startswith("L:")), "unknown": unknown}


def parse_prog_file(data: bytes) -> list[dict]:
    """Programs stored in a T3000 .prog file (format version 8)."""
    if len(data) != PROG_SIZE or data[:3] != PROG_MAGIC:
        raise ValueError("This is not a supported T3000 .prog file (format 8)")
    out = []
    for i in range(16):
        q = data[PROG_STATUS_OFFSET + 37 * i:PROG_STATUS_OFFSET + 37 * (i + 1)]
        code = data[PROG_CODE_OFFSET + 2000 * i:PROG_CODE_OFFSET + 2000 * (i + 1)]
        out.append({"program": i + 1, "description": q[:21].split(b"\0")[0].decode("latin-1"), "label": q[21:30].split(b"\0")[0].decode("latin-1"),
                    "bytes": q[30] | q[31] << 8, "on": q[32] != 0, "code": code, "empty": not any(code)})
    return out


EDIT_BLOCKERS = {"cb_unmapped": "a program line that has no block"}


def make_editable(sheet: dict, *, new_id: str, name: str, work_vars: list[str]) -> tuple[dict | None, list[dict]]:
    """Copy of a sheet generated from a controller program that can be edited and sent to the controller.
    Returns (sheet, blockers). Blocks that only document the program (unmapped code, local variables) cannot be
    compiled, so a sheet that has them is not converted: blockers name each of them."""
    t3 = sheet.get("t3000") or {}
    if sheet.get("mode") != "t3000" or not t3.get("imported"):
        return None, [{"message": "Only sheets generated from a controller program need this"}]
    blockers = [{"block": b["id"], "type": b["type"], "label": b.get("label") or b["id"], "message": f"{b.get('label') or b['id']}: {EDIT_BLOCKERS[b['type']]}"}
                for b in sheet.get("blocks", []) if b.get("type") in EDIT_BLOCKERS]
    if blockers:
        return None, blockers
    import copy
    out = copy.deepcopy(sheet)
    out["id"], out["name"] = new_id, name
    out["t3000"] = {k: v for k, v in t3.items() if k not in ("imported", "timers", "logic")}
    out["t3000"]["work_vars"] = list(work_vars)
    out["t3000"]["work_locals"] = True  # intermediate values in program-local variables; only timers take VARs
    out["description"] = (f"Editable copy of {sheet.get('name') or sheet.get('id')} (generated from T3000 program {t3.get('program_slot')} of controller "
                          f"{t3.get('device_instance')}). Send to Controller replaces that program with the code compiled from this sheet.")
    return out, []


def free_vars(listings: list[str], sheet: dict) -> list[str]:
    """VARs no program listing of the controller and no block of the sheet refers to, highest first (work variable pool)."""
    used = set()
    for text in listings:
        used |= {int(n) for n in re.findall(r"\bVAR(\d+)\b", text or "")}
    for b in sheet.get("blocks", []):
        m = re.match(r"^VAR(\d+)$", str((b.get("params") or {}).get("point", "")).upper())
        if m:
            used.add(int(m.group(1)))
    return [f"VAR{n}" for n in range(128, 0, -1) if n not in used]
