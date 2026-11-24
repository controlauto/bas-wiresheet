// Live Monitoring of T3000 sheets with controller timer/local state: node --test tests/
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const Module = require("node:module");
const esbuild = require("../frontend/node_modules/esbuild");
const RT = require("../custom_components/bas_wiresheet/runtime/runtime.js");

const src = esbuild.buildSync({ entryPoints: [path.join(__dirname, "../frontend/src/model.js")], bundle: true, format: "cjs", platform: "node", write: false }).outputFiles[0].text;
const mod = new Module("model");
mod._compile(src, "model.js");
const { t3LiveSnapshot, controllerValues } = mod.exports;

const NOW = 1_800_000_000_000;
const pt = (category, index, value) => ({ category, index, value: String(value), entity_id: `number.x_${index}`, freshness: "Fresh", last_reported: NOW, last_changed: NOW });
const blk = (id, type, params = {}) => ({ id, type, position: { x: 0, y: 0 }, params });
const wire = (from, port, to, tp) => ({ id: `${from}-${to}`, from: { block: from, port }, to: { block: to, port: tp } });
const sheet = {
  schema: "bas-wiresheet/1", id: "s", name: "s", mode: "t3000", t3000: { device_instance: 1, program_slot: 16, work_vars: ["VAR125-128"] },
  blocks: [blk("r", "t3_read", { point: "VAR120", value_type: "number" }), blk("c", "compare", { op: ">" }), blk("k", "constant", { value: 50 }),
    blk("d", "on_delay", { delay_s: 30 }), blk("o", "t3_write", { point: "VAR121", value_type: "boolean" })],
  wires: [wire("r", "value", "c", "a"), wire("k", "value", "c", "b"), wire("c", "out", "d", "in"), wire("d", "out", "o", "in")],
};
const stateMap = { d: { kind: "on_delay", timer: "VAR125", delay_s: 30 } };
const points = [pt("VAR", 120, 60), pt("VAR", 121, 0), pt("VAR", 125, 1)];
const ctl = (on, elapsed, extra = {}) => ({ program: 16, on: true, used: 263, logic: "L1", timers: { VAR125: { on, elapsed_s: elapsed } }, locals: {}, read_at: NOW - 2000, ...extra });

test("on-delay output and remaining time come from the controller timer", () => {
  const a = t3LiveSnapshot(sheet, points, NOW, RT, ctl(true, 12.5), stateMap, "L1");
  assert.equal(a.controller, "ok");
  assert.equal(a.blocks.d.outputs.out.v, false);
  assert.equal(a.blocks.d.outputs.out.q, "good");
  assert.equal(a.blocks.d.state.remaining_s, 17.5);
  assert.equal(a.calculated.o, 0);
  const b = t3LiveSnapshot(sheet, points, NOW, RT, ctl(true, 31), stateMap, "L1");
  assert.equal(b.blocks.d.outputs.out.v, true);
  assert.equal(b.calculated.o, 1);
});

test("no controller value when the program differs, is stopped, stale or unreadable", () => {
  for (const [c, status] of [[ctl(true, 31, { logic: "OTHER" }), "different"], [ctl(true, 31, { on: false }), "stopped"],
    [ctl(true, 31, { read_at: NOW - 60000 }), "stale"], [{ error: "timeout" }, "error"], [ctl(true, 31, { used: 0 }), "empty"]]) {
    const s = t3LiveSnapshot(sheet, points, NOW, RT, c, stateMap, "L1");
    assert.equal(s.controller, status);
    assert.equal(s.blocks.d.outputs.out.q, "no_telemetry", status);
  }
  assert.equal(t3LiveSnapshot(sheet, points, NOW, RT, ctl(true, 31), stateMap, null).controller, "unverified");
  assert.equal(t3LiveSnapshot(sheet, points, NOW, RT).blocks.d.outputs.out.q, "no_telemetry");
});

test("imported TIME-OFF timer and local variables", () => {
  const imp = { ...sheet, t3000: { ...sheet.t3000, imported: true, logic: "L1", timers: { d: { key: "VAR125", off: true } } },
    blocks: [...sheet.blocks, blk("l", "cb_local_read", { name: "A3_SAT", initial: 0 })] };
  const v = controllerValues(imp, points, ctl(false, 40, { locals: { A3_SAT: 78.08 } }), null, null, NOW);
  assert.equal(v.status, "ok");
  assert.equal(v.values.d, true);
  assert.equal(v.values.l, 78.08);
  const w = controllerValues(imp, points, ctl(true, 40), null, null, NOW);
  assert.equal(w.values.d, false);
  assert.equal(w.state.d.remaining_s, null);
});

test("off-delay uses the controller latch variable and timer", () => {
  const sm = { d: { kind: "off_delay", timer: "VAR125", latch: "VAR126", delay_s: 10 } };
  const s2 = { ...sheet, blocks: sheet.blocks.map((b) => (b.id === "d" ? { ...b, type: "off_delay", params: { delay_s: 10 } } : b)) };
  const v = controllerValues(s2, [...points, pt("VAR", 126, 1)], ctl(false, 4), sm, "L1", NOW);
  assert.equal(v.values.d, true);
  assert.equal(v.state.d.remaining_s, 6);
});
