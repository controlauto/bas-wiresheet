// HVAC application blocks: the behaviour an engineer expects, scenario by scenario (JavaScript runtime).
const test = require("node:test"); const assert = require("node:assert");
const RT = require("../custom_components/bas_wiresheet/runtime/runtime.js");
const CAT = require("../custom_components/bas_wiresheet/runtime/blocks.json");
RT.setMacroCatalog(CAT.blocks);

// One HVAC block whose inputs are controller points set by the test; returns step(points, seconds) -> outputs.
function rig(type, params = {}) {
  const d = CAT.blocks[type];
  const blocks = [{ id: "m", type, position: { x: 0, y: 0 }, params }], wires = [];
  d.inputs.forEach((ip, i) => {
    blocks.push({ id: "i" + i, type: "t3_read", position: { x: 0, y: 0 }, params: { point: "VAR" + (101 + i), value_type: ip.type === "boolean" ? "boolean" : "number", unit: "" } });
    wires.push({ id: "w" + i, from: { block: "i" + i, port: "value" }, to: { block: "m", port: ip.id } });
  });
  const rt = RT.createRuntime({ schema: "bas-wiresheet/1", id: "t", blocks, wires });
  const index = Object.fromEntries(d.inputs.map((ip, i) => [ip.id, "VAR" + (101 + i)]));
  let t = 0, last = null;
  return function step(points, seconds = 1) {
    for (const [k, v] of Object.entries(points)) rt.setPoint(index[k], v === true ? 1 : v === false ? 0 : v);
    for (let s = 0; s < seconds; s++) { t += 1000; last = rt.tick(t).blocks.m.outputs; }
    return Object.fromEntries(Object.entries(last).map(([k, x]) => [k, x.v]));
  };
}

test("Lead/Lag Pumps: lead runs, fails over on lost proof, holds the failure until disabled, alarms when both fail", () => {
  const s = rig("lead_lag_pumps", { proof_delay: 15 });
  let o = s({ enable: true, proof1: false, proof2: true, pump2_lead: false }, 10);
  assert.deepEqual([o.pump1, o.pump2, o.pump1_fail], [true, false, false]);
  o = s({}, 6);
  assert.deepEqual([o.pump1, o.pump2, o.pump1_fail, o.both_fail], [false, true, true, false]);
  o = s({ proof1: true }, 5);
  assert.equal(o.pump1_fail, true, "a failure is held until the pumps are disabled");
  o = s({ enable: false }, 1);
  assert.deepEqual([o.pump1, o.pump2, o.pump1_fail], [false, false, false]);
  o = s({ enable: true, pump2_lead: true }, 1);
  assert.deepEqual([o.pump1, o.pump2], [false, true], "pump 2 leads when selected");
  o = s({ proof1: false, proof2: false }, 20);
  assert.equal(o.both_fail, true);
});

test("Economizer: modulates on mixed air when outdoor air is useful, minimum position otherwise, closed with the fan off", () => {
  const s = rig("economizer", { mat_setpoint: 55, min_position: 20, high_limit: 65, low_limit: 40, kp: 2, ki: 0.05 });
  let o = s({ oat: 50, rat: 72, mat: 62, fan_on: true, mat_sp: 55 }, 30);
  assert.equal(o.economizing, true);
  assert.ok(o.damper > 20, `opens beyond minimum (${o.damper})`);
  o = s({ oat: 70 }, 1);
  assert.deepEqual([o.economizing, o.damper], [false, 20], "above the high limit: minimum position");
  o = s({ oat: 50, mat: 35 }, 1);
  assert.equal(o.damper, 20, "mixed air below the low limit: minimum position");
  o = s({ fan_on: false }, 1);
  assert.equal(o.damper, 0, "fan off: closed");
});

test("Heat Exchanger: reset setpoint, heat request by outdoor air, pump with post-run, valve only after proven pump", () => {
  const s = rig("hx_steam", { oat_low: 20, sp_at_low: 180, oat_high: 60, sp_at_high: 140, heat_on_below: 55, heat_off_above: 60, valve_delay: 5, pump_postrun: 90 });
  let o = s({ supply_temp: 120, oat: 30, enable: true, pump_proof: false }, 10);
  assert.equal(o.setpoint, 170);
  assert.deepEqual([o.heating, o.pump, o.valve], [true, true, 0], "no pump proof: valve stays closed");
  o = s({ pump_proof: true }, 4);
  assert.equal(o.valve, 0, "within the valve delay");
  o = s({}, 3);
  assert.ok(o.valve > 0, "proven pump: the valve opens below setpoint");
  o = s({ oat: 65 }, 1);
  assert.deepEqual([o.heating, o.pump], [false, true], "warm: heat off, pump in post-run");
  o = s({}, 91);
  assert.equal(o.pump, false);
});

test("Heating Valve opens fully on freeze; Cooling Valve is locked out by cold outdoor air", () => {
  let s = rig("heating_valve", {});
  let o = s({ temp: 80, setpoint: 55, enable: true, freeze: false }, 5);
  assert.equal(o.valve, 0);
  o = s({ freeze: true }, 1);
  assert.deepEqual([o.valve, o.heating], [100, true]);
  s = rig("cooling_valve", { lockout_oat: 50 });
  o = s({ temp: 70, setpoint: 55, enable: true, oat: 40 }, 5);
  assert.deepEqual([o.valve, o.cooling], [0, false]);
  o = s({ oat: 75 }, 5);
  assert.ok(o.valve > 0);
});

test("Freeze Protection trips after the delay and holds until reset; Fan with Proof stops and alarms without status", () => {
  let s = rig("freeze_protection", { limit: 38, delay_s: 5 });
  let o = s({ temp: 35, reset: false }, 4);
  assert.equal(o.trip, false);
  o = s({}, 2);
  assert.equal(o.trip, true);
  o = s({ temp: 50 }, 3);
  assert.equal(o.trip, true, "held after recovery");
  o = s({ reset: true }, 1);
  assert.equal(o.trip, false);
  s = rig("fan_proof", { proof_delay: 30 });
  o = s({ command: true, status: false }, 29);
  assert.deepEqual([o.run, o.fail], [true, false]);
  o = s({}, 2);
  assert.deepEqual([o.run, o.fail], [false, true]);
  o = s({ command: false }, 1);
  assert.equal(o.fail, false);
});

test("Outdoor Air Reset is a held straight line", () => {
  const s = rig("oa_reset", { oat_low: 20, sp_at_low: 180, oat_high: 60, sp_at_high: 140 });
  assert.equal(s({ oat: 0 }).setpoint, 180);
  assert.equal(s({ oat: 40 }).setpoint, 160);
  assert.equal(s({ oat: 80 }).setpoint, 140);
});
