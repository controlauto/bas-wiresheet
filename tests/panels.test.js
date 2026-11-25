const test = require("node:test"); const assert = require("node:assert");
const RT = require("../custom_components/bas_wiresheet/runtime/runtime.js");
const B = (id, type, params = {}) => ({ id, type, position: { x: 0, y: 0 }, params });
const sheet = { schema: "bas-wiresheet/1", id: "fp", name: "fp", mode: "t3000", t3000: { device_instance: 1, program_slot: 16 },
  blocks: [B("ci", "t3_panel_in"), B("lim", "limit", { low: 0, high: 100 }), B("co", "t3_panel_out", { pins: { OUT8: "number" } })],
  wires: [{ id: "w1", from: { block: "ci", port: "IN5" }, to: { block: "lim", port: "in" } }, { id: "w2", from: { block: "lim", port: "out" }, to: { block: "co", port: "OUT8" } }] };

test("faceplate terminals expand to T3000 Read / Write blocks", () => {
  const r = RT.expandPanels(sheet);
  const ids = r.sheet.blocks.map((b) => `${b.id}:${b.type}:${b.params.point || ""}`).sort();
  assert.deepEqual(ids, ["ci__IN5:t3_read:IN5", "co__OUT8:t3_write:OUT8", "lim:limit:"]);
  assert.deepEqual(r.sheet.wires[0].from, { block: "ci__IN5", port: "value" });
  assert.deepEqual(r.sheet.wires[1].to, { block: "co__OUT8", port: "in" });
  assert.deepEqual(r.origin["co__OUT8"], ["co", "OUT8"]);
  assert.equal(r.issues.length, 0);
});

test("wrong terminal is reported on the faceplate and the wire is dropped", () => {
  const bad = JSON.parse(JSON.stringify(sheet));
  bad.wires.push({ id: "w3", from: { block: "co", port: "OUT2" }, to: { block: "lim", port: "in" } });
  const r = RT.expandPanels(bad);
  assert.equal(r.issues[0].code, "panel_pin"); assert.equal(r.issues[0].block, "co"); assert.equal(r.issues[0].wire, "w3");
  assert.ok(!r.sheet.wires.some((w) => w.id === "w3"));
});

test("simulation runs through the faceplate", () => {
  const rt = RT.createRuntime(sheet); rt.setPoint("IN5", 150);
  const s = rt.tick(Date.now());
  assert.equal(s.wires.w2.v, 100);
});
