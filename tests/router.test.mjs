import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
// router.js is an ES module bundled by esbuild; the repository has no "type": "module", so load it from source
const src = readFileSync(new URL("../frontend/src/router.js", import.meta.url), "utf8");
const { routeAll, simplify, tidyLayout, elbowPoints } = await import("data:text/javascript," + encodeURIComponent(src));

const inside = (p, r, m = 2) => p[0] > r.x + m && p[0] < r.x + r.w - m && p[1] > r.y + m && p[1] < r.y + r.h - m;
function segCrosses(a, b, r) {
  // sample the orthogonal segment every 2 px
  const n = Math.max(1, Math.ceil(Math.hypot(b[0] - a[0], b[1] - a[1]) / 2));
  for (let k = 0; k <= n; k++) { const p = [a[0] + ((b[0] - a[0]) * k) / n, a[1] + ((b[1] - a[1]) * k) / n]; if (inside(p, r)) return true; }
  return false;
}

test("routes are orthogonal, start and end at the handles and avoid other blocks", () => {
  const rects = [{ id: "a", x: 0, y: 0, w: 160, h: 80 }, { id: "b", x: 300, y: 0, w: 160, h: 200 }, { id: "c", x: 600, y: 60, w: 160, h: 80 }];
  const wires = [{ id: "w1", sx: 160, sy: 40, tx: 600, ty: 100, source: "a", target: "c" }, { id: "w2", sx: 160, sy: 60, tx: 300, ty: 150, source: "a", target: "b" }];
  const r = routeAll(rects, wires);
  for (const w of wires) {
    const p = r[w.id];
    assert.deepEqual(p[0], [w.sx, w.sy]); assert.deepEqual(p[p.length - 1], [w.tx, w.ty]);
    for (let i = 1; i < p.length; i++) assert.ok(p[i][0] === p[i - 1][0] || p[i][1] === p[i - 1][1], "orthogonal");
    assert.ok(p[1][0] > p[0][0], "leaves the output to the right");
    assert.ok(p[p.length - 2][0] < w.tx, "enters the input from the left");
  }
  // w1 must go around block b, which sits between a and c
  const p = r.w1;
  for (let i = 1; i < p.length; i++) assert.ok(!segCrosses(p[i - 1], p[i], rects[1]), "w1 crosses block b");
});

test("parallel wires get separate lanes", () => {
  const rects = [{ id: "a", x: 0, y: 0, w: 160, h: 120 }, { id: "b", x: 500, y: 200, w: 160, h: 120 }];
  const wires = [0, 1, 2].map((k) => ({ id: `w${k}`, sx: 160, sy: 30 + k * 30, tx: 500, ty: 230 + k * 30, source: "a", target: "b" }));
  const r = routeAll(rects, wires);
  const verticalX = wires.map((w) => simplify(r[w.id]).find((q, i, a) => i > 0 && a[i - 1][0] === q[0] && a[i - 1][1] !== q[1])[0]);
  assert.equal(new Set(verticalX).size, 3, `vertical runs overlap: ${verticalX}`);
});

test("backward wire loops around and elbow fallback is orthogonal", () => {
  const rects = [{ id: "a", x: 400, y: 0, w: 160, h: 80 }, { id: "b", x: 0, y: 0, w: 160, h: 80 }];
  const r = routeAll(rects, [{ id: "w", sx: 560, sy: 40, tx: 0, ty: 40, source: "a", target: "b" }]);
  const p = r.w;
  for (const rc of rects) for (let i = 2; i < p.length - 1; i++) assert.ok(!segCrosses(p[i - 1], p[i], rc), "crosses a block");
  const e = elbowPoints(0, 0, -100, 50);
  for (let i = 1; i < e.length; i++) assert.ok(e[i][0] === e[i - 1][0] || e[i][1] === e[i - 1][1]);
});

test("tidy layout puts each block right of its inputs", () => {
  const nodes = ["a", "b", "c", "d"].map((id, i) => ({ id, position: { x: 0, y: i * 10 }, measured: { width: 160, height: 80 } }));
  const edges = [{ source: "a", target: "b" }, { source: "b", target: "c" }, { source: "a", target: "c" }, { source: "d", target: "c" }];
  const pos = tidyLayout(nodes, edges);
  for (const e of edges) assert.ok(pos[e.target].x > pos[e.source].x);
  assert.equal(pos.a.x, pos.d.x);
  assert.notEqual(pos.a.y, pos.d.y);
});
