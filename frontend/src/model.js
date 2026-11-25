// Sheet <-> React Flow conversion and client-side helpers. The sheet JSON is the source of truth.
export const SCHEMA = "bas-wiresheet/1";
export const nodeTypeOf = (type) => (type === "t3_panel_in" || type === "t3_panel_out" ? "panel" : "bas");

export function resolvePortType(block, port, catalog) {
  const t = port.type;
  if (t && typeof t === "object" && t.param) {
    const v = (block.params || {})[t.param];
    if (v) return v;
    const pd = catalog.blocks[block.type].params.find((p) => p.id === t.param);
    return (pd && pd.default) || "number";
  }
  return t || "any";
}

export function resolvePortUnit(block, port, hass) {
  const u = port.unit;
  let unit = "";
  if (u && typeof u === "object" && u.param) unit = (block.params || {})[u.param] || "";
  if (!unit && block.type === "entity_input" && hass && block.params && block.params.entity_id) {
    const s = hass.states[block.params.entity_id];
    unit = (s && s.attributes.unit_of_measurement) || "";
  }
  return unit;
}

export function sheetToFlow(sheet) {
  const nodes = sheet.blocks.map((b) => ({ id: b.id, type: nodeTypeOf(b.type), position: { ...b.position }, data: { block: b } }));
  const edges = (sheet.wires || []).map((w) => ({
    id: w.id, source: w.from.block, sourceHandle: w.from.port, target: w.to.block, targetHandle: w.to.port, type: "bas",
  }));
  return { nodes, edges };
}

export function flowToSheet(meta, nodes, edges) {
  return {
    ...meta,
    schema: SCHEMA,
    blocks: nodes.map((n) => ({ ...n.data.block, id: n.id, position: { x: Math.round(n.position.x), y: Math.round(n.position.y) } })),
    wires: edges.map((e) => ({ id: e.id, from: { block: e.source, port: e.sourceHandle }, to: { block: e.target, port: e.targetHandle } })),
  };
}

export function defaultParams(catalog, type) {
  const p = {};
  for (const d of catalog.blocks[type].params) if (d.default !== undefined && d.default !== null) p[d.id] = Array.isArray(d.default) ? [...d.default] : d.default;
  return p;
}

let counter = 0;
export function newId(prefix, taken) {
  let id;
  do { id = `${prefix}_${Date.now().toString(36)}${(counter++).toString(36)}`; } while (taken.has(id));
  return id;
}

export function fmtValue(x) {
  if (!x) return "—";
  if (x.v === null || x.v === undefined) return qualityLabel(x.q);
  if (typeof x.v === "number") return `${Number.isInteger(x.v) ? x.v : x.v.toFixed(2)}${x.unit ? " " + x.unit : ""}`;
  if (typeof x.v === "boolean") return x.v ? "True" : "False";
  return String(x.v) + (x.unit && x.kind === "sample" ? " " + x.unit : "");
}

export function qualityLabel(q) {
  return { good: "Good", invalid: "Invalid Data", unavailable: "Unavailable", no_telemetry: "No Telemetry" }[q] || q || "—";
}

export function fmtTime(ms) {
  if (ms === null || ms === undefined) return "Not available";
  const d = new Date(ms);
  return d.toLocaleString("en-US", { hour12: false, year: "numeric", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function fmtAge(ms) {
  if (ms === null || ms === undefined || !isFinite(ms)) return "Not available";
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < 120) return `${s} s`;
  if (s < 7200) return `${Math.floor(s / 60)} min ${s % 60} s`;
  if (s < 172800) return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
  return `${Math.floor(s / 86400)} d ${Math.floor((s % 86400) / 3600)} h`;
}

export const FRESH_CLASS = { "Fresh": "ok", "Stale": "bad", "Unavailable": "bad", "Freshness Unknown": "unk" };

/* Live view of a T3000 sheet: only values the controller really reports. Logic between points runs on the controller. */
export const CONTROLLER_STATE_MAX_AGE_MS = 30000;

/* Live values of blocks whose state exists only on the controller (timers, latches, PI, hysteresis, locals).
   ctl: result of t3_program_state; stateMap: generator state_map (generated sheets); imported sheets carry
   t3000.timers. A block gets a value only when the controller program is running, fresh and the same logic. */
export function controllerValues(sheet, points, ctl, stateMap, expectLogic, now) {
  const byName = Object.fromEntries((points || []).map((p) => [`${p.category}${p.index}`, p]));
  const num = (name) => {
    const r = byName[String(name || "").toUpperCase()];
    if (!r || r.freshness === "Unavailable" || r.freshness === "Stale") return null;
    const n = r.value === "on" ? 1 : r.value === "off" ? 0 : Number(r.value);
    return Number.isFinite(n) ? n : null;
  };
  const t3 = sheet.t3000 || {};
  let status = "none";
  if (ctl && ctl.error) status = "error";
  else if (ctl) {
    const want = t3.logic || expectLogic || null;
    if (now - ctl.read_at > CONTROLLER_STATE_MAX_AGE_MS) status = "stale";
    else if (!ctl.used) status = "empty";
    else if (want && ctl.logic !== want) status = "different";
    else if (!want) status = "unverified";
    else if (!ctl.on) status = "stopped";
    else status = "ok";
  }
  const values = {}, state = {};
  if (status !== "ok") return { status, values, state };
  const timers = ctl.timers || {}, locals = ctl.locals || {};
  (sheet.blocks || []).forEach((b) => {
    const m = (stateMap || {})[b.id];
    const imp = (t3.timers || {})[b.id];
    if (b.type === "on_delay" && (m || imp)) {
      const t = timers[m ? m.timer : imp.key];
      if (!t) return;
      const delay = Number(m ? m.delay_s : (b.params || {}).delay_s);
      const active = imp && imp.off ? !t.on : t.on;
      values[b.id] = active && t.elapsed_s >= delay;
      state[b.id] = { remaining_s: active ? Math.max(0, delay - t.elapsed_s) : null, elapsed_s: t.elapsed_s, timer: active ? "running" : "idle" };
    } else if (b.type === "off_delay" && m && m.kind === "off_delay") {
      const t = timers[m.timer], latch = num(m.latch);
      if (!t || latch === null) return;
      values[b.id] = latch > 0;
      state[b.id] = { remaining_s: !t.on && latch > 0 ? Math.max(0, m.delay_s - t.elapsed_s) : null, elapsed_s: t.elapsed_s };
    } else if (m && m.kind === "var_bool") {
      const v = num(m.var);
      if (v !== null) values[b.id] = v > 0;
    } else if (b.type === "hysteresis" && m && m.kind === "hysteresis") {
      const v = num(m.var);
      if (v !== null) values[b.id] = v > 0;
    } else if (b.type === "pi" && m && m.kind === "pi") {
      const integ = num(m.integral), prop = num(m.proportional);
      const out = m.out ? num(m.out) : (integ === null || prop === null ? null : Math.max(m.out_min, Math.min(m.out_max, prop + integ)));
      if (out !== null) { values[b.id] = out; state[b.id] = { integral: integ }; }
    } else if (b.type === "cb_local_read") {
      const v = locals[(b.params || {}).name];
      if (v !== undefined && v !== null) values[b.id] = v;
    }
  });
  return { status, values, state };
}

export function t3LiveSnapshot(sheet, points, now, RT, ctl = null, stateMap = null, expectLogic = null) {
  if (RT && RT.expandPanels) sheet = RT.expandPanels(sheet).sheet; // controller faceplates -> T3000 Read / Write
  const byName = Object.fromEntries((points || []).map((p) => [`${p.category}${p.index}`, p]));
  const read = (b) => {
    const name = String((b.params || {}).point || "").toUpperCase();
    const r = byName[name];
    const base = { v: null, unit: (b.params || {}).unit || null, src: name, reported: null, changed: null, reason: null };
    if (!r) return { ...base, q: "no_telemetry", reason: `${name} is not reported by Home Assistant` };
    const out = { ...base, unit: r.units || base.unit, src: r.entity_id, reported: r.last_reported, changed: r.last_changed };
    if (r.freshness === "Unavailable") return { ...out, q: "unavailable", reason: r.freshness_reason };
    if (r.freshness === "Stale") return { ...out, q: "invalid", reason: r.freshness_reason };
    const n = r.value === "on" ? 1 : r.value === "off" ? 0 : Number(r.value);
    if (!Number.isFinite(n)) return { ...out, q: "invalid", reason: `Value is ${r.value}` };
    return { ...out, v: (b.params || {}).value_type === "boolean" ? n !== 0 : n, q: "good" };
  };
  // Logic between points is recalculated from the live values. Timers and local variables come from the running
  // controller program when it can be read and matches this sheet; otherwise they show No Telemetry.
  const cv = controllerValues(sheet, points, ctl, stateMap, expectLogic, now);
  const clone = JSON.parse(JSON.stringify(sheet));
  clone.blocks.forEach((b) => {
    if (!["on_delay", "off_delay", "cb_local_read", "pi", "hysteresis", "latch"].includes(b.type)) return;
    if (b.id in cv.values) { b.params = { key: b.id, boolean: typeof cv.values[b.id] === "boolean" }; b.type = "cb_controller_value"; }
    else b.type = "cb_controller_only";
  });
  let calc = null;
  if (RT) {
    try {
      const rt = RT.createRuntime(clone);
      sheet.blocks.forEach((b) => { if (b.type === "t3_read") { const x = read({ ...b, params: { ...b.params, value_type: "number" } }); if (x.q === "good") rt.setPoint(b.params.point, x.v); } });
      Object.entries(cv.values).forEach(([id, v]) => rt.setPoint(`CTL:${id}`, v === true ? 1 : v === false ? 0 : v));
      calc = rt.tick(now);
    } catch { calc = null; }
  }
  const blocks = {}, displays = {}, wires = {}, calculated = {};
  sheet.blocks.forEach((b) => {
    blocks[b.id] = calc && calc.blocks[b.id] ? { outputs: { ...calc.blocks[b.id].outputs }, state: { ...(cv.state[b.id] || {}) } } : { outputs: {}, state: {} };
    if (b.type === "t3_read") blocks[b.id].outputs.value = read(b);
    if (b.type === "t3_write") {
      displays[b.id] = { label: b.label || b.id, value: read(b) };
      const c = calc && calc.displays[b.id] && calc.displays[b.id].value;
      if (c && c.q === "good") calculated[b.id] = c.v === true ? 1 : c.v === false ? 0 : c.v;
    }
  });
  (sheet.wires || []).forEach((w) => {
    wires[w.id] = (blocks[w.from.block] && blocks[w.from.block].outputs[w.from.port])
      || { v: null, q: "no_telemetry", reason: "Computed on the controller", unit: null, src: null, reported: null, changed: null };
  });
  return { schema: SCHEMA, sheet_id: sheet.id, now, wires, blocks, displays, calculated, controller: cv.status };
}

/* Copy of the selected blocks and the wires between them, with new ids. */
export function cloneSelection(nodes, edges, takenNodes, takenEdges, offset = 40) {
  const map = {};
  const out = nodes.map((n) => {
    const id = newId(n.data.block.type.replace(/_/g, ""), takenNodes);
    takenNodes.add(id); map[n.id] = id;
    const block = JSON.parse(JSON.stringify(n.data.block));
    block.id = id;
    const position = { x: n.position.x + offset, y: n.position.y + offset };
    block.position = position;
    return { id, type: nodeTypeOf(block.type), position, data: { block }, selected: true };
  });
  const wires = edges.filter((e) => map[e.source] && map[e.target]).map((e) => {
    const id = newId("w", takenEdges);
    takenEdges.add(id);
    return { ...e, id, source: map[e.source], target: map[e.target], selected: false };
  });
  return { nodes: out, edges: wires };
}
