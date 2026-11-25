const test = require("node:test"); const assert = require("node:assert");
const RT = require("../custom_components/bas_wiresheet/runtime/runtime.js");
const CAT = require("../custom_components/bas_wiresheet/runtime/blocks.json");

test("every block runs with its inputs not connected yet (no crash, the rest of the sheet keeps going)", () => {
  for (const t of Object.keys(CAT.blocks)) {
    const p = {}; for (const d of CAT.blocks[t].params) if (d.default !== undefined && d.default !== null) p[d.id] = d.default;
    const sheet = { schema: "bas-wiresheet/1", id: "x", blocks: [{ id: "b", type: t, params: p, position: { x: 0, y: 0 } },
      { id: "k", type: "constant", params: { value_type: "number", value: 5 }, position: { x: 0, y: 0 } }], wires: [] };
    const rt = RT.createRuntime(sheet);
    const s = rt.tick(1000); rt.tick(2000);
    assert.equal(s.blocks.k.outputs.value.v, 5, t);
  }
});
