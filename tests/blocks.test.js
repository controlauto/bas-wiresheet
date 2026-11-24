// Math, control and T3000 blocks in the shared runtime (panel Simulation): node --test tests/
const test = require("node:test");
const assert = require("node:assert/strict");
const RT = require("../custom_components/bas_wiresheet/runtime/runtime.js");
const rd = (id, point, value_type = "number") => ({ id, type: "t3_read", position: { x: 0, y: 0 }, params: { point, value_type } });
const blk = (id, type, params = {}) => ({ id, type, position: { x: 0, y: 0 }, params });
const w = (from, port, to, tp) => ({ id: `${from}-${to}-${tp}`, from: { block: from, port }, to: { block: to, port: tp } });
const sheet = (blocks, wires) => ({ schema: "bas-wiresheet/1", id: "t", name: "t", blocks, wires });
function one(type, params, a, b) {
  const rt = RT.createRuntime(sheet([rd("a", "VAR1"), rd("b", "VAR2"), blk("m", type, params)],
    type === "absolute" || type === "limit" || type === "scale" ? [w("a", "value", "m", "in")] : [w("a", "value", "m", "a"), w("b", "value", "m", "b")]));
  if (a !== undefined) rt.setPoint("VAR1", a);
  if (b !== undefined) rt.setPoint("VAR2", b);
  return rt.tick(1000).blocks.m.outputs.out;
}
for (const [type, a, b, want] of [["add", 2, 3, 5], ["subtract", 2, 3, -1], ["multiply", 2, 3, 6], ["divide", 9, 2, 4.5], ["minimum", 2, 3, 2], ["maximum", 2, 3, 3]])
  test(`${type}`, () => assert.equal(one(type, {}, a, b).v, want));
test("divide by zero is invalid", () => assert.equal(one("divide", {}, 1, 0).q, "invalid"));
test("missing input propagates", () => assert.equal(one("add", {}, 1, undefined).q, "no_telemetry"));
test("absolute", () => assert.equal(one("absolute", {}, -4).v, 4));
test("limit", () => { assert.equal(one("limit", { low: 0, high: 10 }, 15).v, 10); assert.equal(one("limit", { low: 0, high: 10 }, -1).v, 0); });
test("scale clamps and reverses", () => {
  const p = { in_low: 0, in_high: 10, out_low: 0, out_high: 100, clamp: "yes" };
  assert.equal(one("scale", p, 5).v, 50); assert.equal(one("scale", p, 12).v, 100);
  assert.equal(one("scale", { in_low: 2, in_high: 10, out_low: 100, out_high: 0, clamp: "no" }, 12).v, -25);
});
test("hysteresis holds between limits", () => {
  const rt = RT.createRuntime(sheet([rd("a", "VAR1"), blk("h", "hysteresis", { on_above: 60, off_below: 50 })], [w("a", "value", "h", "in")]));
  const seq = [[55, false], [61, true], [55, true], [50, false], [55, false]];
  seq.forEach(([x, want], i) => { rt.setPoint("VAR1", x); assert.equal(rt.tick(1000 + i).blocks.h.outputs.out.v, want); });
});
test("select", () => {
  const rt = RT.createRuntime(sheet([rd("s", "VAR1", "boolean"), rd("a", "VAR2"), rd("b", "VAR3"), blk("x", "select")], [w("s", "value", "x", "s"), w("a", "value", "x", "a"), w("b", "value", "x", "b")]));
  rt.setPoint("VAR2", 7); rt.setPoint("VAR3", 3); rt.setPoint("VAR1", 1); assert.equal(rt.tick(1).blocks.x.outputs.out.v, 7);
  rt.setPoint("VAR1", 0); assert.equal(rt.tick(2).blocks.x.outputs.out.v, 3);
});
test("PI integrates, clamps and recovers without windup", () => {
  const rt = RT.createRuntime(sheet([rd("a", "VAR1"), rd("b", "VAR2"), blk("p", "pi", { kp: 2, ki: 0.1, out_min: 0, out_max: 100, action: "reverse" })], [w("a", "value", "p", "pv"), w("b", "value", "p", "sp")]));
  let t = 0, v; const run = (pv, sp, n) => { rt.setPoint("VAR1", pv); rt.setPoint("VAR2", sp); for (let i = 0; i < n; i++) { t += 1000; v = rt.tick(t).blocks.p.outputs.out.v; } return v; };
  assert.ok(Math.abs(run(50, 60, 11) - 30) < 0.01);
  assert.equal(run(50, 200, 100), 100);
  assert.ok(run(90, 60, 1) < 100);
});
test("T3000 write shows its input", () => {
  const rt = RT.createRuntime(sheet([rd("a", "VAR1"), blk("o", "t3_write", { point: "VAR2" })], [w("a", "value", "o", "in")]));
  rt.setPoint("VAR1", 12); assert.equal(rt.tick(1).displays.o.value.v, 12);
});

test("runtime timer state survives a restart and is frozen while stopped", () => {
  const s = { schema: "bas-wiresheet/1", id: "t", name: "t", blocks: [rd("a", "VAR1", "boolean"), blk("d", "on_delay", { delay_s: 60 }), blk("o", "t3_write", { point: "VAR2" })],
    wires: [w("a", "value", "d", "in"), w("d", "out", "o", "in")] };
  const r1 = RT.createRuntime(s); r1.setPoint("VAR1", 1);
  r1.tick(1000); r1.tick(41000);
  const saved = r1.exportState();
  assert.deepEqual(Object.keys(saved), ["d"]);
  const r2 = RT.createRuntime(s); r2.setPoint("VAR1", 1);
  assert.equal(r2.importState(saved, 41000, 341000), 1);           // 5 min down
  let t = r2.tick(341000);
  assert.equal(t.blocks.d.outputs.out.v, false);
  assert.ok(Math.abs(t.blocks.d.state.remaining_s - 20) < 0.01);    // 40 s were done before the stop
  t = r2.tick(361000);
  assert.equal(t.blocks.d.outputs.out.v, true);
  const r3 = RT.createRuntime({ ...s, blocks: s.blocks.map((b) => (b.id === "d" ? { ...b, type: "off_delay" } : b)) });
  assert.equal(r3.importState(saved, 41000, 341000), 0);           // different block type: not restored
});
