import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import {
  ReactFlow, ReactFlowProvider, Background, Controls, MiniMap, Handle, Position, BaseEdge, EdgeLabelRenderer,
  getBezierPath, NodeChangeset, EdgeChangeset, useReactFlow,
} from "@xyflow/react";
import RT from "../../custom_components/bas_wiresheet/runtime/runtime.js";
import {
  sheetToFlow, flowToSheet, defaultParams, newId, resolvePortType, resolvePortUnit, fmtValue, fmtTime, fmtAge,
  qualityLabel, FRESH_CLASS, SCHEMA, t3LiveSnapshot, cloneSelection,
} from "./model.js";
import SCENARIOS from "./scenarios.js";
import Explorer from "./Explorer.jsx";
import { History, TrendChart, buildDocument, SheetRuntime, TemplateDialog } from "./Extras.jsx";

const Ctx = createContext(null);
const useCtx = () => useContext(Ctx);
const CONTROLLER_TEXT = {
  ok: "timers and locals from the running controller program",
  none: "reading the controller program…",
  error: "controller program could not be read: timers show No Telemetry",
  stale: "controller program read is old: timers show No Telemetry",
  empty: "the controller program slot is empty: timers show No Telemetry",
  different: "the program on the controller is not this sheet: timers show No Telemetry",
  unverified: "this sheet cannot be matched to the controller program: timers show No Telemetry",
  stopped: "the controller program is stopped: timers show No Telemetry",
};
const MODE = { EDIT: "Edit", SIM: "Simulation", LIVE: "Live Monitoring" };

/* ======================= block identity ======================= */
// One hue per block family (reference categorical palette, dark steps, validated on the panel surface).
export const CATEGORY_STYLE = {
  Inputs: { color: "#3987e5", tag: "IN" }, T3000: { color: "#199e70", tag: "T3" }, Logic: { color: "#9085e9", tag: "LOG" },
  Math: { color: "#c98500", tag: "MATH" }, Control: { color: "#d55181", tag: "CTL" }, Timers: { color: "#d95926", tag: "TMR" },
  "Data Quality": { color: "#0ca30c", tag: "DQ" }, Alarms: { color: "#e66767", tag: "ALM" }, Displays: { color: "#8b97a8", tag: "DSP" },
};
export const BLOCK_ICON = {
  entity_input: "⇢", constant: "#", compare: "≷", and: "&", or: "≥1", not: "¬", xor: "=1", latch: "SR", edge: "↑⎍",
  on_delay: "T→", off_delay: "→T", data_freshness: "◷", status_display: "▣", alarm: "⚠", range_check: "⟦ ⟧",
  t3_read: "⇥", t3_write: "⇤", cb_unmapped: "?", cb_local_read: "L⇥", cb_local_write: "L⇤",
  add: "+", subtract: "−", multiply: "×", divide: "÷", minimum: "min", maximum: "max", absolute: "|x|",
  limit: "⊓", scale: "⤢", hysteresis: "⌇", select: "⇋", pi: "PI", counter: "Σn", average: "x̄", rate: "Δ/t", ramp: "⟋",
};
function fmtPortValue(o) {
  if (!o) return null;
  if (o.q !== "good") return { text: o.q === "no_telemetry" ? "—" : o.q === "unavailable" ? "n/a" : "?", cls: "bad" };
  const v = o.v;
  if (v === true) return { text: "ON", cls: "on" };
  if (v === false) return { text: "OFF", cls: "off" };
  if (typeof v === "number") return { text: `${Math.abs(v) >= 1000 ? v.toFixed(0) : Number(v.toFixed(2))}${o.unit ? ` ${o.unit}` : ""}`, cls: "num" };
  return { text: String(v).slice(0, 14), cls: "txt" };
}

/* ======================= block node ======================= */
function BlockNode({ id, data, selected }) {
  const c = useCtx();
  const b = data.block;
  const def = c.catalog.blocks[b.type];
  if (!def) return <div className="blk blk-unknown">Unknown block {b.type}</div>;
  const issues = c.issuesByBlock[id] || [];
  const err = issues.some((i) => i.severity === "error");
  const warn = !err && issues.length > 0;
  const out = c.snapshot && c.snapshot.blocks[id];
  const eq = b.type === "entity_input" && b.params && b.params.equipment && c.equipment[b.params.equipment];
  const disp = c.snapshot && c.snapshot.displays[id];
  const fr = out && out.outputs.status;
  return (
    <div className={`blk cat-${(def.category || "").replace(/\s+/g, "-").toLowerCase()} ${selected ? "sel" : ""} ${err ? "err" : warn ? "warn" : ""} ${disp && disp.value && disp.value.alarm ? "alarm-on" : ""}`}
      style={{ "--cat": (CATEGORY_STYLE[def.category] || {}).color || "#8b97a8" }} data-testid={`block-${id}`}>
      <div className="blk-head">
        <span className="blk-icon" aria-hidden="true">{BLOCK_ICON[b.type] || "■"}</span>
        <span className="blk-type">{def.label}</span>
        <span className="blk-cat">{(CATEGORY_STYLE[def.category] || {}).tag || def.category}</span>
        {eq && <span className="badge comm" title={`${b.params.equipment}: ${eq.status} since ${eq.since}`}>Commissioning Pending</span>}
      </div>
      <div className="blk-title">{b.label || (b.params && b.params.label) || id}</div>
      {b.type === "entity_input" && <div className="blk-sub mono">{(b.params && b.params.entity_id) || "No entity selected"}</div>}
      {b.type === "data_freshness" && <div className="blk-sub">Stale after {b.params && b.params.stale_after_s} s</div>}
      {(b.type === "on_delay" || b.type === "off_delay") && (
        <div className="blk-sub">Delay {b.params && b.params.delay_s} s{out && out.state.remaining_s != null ? ` · ${Math.ceil(out.state.remaining_s)} s left` : ""}</div>
      )}
      {b.type === "constant" && <div className="blk-sub mono">{String(b.params && b.params.value)} {b.params && b.params.unit}</div>}
      {b.type === "compare" && <div className="blk-sub mono">A {b.params && b.params.op} B</div>}
      {(b.type === "t3_read" || b.type === "t3_write") && <div className="blk-sub mono">{(b.params && b.params.point) || "No point selected"}</div>}
      {b.type === "cb_unmapped" && <div className="blk-sub mono" style={{ whiteSpace: "pre-wrap", maxWidth: 260 }}>{b.params.code}</div>}
      {(b.type === "cb_local_read" || b.type === "cb_local_write") && <div className="blk-sub mono">{b.params.name}</div>}
      {b.type === "limit" && <div className="blk-sub mono">{b.params.low} to {b.params.high}</div>}
      {b.type === "scale" && <div className="blk-sub mono">{b.params.in_low}..{b.params.in_high} to {b.params.out_low}..{b.params.out_high}</div>}
      {b.type === "hysteresis" && <div className="blk-sub mono">On ≥ {b.params.on_above} · Off ≤ {b.params.off_below}</div>}
      {b.type === "range_check" && <div className="blk-sub mono">{b.params.low} … {b.params.high}</div>}
      {b.type === "edge" && <div className="blk-sub mono">{b.params.edge} · pulse {b.params.pulse_s} s{out && out.state.remaining_s != null ? ` · ${Math.ceil(out.state.remaining_s)} s left` : ""}</div>}
      {(b.type === "average" || b.type === "rate") && <div className="blk-sub mono">window {b.params.window_s} s</div>}
      {b.type === "ramp" && <div className="blk-sub mono">max {b.params.rate_per_min} /min</div>}
      {b.type === "alarm" && <div className="blk-sub"><span className={`sev sev-${b.params.severity}`}>{b.params.severity}</span> {b.params.delay_s ? `after ${b.params.delay_s} s` : "immediate"}{out && out.state.remaining_s != null ? ` · ${Math.ceil(out.state.remaining_s)} s left` : ""}</div>}
      {b.type === "pi" && <div className="blk-sub mono">Kp {b.params.kp} · Ki {b.params.ki} · {b.params.out_min}..{b.params.out_max}</div>}
      {b.type === "pi" && out && out.state.integral != null && <div className="blk-sub mono" data-testid={`ctl-${id}`}>Integral {Number(out.state.integral).toFixed(2)} (controller)</div>}
      {(b.type === "on_delay" || b.type === "off_delay") && out && out.state.elapsed_s != null && <div className="blk-sub mono" data-testid={`ctl-${id}`}>Controller timer {out.state.elapsed_s.toFixed(1)} s</div>}
      {b.type === "t3_read" && out && out.outputs.value && <DisplayValue value={out.outputs.value} />}
      {fr && <div className={`chip ${FRESH_CLASS[fr.v] || "unk"}`}>{fr.v}</div>}
      {disp && <DisplayValue value={disp.value} />}
      {b.type === "t3_write" && c.snapshot && c.snapshot.calculated && c.snapshot.calculated[id] !== undefined && (
        <div className={`blk-sub mono ${disp && disp.value.q === "good" && Math.abs(Number(disp.value.v) - c.snapshot.calculated[id]) > 0.05 ? "errtxt" : ""}`} data-testid={`calc-${id}`}>Logic result {Number(c.snapshot.calculated[id]).toFixed(2)}</div>
      )}
      <div className="ports">
        <div className="ports-in">
          {def.inputs.map((p) => {
            const t = resolvePortType(b, p, c.catalog);
            const bad = issues.some((i) => i.port === p.id);
            return (
              <div key={p.id} className={`port in ${bad ? "bad" : ""}`}>
                <Handle type="target" position={Position.Left} id={p.id} style={{ background: c.catalog.port_types[t]?.color }} />
                <span>{p.label}{p.required ? "" : " (opt)"}</span><em>{c.catalog.port_types[t]?.label}</em>
              </div>
            );
          })}
        </div>
        <div className="ports-out">
          {def.outputs.map((p) => {
            const t = resolvePortType(b, p, c.catalog);
            const u = resolvePortUnit(b, p, c.hass);
            return (
              <div key={p.id} className="port out">
                {(() => { const pv = out && c.mode !== "Edit" ? fmtPortValue(out.outputs[p.id]) : null; return pv ? <b className={`pv ${pv.cls}`} data-testid={`pv-${id}-${p.id}`}>{pv.text}</b> : null; })()}
                <em>{c.catalog.port_types[t]?.label}{u ? ` · ${u}` : ""}</em><span>{p.label}</span>
                <Handle type="source" position={Position.Right} id={p.id} style={{ background: c.catalog.port_types[t]?.color }} />
              </div>
            );
          })}
        </div>
      </div>
      {issues.length > 0 && <div className="blk-issue">{issues[0].message}</div>}
    </div>
  );
}

function DisplayValue({ value }) {
  if (!value) return null;
  const isFresh = typeof value.v === "string" && FRESH_CLASS[value.v];
  if (isFresh) return <div className={`chip big ${FRESH_CLASS[value.v]}`}>{value.v}</div>;
  if (value.q !== "good") return <div className="chip big dq" title={value.reason || ""}>{qualityLabel(value.q)}</div>;
  return <div className="chip big val mono">{fmtValue(value)}</div>;
}

/* ======================= wire ======================= */
function BasEdge(props) {
  const c = useCtx();
  const { id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, selected } = props;
  const [path, lx, ly] = getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition });
  const issues = c.issuesByWire[id] || [];
  const v = c.snapshot && c.snapshot.wires[id];
  const color = issues.some((i) => i.severity === "error") ? "var(--bad)" : c.wireColor(id);
  return (
    <>
      <BaseEdge id={id} path={path} interactionWidth={18} style={{ stroke: color, strokeWidth: selected ? 3.5 : 2, strokeDasharray: issues.length ? "6 4" : undefined }} />
      {(v || issues.length > 0) && (
        <EdgeLabelRenderer>
          <div className={`edge-label ${issues.length ? "bad" : v && v.q !== "good" ? "dq" : ""}`} style={{ transform: `translate(-50%,-50%) translate(${lx}px,${ly}px)` }}>
            {issues.length ? "Invalid connection" : fmtValue(v)}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

const nodeTypes = { bas: BlockNode };
const edgeTypes = { bas: BasEdge };

/* ======================= inspector ======================= */
function ParamField({ def, value, onChange, readOnly, hass, error, options }) {
  const id = `p-${def.id}`;
  let input;
  if (def.kind === "enum") {
    input = (
      <select id={id} value={value ?? ""} disabled={readOnly} onChange={(e) => onChange(e.target.value)}>
        {def.options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    );
  } else if (def.kind === "number") {
    input = <input id={id} type="number" value={value ?? ""} disabled={readOnly} onChange={(e) => onChange(e.target.value === "" ? null : Number.isNaN(e.target.valueAsNumber) ? e.target.value : e.target.valueAsNumber)} />;
  } else if (def.kind === "entity") {
    input = (
      <>
        <input id={id} list="bas-entities" value={value ?? ""} disabled={readOnly} onChange={(e) => onChange(e.target.value.trim())} placeholder="sensor.example" />
        <datalist id="bas-entities">{Object.keys(hass.states).sort().map((k) => <option key={k} value={k}>{hass.states[k].attributes.friendly_name}</option>)}</datalist>
      </>
    );
  } else if (options) {
    input = (
      <>
        <input id={id} list="bas-points" value={value ?? ""} disabled={readOnly} onChange={(e) => onChange(e.target.value.trim().toUpperCase())} placeholder="VAR120" data-testid="point-input" />
        <datalist id="bas-points">{options.map((o) => <option key={o.value} value={o.value}>{o.label}</option>)}</datalist>
      </>
    );
  } else if (def.kind === "list") {
    input = <input id={id} value={(value || []).join(", ")} disabled={readOnly} onChange={(e) => onChange(e.target.value.split(",").map((s) => s.trim()).filter(Boolean))} placeholder="comma separated" />;
  } else if (def.kind === "text") {
    input = <textarea id={id} rows={4} value={value ?? ""} disabled={readOnly} onChange={(e) => onChange(e.target.value)} />;
  } else if (def.kind === "value") {
    input = null;
  } else {
    input = <input id={id} value={value ?? ""} disabled={readOnly} onChange={(e) => onChange(e.target.value)} />;
  }
  if (!input) return null;
  return (
    <label className={`field ${error ? "bad" : ""}`} htmlFor={id}>
      <span>{def.label}{def.required ? " *" : ""}{def.unit ? ` (${def.unit})` : ""}</span>
      {input}
      {def.help && <small>{def.help}</small>}
      {error && <small className="errtxt">{error}</small>}
    </label>
  );
}

function pointOptions(points, type) {
  return (points || [])
    .filter((p) => ["IN", "OUT", "VAR"].includes(p.category) && (type === "t3_read" || (p.category !== "IN" && p.protection === "sandbox")))
    .map((p) => ({ value: `${p.category}${p.index}`, label: `${p.label} · ${p.value}${p.units ? ` ${p.units}` : ""}${p.protection === "locked" ? " · Locked" : ""}` }));
}

function BlockInspector({ node, onParam, onLabel, onDelete }) {
  const c = useCtx();
  const b = node.data.block;
  const def = c.catalog.blocks[b.type];
  const issues = c.issuesByBlock[node.id] || [];
  const ro = c.readOnly;
  const errFor = (pid) => (issues.find((i) => i.param === pid) || {}).message;
  const ent = b.type === "entity_input" && b.params.entity_id ? c.hass.states[b.params.entity_id] : null;
  return (
    <div className="insp">
      <h3>{def.label}</h3>
      <div className="muted small">{def.description}</div>
      <div className="kv"><span>Block ID</span><b className="mono">{node.id}</b></div>
      <label className="field"><span>Label</span><input value={b.label || ""} disabled={ro} onChange={(e) => onLabel(e.target.value)} /></label>
      {def.params.map((p) => (
        <ParamField key={p.id} def={p} value={b.params[p.id]} readOnly={ro} hass={c.hass} error={errFor(p.id)} onChange={(v) => onParam(p.id, v)}
          options={p.id === "point" && (b.type === "t3_read" || b.type === "t3_write") ? pointOptions(c.t3Points, b.type) : undefined} />
      ))}
      {b.type === "constant" && (
        <label className={`field ${errFor("value") ? "bad" : ""}`}>
          <span>Value *</span>
          {b.params.value_type === "boolean"
            ? <select value={String(b.params.value === true)} disabled={ro} onChange={(e) => onParam("value", e.target.value === "true")}><option value="true">True</option><option value="false">False</option></select>
            : <input type="number" value={b.params.value ?? ""} disabled={ro} onChange={(e) => onParam("value", e.target.value === "" ? null : e.target.valueAsNumber)} />}
          {errFor("value") && <small className="errtxt">{errFor("value")}</small>}
        </label>
      )}
      {b.type === "entity_input" && (
        <div className="box">
          <div className="box-h">Home Assistant Entity</div>
          {ent ? (
            <>
              <div className="kv"><span>Name</span><b>{ent.attributes.friendly_name}</b></div>
              <div className="kv"><span>State</span><b className="mono">{ent.state} {ent.attributes.unit_of_measurement || ""}</b></div>
              <div className="kv"><span>Last Report</span><b>{fmtTime(Date.parse(ent.last_reported || ""))}</b></div>
              <div className="kv"><span>Last Value Change</span><b>{fmtTime(Date.parse(ent.last_changed))}</b></div>
            </>
          ) : <div className="muted">Entity not found in this Home Assistant instance.</div>}
          {b.params.equipment && c.equipment[b.params.equipment] && (
            <div className="kv"><span>Equipment</span><b><span className="badge comm">Commissioning Pending</span> {b.params.equipment}</b></div>
          )}
        </div>
      )}
      {issues.length > 0 && <IssueList issues={issues} />}
      {!ro && <button className="btn danger" onClick={onDelete}>Delete Block</button>}
    </div>
  );
}

function WireInspector({ edge, onDelete }) {
  const c = useCtx();
  const v = c.snapshot && c.snapshot.wires[edge.id];
  const issues = c.issuesByWire[edge.id] || [];
  const src = c.nodeById[edge.source], dst = c.nodeById[edge.target];
  const sdef = src && c.catalog.blocks[src.data.block.type], ddef = dst && c.catalog.blocks[dst.data.block.type];
  const sp = sdef && sdef.outputs.find((p) => p.id === edge.sourceHandle);
  const type = sp ? resolvePortType(src.data.block, sp, c.catalog) : "any";
  const unit = sp ? resolvePortUnit(src.data.block, sp, c.hass) : "";
  const now = c.now;
  const noTelemetry = c.mode !== MODE.EDIT && (!v || v.q === "no_telemetry" || (c.mode === MODE.LIVE && !c.liveOk));
  return (
    <div className="insp" data-testid="wire-inspector">
      <h3>Wire</h3>
      <div className={`modebar ${c.mode === MODE.SIM ? "sim" : c.mode === MODE.LIVE ? "live" : "edit"}`}>{c.mode === MODE.EDIT ? "Edit Mode · no values" : c.mode}</div>
      <div className="kv"><span>Wire ID</span><b className="mono">{edge.id}</b></div>
      <div className="kv"><span>From</span><b>{src ? `${src.data.block.label || src.id} · ${sp ? sp.label : edge.sourceHandle}` : "Missing block"}</b></div>
      <div className="kv"><span>To</span><b>{dst ? `${dst.data.block.label || dst.id} · ${edge.targetHandle}` : "Missing block"}</b></div>
      <div className="kv"><span>Type</span><b>{c.catalog.port_types[type]?.label || type}</b></div>
      {c.mode !== MODE.EDIT && (
        <div className="box">
          {noTelemetry && <div className="notel">No Telemetry{c.mode === MODE.LIVE && !c.liveOk ? " · no live result from the runtime" : ""}</div>}
          <div className="kv"><span>Value</span><b className="mono big">{v ? fmtValue({ ...v, unit: null }) : "—"}</b></div>
          <div className="kv"><span>Unit</span><b>{(v && v.unit) || unit || "None"}</b></div>
          <div className="kv"><span>Quality</span><b className={`q-${v ? v.q : "none"}`}>{v ? qualityLabel(v.q) : "—"}</b></div>
          {v && v.reason && <div className="kv"><span>Detail</span><b>{v.reason}</b></div>}
          <div className="kv"><span>Source Entity</span><b className="mono">{(v && v.src) || "Derived (no direct source)"}</b></div>
          <div className="kv"><span>Last Report</span><b>{fmtTime(v && v.reported)}</b></div>
          <div className="kv"><span>Data Age</span><b>{v && v.reported != null ? fmtAge(now - v.reported) : "Not available"}</b></div>
          <div className="kv"><span>Last Value Change</span><b>{fmtTime(v && v.changed)}</b></div>
          <div className="muted small">Last Report is the time Home Assistant last wrote the state (last_reported). Data Age is measured from it, never from the value-change time.</div>
        </div>
      )}
      {issues.length > 0 && <IssueList issues={issues} />}
      {!c.readOnly && <button className="btn danger" onClick={onDelete}>Delete Wire</button>}
    </div>
  );
}

function IssueList({ issues, onPick }) {
  return (
    <ul className="issues">
      {issues.map((i, k) => (
        <li key={k} className={i.severity} onClick={onPick ? () => onPick(i) : undefined}>
          <b>{i.severity === "error" ? "Error" : "Warning"}</b> {i.message}
          {(i.block || i.wire) && <span className="mono muted"> [{i.wire ? `wire ${i.wire}` : `block ${i.block}`}{i.param ? ` · ${i.param}` : ""}{i.port ? ` · ${i.port}` : ""}]</span>}
        </li>
      ))}
    </ul>
  );
}

/* ======================= simulation ======================= */
function useSimulation(sheet, active) {
  const [tick, setTick] = useState(0);
  const sim = useRef(null);
  const sheetKey = useMemo(() => JSON.stringify({ b: sheet.blocks.map((b) => [b.id, b.type, b.params]), w: sheet.wires }), [sheet]);
  const build = useCallback((scenario) => {
    let rt;
    try {
      let sh = sheet;
      if (scenario && scenario.param_overrides) {
        sh = JSON.parse(JSON.stringify(sheet));
        sh.blocks.forEach((b) => { const o = scenario.param_overrides[b.id]; if (o) Object.assign(b.params, o); });
      }
      rt = RT.createRuntime(sh);
    } catch (e) { sim.current = { error: e.message }; setTick((x) => x + 1); return; }
    const src = RT.createSourceEmulator(rt);
    const start = scenario ? Date.parse(scenario.start) : Date.now();
    if (scenario) Object.entries(scenario.entities || {}).forEach(([id, o]) => src.configure(id, o));
    else rt.entityIds().forEach((id) => src.configure(id, { write_mode: "every_sample" }));
    sheet.blocks.forEach((b) => { if (b.type === "t3_read" && b.params.point) rt.setPoint(b.params.point, 0); });
    sim.current = { rt, src, start, clock: start, scenario, ei: 0, events: scenario ? [...scenario.events].sort((a, b) => a.t - b.t) : [], playing: false, speed: 1, snapshot: rt.tick(start), log: [] };
    setTick((x) => x + 1);
  }, [sheet]);
  useEffect(() => { if (active) build(sim.current && sim.current.scenario); }, [active, sheetKey]); // eslint-disable-line
  const advance = useCallback((sec) => {
    const s = sim.current; if (!s || !s.rt) return;
    for (let i = 0; i < sec; i++) {
      s.clock += 1000;
      while (s.ei < s.events.length && s.start + s.events[s.ei].t * 1000 <= s.clock) {
        const ev = s.events[s.ei++];
        s.src.apply(ev, s.start + ev.t * 1000);
      }
      s.snapshot = s.rt.tick(s.clock);
    }
    setTick((x) => x + 1);
  }, []);
  useEffect(() => {
    if (!active) return undefined;
    const h = setInterval(() => { const s = sim.current; if (s && s.playing) advance(s.speed); }, 1000);
    return () => clearInterval(h);
  }, [active, advance]);
  const api = {
    state: sim.current, tick,
    load: (name) => build(name ? SCENARIOS[name] : null),
    reset: () => build(sim.current && sim.current.scenario),
    play: (on) => { if (sim.current) { sim.current.playing = on; setTick((x) => x + 1); } },
    speed: (n) => { if (sim.current) { sim.current.speed = n; setTick((x) => x + 1); } },
    advance,
    point: (name, value) => {
      const s = sim.current; if (!s || !s.rt) return;
      s.rt.setPoint(name, value);
      s.snapshot = s.rt.tick(s.clock);
      setTick((x) => x + 1);
    },
    write: (entity, ev) => {
      const s = sim.current; if (!s || !s.src) return;
      if (ev.write_mode || ev.ts_missing !== undefined) s.src.configure(entity, ev);
      else s.src.apply({ entity_id: entity, ...ev }, s.clock);
      s.snapshot = s.rt.tick(s.clock);
      setTick((x) => x + 1);
    },
  };
  return api;
}

function SimPanel({ sim, sheet }) {
  const s = sim.state;
  const [manual, setManual] = useState({});
  if (!s) return null;
  if (s.error) return <div className="simpanel"><div className="errtxt">Simulation cannot start: {s.error}. Validate the sheet.</div></div>;
  const inputs = sheet.blocks.filter((b) => b.type === "entity_input" && b.params.entity_id);
  const ents = s.src.entities();
  return (
    <div className="simpanel" data-testid="sim-panel">
      <div className="simbar">
        <span className="modebar sim">Simulation</span>
        <label>Scenario
          <select value={s.scenario ? s.scenario.name : ""} onChange={(e) => sim.load(e.target.value)} data-testid="scenario-select">
            <option value="">Manual Inputs</option>
            {Object.keys(SCENARIOS).map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
        </label>
        <button className="btn" onClick={() => sim.play(!s.playing)}>{s.playing ? "Pause" : "Run"}</button>
        <button className="btn" onClick={() => sim.advance(1)}>Step 1 s</button>
        <button className="btn" onClick={() => sim.advance(15)}>+15 s</button>
        <button className="btn" onClick={() => sim.advance(60)} data-testid="adv60">+60 s</button>
        <label>Speed <select value={s.speed} onChange={(e) => sim.speed(Number(e.target.value))}>{[1, 5, 15, 60].map((n) => <option key={n} value={n}>{n}x</option>)}</select></label>
        <button className="btn" onClick={sim.reset}>Reset</button>
        <span className="mono simclock">Sim Clock {fmtTime(s.clock)} · T+{Math.round((s.clock - s.start) / 1000)} s</span>
      </div>
      {s.scenario && <div className="muted small">{s.scenario.description}</div>}
      {sheet.mode === "t3000" && <table className="simtab" data-testid="sim-t3-points"><thead><tr><th>Controller Point</th><th>Simulated Value</th></tr></thead>
        <tbody>{[...new Set(sheet.blocks.filter((b) => b.type === "t3_read" && b.params.point).map((b) => b.params.point.toUpperCase()))].map((pt) => (
          <tr key={pt}><td className="mono">{pt}</td><td><input type="number" step="any" className="mono" defaultValue={0} onChange={(ev) => { if (ev.target.value !== "") sim.point(pt, Number(ev.target.value)); }} data-testid={`sim-point-${pt}`} /></td></tr>
        ))}</tbody></table>}
      <table className="simtab">
        <thead><tr><th>Entity</th><th>Write Mode</th><th>Omit last_reported</th><th>Current</th><th>Manual State</th><th></th></tr></thead>
        <tbody>
          {[...new Set(inputs.map((b) => b.params.entity_id))].map((id) => {
            const e = ents[id] || {};
            return (
              <tr key={id}>
                <td className="mono">{id}</td>
                <td><select value={e.write_mode || "on_change"} onChange={(ev) => sim.write(id, { write_mode: ev.target.value })}><option value="every_sample">Every Sample</option><option value="on_change">Changes Only</option></select></td>
                <td><input type="checkbox" checked={!!e.ts_missing} onChange={(ev) => sim.write(id, { ts_missing: ev.target.checked })} /></td>
                <td className="mono">{e.cur ? `${e.cur.state} @ T+${Math.round((e.cur.last_reported - s.start) / 1000)} s` : "No telemetry"}</td>
                <td><input className="mono" value={manual[id] ?? ""} placeholder="e.g. 72.9" onChange={(ev) => setManual({ ...manual, [id]: ev.target.value })} data-testid={`manual-${id}`} /></td>
                <td>
                  <button className="btn sm" onClick={() => sim.write(id, { sample: manual[id] ?? (e.cur ? e.cur.state : "") })} data-testid={`report-${id}`}>Report Sample</button>
                  <button className="btn sm" onClick={() => sim.write(id, { unavailable: true })}>Set Unavailable</button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function PointsTable({ sheet, snapshot, equipment }) {
  const rows = sheet.blocks.filter((b) => b.type === "data_freshness").map((f) => {
    const w = sheet.wires.find((x) => x.to.block === f.id && x.to.port === "sample");
    const inp = w && sheet.blocks.find((b) => b.id === w.from.block);
    const fo = snapshot && snapshot.blocks[f.id] && snapshot.blocks[f.id].outputs.status;
    const io = snapshot && inp && snapshot.blocks[inp.id] && snapshot.blocks[inp.id].outputs.value;
    return { f, inp, fo, io };
  });
  if (!rows.length) return null;
  return (
    <table className="points" data-testid="points-table">
      <thead><tr><th>Point</th><th>Freshness</th><th>Data Quality</th><th>Value</th><th>Equipment Status</th><th>Detail</th></tr></thead>
      <tbody>
        {rows.map(({ f, inp, fo, io }) => {
          const eq = inp && inp.params.equipment && equipment[inp.params.equipment];
          return (
            <tr key={f.id}>
              <td>{inp ? inp.label || inp.params.entity_id : f.label}</td>
              <td><span className={`chip ${fo ? FRESH_CLASS[fo.v] : "unk"}`}>{fo ? fo.v : "—"}</span></td>
              <td>{io ? <span className={`chip ${io.q === "good" ? "ok" : "dq"}`}>{qualityLabel(io.q)}</span> : "—"}</td>
              <td className="mono">{io && io.q === "good" ? fmtValue(io) : "—"}</td>
              <td>{eq ? <span className="badge comm">Commissioning Pending</span> : <span className="muted">Not Specified</span>}</td>
              <td className="muted small">{fo && fo.reason}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/* ======================= dialogs ======================= */
function Modal({ title, children, onClose, actions }) {
  return (
    <div className="modal-bg" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="modal" role="dialog" aria-label={title}>
        <div className="modal-h"><b>{title}</b><button className="btn sm" onClick={onClose}>Close</button></div>
        <div className="modal-b">{children}</div>
        {actions && <div className="modal-f">{actions}</div>}
      </div>
    </div>
  );
}

/* ======================= main editor ======================= */
function Editor({ hass, boot, initialSheet }) {
  const { catalog, role, targets, production } = boot;
  const rf = useReactFlow();
  const [sheets, setSheets] = useState([]);
  const [sheetId, setSheetId] = useState(null);
  const [meta, setMeta] = useState(null);
  const [nodes, setNodes] = useState([]);
  const [edges, setEdges] = useState([]);
  const [info, setInfo] = useState(null);
  const [libFilter, setLibFilter] = useState("");
  const [issues, setIssues] = useState([]);
  const [selection, setSelection] = useState(null);
  const [mode, setMode] = useState(MODE.EDIT);
  const [toast, setToast] = useState(null);
  const [dialog, setDialog] = useState(null);
  const [live, setLive] = useState(null);
  const [now, setNow] = useState(Date.now());
  const [dirty, setDirty] = useState(false);
  const hist = useRef({ past: [], future: [] });
  const sheetLocked = !!(info && info.can_edit === false);
  const readOnly = role !== "editor" || mode !== MODE.EDIT || sheetLocked;
  const wrapper = useRef(null);

  const sheet = useMemo(() => (meta ? flowToSheet(meta, nodes, edges) : null), [meta, nodes, edges]);
  const notify = (msg, kind = "ok") => { setToast({ msg, kind }); setTimeout(() => setToast(null), 5000); };
  const hassRef = useRef(hass);
  hassRef.current = hass;
  const ws = useCallback((msg) => hassRef.current.callWS(msg), []);

  const refreshList = useCallback(async () => { const r = await ws({ type: "bas_wiresheet/list" }); setSheets(r.sheets); return r.sheets; }, [ws]);

  const loadSheet = useCallback(async (id, sheetObj) => {
    const d = await ws({ type: "bas_wiresheet/get", sheet_id: id });
    const s = sheetObj || d.draft;
    const { blocks, wires, ...m } = s;
    const f = sheetToFlow(s);
    setMeta(m); setNodes(f.nodes); setEdges(f.edges); setInfo(d); setSheetId(id); setSelection(null); setDirty(false);
    hist.current = { past: [], future: [] };
    setTimeout(() => { if (s.t3000 && s.t3000.imported) rf.setViewport({ x: 10, y: 10, zoom: 0.8 }); else if (f.nodes.length) rf.fitView({ padding: 0.15, maxZoom: 1 }); else rf.setViewport({ x: 0, y: 0, zoom: 1 }); }, 80);
  }, [ws, rf]);

  useEffect(() => { refreshList().then((l) => { if (l.length) loadSheet(initialSheet && l.some((x) => x.id === initialSheet) ? initialSheet : l[0].id); }); }, []); // eslint-disable-line

  // backend validation (debounced)
  useEffect(() => {
    if (!sheet) return undefined;
    const h = setTimeout(() => ws({ type: "bas_wiresheet/validate", sheet }).then((r) => setIssues(r.issues)).catch(() => {}), 300);
    return () => clearTimeout(h);
  }, [sheet, ws]);

  useEffect(() => { const h = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(h); }, []);

  // live monitoring subscription
  useEffect(() => {
    if (mode !== MODE.LIVE || !sheetId || (meta && meta.mode === "t3000")) { setLive(null); return undefined; }
    let unsub = null, cancelled = false;
    hassRef.current.connection.subscribeMessage((ev) => setLive(ev), { type: "bas_wiresheet/subscribe_live", sheet_id: sheetId })
      .then((u) => { if (cancelled) u(); else unsub = u; }).catch((e) => notify(e.message, "bad"));
    return () => { cancelled = true; if (unsub) unsub(); };
  }, [mode, sheetId]);

  // T3000 sheets: controller points as Home Assistant reports them (point picker and Live Monitoring)
  const [t3Points, setT3Points] = useState(null);
  const t3Instance = meta && meta.mode === "t3000" && meta.t3000 ? meta.t3000.device_instance : null;
  useEffect(() => {
    if (t3Instance === null || t3Instance === undefined) { setT3Points(null); return undefined; }
    let stop = false, deviceId = null;
    const load = async () => {
      try {
        if (!deviceId) {
          const d = (await ws({ type: "bas_wiresheet/t3_devices" })).devices.find((x) => x.instance === t3Instance);
          if (!d) { if (!stop) setT3Points([]); return; }
          deviceId = d.device_id;
        }
        const r = await ws({ type: "bas_wiresheet/t3_points", device_id: deviceId });
        if (!stop) setT3Points(r.points);
      } catch { if (!stop) setT3Points([]); }
    };
    load();
    const h = mode === MODE.LIVE ? setInterval(load, 5000) : null;
    return () => { stop = true; if (h) clearInterval(h); };
  }, [t3Instance, mode, ws]);
  // Live Monitoring of T3000 sheets: timers and locals from the running program (read only, every 10 s)
  const t3Slot = meta && meta.mode === "t3000" && meta.t3000 ? meta.t3000.program_slot : null;
  const t3Imported = !!(meta && meta.t3000 && meta.t3000.imported);
  const [t3Ctl, setT3Ctl] = useState(null);
  const [t3Build, setT3Build] = useState(null);
  useEffect(() => {
    if (mode !== MODE.LIVE || !t3Instance || !t3Slot) { setT3Ctl(null); return undefined; }
    let stop = false;
    const load = () => ws({ type: "bas_wiresheet/t3_program_state", instance: t3Instance, slot: t3Slot })
      .then((r) => { if (!stop) setT3Ctl(r); }).catch((e) => { if (!stop) setT3Ctl({ error: e.message }); });
    load();
    const h = setInterval(load, 10000);
    return () => { stop = true; clearInterval(h); };
  }, [mode, t3Instance, t3Slot, ws]);
  useEffect(() => {
    if (mode !== MODE.LIVE || !sheet || !meta || meta.mode !== "t3000" || t3Imported) { setT3Build(null); return; }
    ws({ type: "bas_wiresheet/t3_compile", sheet }).then((r) => setT3Build(r)).catch(() => setT3Build(null));
  }, [mode, sheetId, t3Imported]); // eslint-disable-line
  const t3Live = useMemo(() => (mode === MODE.LIVE && meta && meta.mode === "t3000" && t3Points && sheet
    ? { data: t3LiveSnapshot(sheet, t3Points, Date.now(), RT, t3Ctl, t3Build && t3Build.state_map, t3Build && t3Build.logic), received_at: Date.now() } : null),
  [mode, meta, t3Points, sheet, t3Ctl, t3Build]);

  const sim = useSimulation(sheet || { blocks: [], wires: [] }, mode === MODE.SIM && !!sheet);

  // ---- history ----
  const snapshotState = () => ({ nodes, edges, meta });
  const pushHist = () => { hist.current.past.push(snapshotState()); if (hist.current.past.length > 100) hist.current.past.shift(); hist.current.future = []; setDirty(true); };
  const undo = () => { const h = hist.current; if (!h.past.length) return; h.future.push(snapshotState()); const p = h.past.pop(); setNodes(p.nodes); setEdges(p.edges); setMeta(p.meta); setDirty(true); };
  const redo = () => { const h = hist.current; if (!h.future.length) return; h.past.push(snapshotState()); const p = h.future.pop(); setNodes(p.nodes); setEdges(p.edges); setMeta(p.meta); setDirty(true); };

  const clipboard = useRef(null);
  const selectedNodes = nodes.filter((n) => n.selected);
  const copySelected = () => {
    if (!selectedNodes.length) return false;
    const ids = new Set(selectedNodes.map((n) => n.id));
    clipboard.current = { nodes: JSON.parse(JSON.stringify(selectedNodes)), edges: JSON.parse(JSON.stringify(edges.filter((e) => ids.has(e.source) && ids.has(e.target)))) };
    notify(`${selectedNodes.length} block(s) copied`);
    return true;
  };
  const pasteClipboard = () => {
    const c = clipboard.current;
    if (readOnly || !c) return false;
    pushHist();
    const copy = cloneSelection(c.nodes, c.edges, new Set(nodes.map((n) => n.id)), new Set(edges.map((e) => e.id)));
    setNodes((n) => [...n.map((x) => ({ ...x, selected: false })), ...copy.nodes]);
    setEdges((e) => [...e.map((x) => ({ ...x, selected: false })), ...copy.edges]);
    clipboard.current = { nodes: JSON.parse(JSON.stringify(copy.nodes)), edges: JSON.parse(JSON.stringify(copy.edges)) };
    setSelection(copy.nodes.length === 1 ? { kind: "node", id: copy.nodes[0].id } : null);
    return true;
  };
  const deleteSelection = () => {
    if (readOnly) return;
    const multi = selectedNodes.length > 1 || edges.filter((e) => e.selected).length > 1 || (selectedNodes.length === 1 && !selection);
    if (multi) {
      const ids = new Set(selectedNodes.map((n) => n.id));
      pushHist();
      setNodes((n) => n.filter((x) => !ids.has(x.id)));
      setEdges((e) => e.filter((x) => !x.selected && !ids.has(x.source) && !ids.has(x.target)));
      setSelection(null);
      return;
    }
    if (!selection) return;
    pushHist();
    if (selection.kind === "node") { setNodes((n) => n.filter((x) => x.id !== selection.id)); setEdges((e) => e.filter((x) => x.source !== selection.id && x.target !== selection.id)); }
    else setEdges((e) => e.filter((x) => x.id !== selection.id));
    setSelection(null);
  };

  // keyboard (shadow-DOM safe: ignore keys typed into form fields)
  useEffect(() => {
    const h = (e) => {
      const path = e.composedPath ? e.composedPath() : [e.target];
      if (path.some((el) => el && el.tagName && /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;
      if (!wrapper.current || !path.includes(wrapper.current.getRootNode().host || wrapper.current)) { /* event from elsewhere in HA */ }
      const k = e.key.toLowerCase();
      if ((e.ctrlKey || e.metaKey) && k === "z" && !e.shiftKey) { e.preventDefault(); if (!readOnly) undo(); }
      else if ((e.ctrlKey || e.metaKey) && (k === "y" || (k === "z" && e.shiftKey))) { e.preventDefault(); if (!readOnly) redo(); }
      else if ((e.ctrlKey || e.metaKey) && k === "c") { if (copySelected()) e.preventDefault(); }
      else if ((e.ctrlKey || e.metaKey) && k === "v") { if (pasteClipboard()) e.preventDefault(); }
      else if ((e.ctrlKey || e.metaKey) && k === "d") { if (!readOnly && copySelected()) { e.preventDefault(); pasteClipboard(); } }
      else if ((e.ctrlKey || e.metaKey) && k === "a") { if (nodes.length) { e.preventDefault(); setNodes((n) => n.map((x) => ({ ...x, selected: true }))); setSelection(null); } }
      else if (k === "delete" || k === "backspace") { if ((selection || selectedNodes.length) && !readOnly) { e.preventDefault(); deleteSelection(); } }
    };
    window.addEventListener("keydown", h, true);
    return () => window.removeEventListener("keydown", h, true);
  });

  // The drag library swallows the click on a block inside Home Assistant's shadow DOM, so block selection comes from the pointer.
  useEffect(() => {
    const el = wrapper.current;
    if (!el) return undefined;
    let down = null;
    const nodeIn = (path) => path.find((x) => x.classList && x.classList.contains("react-flow__node"));
    const onDown = (e) => {
      const path = e.composedPath();
      const n = e.button === 0 && !path.some((x) => x.classList && x.classList.contains("react-flow__handle")) ? nodeIn(path) : null;
      down = n ? { id: n.getAttribute("data-id"), x: e.clientX, y: e.clientY } : null;
    };
    const onUp = (e) => {
      const d = down; down = null;
      if (!d || Math.hypot(e.clientX - d.x, e.clientY - d.y) > 4) return;
      const add = e.shiftKey || e.ctrlKey || e.metaKey;
      setNodes((ns) => ns.map((n) => (n.id === d.id ? { ...n, selected: true } : add ? n : { ...n, selected: false })));
      if (!add) setEdges((es) => es.map((x) => ({ ...x, selected: false })));
      setSelection(add ? null : { kind: "node", id: d.id });
    };
    el.addEventListener("pointerdown", onDown, true);
    el.addEventListener("pointerup", onUp, true);
    return () => { el.removeEventListener("pointerdown", onDown, true); el.removeEventListener("pointerup", onUp, true); };
  }, []);

  // ---- flow handlers ----
  const dragStart = useRef(null);
  const onNodesChange = (cs) => {
    const arr = cs.toArray ? cs.toArray() : cs;
    const filtered = readOnly ? arr.filter((c) => c.type === "select" || c.type === "dimensions") : arr.filter((c) => c.type !== "remove");
    const set = new NodeChangeset(filtered);
    setNodes((n) => set.applyTo(n));
  };
  const onEdgesChange = (cs) => {
    const arr = cs.toArray ? cs.toArray() : cs;
    const set = new EdgeChangeset(arr.filter((c) => c.type === "select"));
    setEdges((e) => set.applyTo(e));
  };
  const onConnect = (c) => {
    if (readOnly) return;
    pushHist();
    const taken = new Set(edges.map((e) => e.id));
    // an input accepts one wire: replace the existing one
    setEdges((e) => [...e.filter((x) => !(x.target === c.target && x.targetHandle === c.targetHandle)), { ...c, id: newId("w", taken), type: "bas" }]);
  };
  const onDrop = (e) => {
    e.preventDefault();
    const type = e.dataTransfer.getData("application/x-bas-block");
    if (!type || readOnly) return;
    addBlock(type, rf.screenToFlowPosition({ x: e.clientX, y: e.clientY }));
  };
  const addBlock = (type, pos) => {
    pushHist();
    const taken = new Set(nodes.map((n) => n.id));
    const id = newId(type.replace(/_/g, ""), taken);
    const block = { id, type, label: catalog.blocks[type].label, params: defaultParams(catalog, type), position: pos };
    setNodes((n) => [...n, { id, type: "bas", position: pos, data: { block }, selected: true }]);
    setSelection({ kind: "node", id });
  };
  const updateBlock = (id, fn) => { pushHist(); setNodes((n) => n.map((x) => (x.id === id ? { ...x, data: { block: fn({ ...x.data.block, params: { ...x.data.block.params } }) } } : x))); };

  // ---- persistence ----
  const saveDraft = async () => {
    const r = await ws({ type: "bas_wiresheet/save_draft", sheet });
    setDirty(false); setIssues(r.issues); await refreshList();
    setInfo(await ws({ type: "bas_wiresheet/get", sheet_id: sheet.id }));
    notify(r.valid ? "Draft saved" : `Draft saved with ${r.issues.filter((i) => i.severity === "error").length} validation error(s)`, r.valid ? "ok" : "warn");
  };
  const commit = async (note) => {
    if (dirty) await ws({ type: "bas_wiresheet/save_draft", sheet });
    const r = await ws({ type: "bas_wiresheet/commit", sheet_id: sheet.id, note });
    setDirty(false); await refreshList(); setInfo(await ws({ type: "bas_wiresheet/get", sheet_id: sheet.id }));
    notify(`Revision ${r.rev} saved`);
  };
  const exportSheet = () => {
    const blob = new Blob([JSON.stringify(sheet, null, 2)], { type: "application/json" });
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `${sheet.id}.wiresheet.json`; a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
  const importSheet = async (file) => {
    let obj;
    try { obj = JSON.parse(await file.text()); } catch { notify("Import failed: file is not valid JSON", "bad"); return; }
    try {
      const r = await ws({ type: "bas_wiresheet/import", sheet: obj });
      await refreshList(); await loadSheet(r.imported);
      notify(r.valid ? `Imported ${r.imported}` : `Imported ${r.imported} with validation errors`, r.valid ? "ok" : "warn");
    } catch (e) { notify(`Import failed: ${e.message}`, "bad"); }
  };
  const newSheet = async (id, name, t3) => {
    const s = { schema: SCHEMA, id, name, mode: t3 ? "t3000" : "monitoring", description: "", equipment: [], blocks: [], wires: [], ...(t3 ? { t3000: t3 } : {}) };
    try { await ws({ type: "bas_wiresheet/save_draft", sheet: s, create: true }); } catch (e) { notify(e.message, "bad"); return false; }
    await refreshList(); await loadSheet(id); return true;
  };

  const issuesByBlock = useMemo(() => { const m = {}; issues.forEach((i) => { if (i.block && !i.wire) (m[i.block] = m[i.block] || []).push(i); if (i.block && i.port && i.wire) (m[i.block] = m[i.block] || []).push(i); }); return m; }, [issues]);
  const issuesByWire = useMemo(() => { const m = {}; issues.forEach((i) => { if (i.wire) (m[i.wire] = m[i.wire] || []).push(i); }); return m; }, [issues]);
  const nodeById = useMemo(() => Object.fromEntries(nodes.map((n) => [n.id, n])), [nodes]);
  const equipment = useMemo(() => Object.fromEntries(((meta && meta.equipment) || []).map((e) => [e.tag, e])), [meta]);
  // Monitoring sheets without a Node-RED result: calculate in this browser from Home Assistant states (read only).
  const nodeRedLive = !!(live && Date.now() - live.received_at < 90000);
  const [browserLive, setBrowserLive] = useState(null);
  const browserRt = useRef(null);
  useEffect(() => {
    if (mode !== MODE.LIVE || !sheet || !meta || meta.mode === "t3000" || nodeRedLive) { browserRt.current = null; setBrowserLive(null); return undefined; }
    let rt;
    try { rt = RT.createRuntime({ ...sheet, revision: null }); } catch { setBrowserLive(null); return undefined; }
    browserRt.current = rt;
    let stop = false, pollOk = 0;
    const ids = rt.entityIds();
    const poll = () => ws({ type: "bas_wiresheet/live_states", entities: ids }).then((r) => {
      if (stop) return;
      Object.entries(r.states).forEach(([id, x]) => { if (x) rt.ingest({ entity_id: id, state: x[0], attributes: x[1], last_reported: x[2], last_changed: x[3] }, Date.now()); });
      pollOk = Date.now(); rt.setTimestampSource(true);
    }).catch(() => { if (!stop) rt.setTimestampSource(false, "Home Assistant states could not be read"); });
    const tick = () => {
      if (stop) return;
      if (pollOk && Date.now() - pollOk > 20000) rt.setTimestampSource(false, `Home Assistant states not read for ${Math.round((Date.now() - pollOk) / 1000)} s`);
      setBrowserLive({ data: { ...rt.tick(Date.now()), browser: true, started_at: startedAt }, received_at: Date.now() });
    };
    const startedAt = Date.now();
    poll().then(tick);
    const hp = setInterval(poll, 5000), ht = setInterval(tick, 1000);
    return () => { stop = true; clearInterval(hp); clearInterval(ht); };
  }, [mode, sheetId, meta && meta.mode, nodeRedLive, sheet && JSON.stringify(sheet.blocks), sheet && JSON.stringify(sheet.wires)]); // eslint-disable-line
  const liveNow = t3Live || (nodeRedLive ? live : browserLive) || live;
  const liveSnap = liveNow && liveNow.data;
  const liveOk = !!(liveSnap && now - liveNow.received_at < 90000);
  const snapshot = mode === MODE.SIM ? sim.state && sim.state.snapshot : mode === MODE.LIVE ? liveSnap : null;
  const wireColor = (id) => {
    const e = edges.find((x) => x.id === id); const n = e && nodeById[e.source];
    if (!n) return "#667";
    const p = catalog.blocks[n.data.block.type]?.outputs.find((o) => o.id === e.sourceHandle);
    return p ? catalog.port_types[resolvePortType(n.data.block, p, catalog)]?.color : "#667";
  };
  const errors = issues.filter((i) => i.severity === "error");
  const ctx = { t3Points, catalog, hass, issuesByBlock, issuesByWire, snapshot, mode, readOnly, equipment, now: mode === MODE.SIM && sim.state ? sim.state.clock : now, nodeById, liveOk, wireColor };

  const selNode = selection && selection.kind === "node" && nodeById[selection.id];
  const trendEntity = (() => {
    const b = selNode && selNode.data && selNode.data.block;
    if (!b) return null;
    const p = b.params || {};
    if (b.type === "entity_input") return p.entity_id && hass.states[p.entity_id] ? p.entity_id : null;
    if ((b.type === "t3_read" || b.type === "t3_write") && p.point && t3Points) {
      const r = t3Points.find((x) => `${x.category}${x.index}` === String(p.point).toUpperCase());
      return r ? r.entity_id : null;
    }
    return null;
  })();
  const exportDocument = async () => {
    const sizes = Object.fromEntries(nodes.map((n) => [n.id, { w: (n.measured && n.measured.width) || n.width || 200, h: (n.measured && n.measured.height) || n.height || 80 }]));
    let t3Build = null;
    if (sheet.mode === "t3000" && !(sheet.t3000 && sheet.t3000.imported)) { try { t3Build = await ws({ type: "bas_wiresheet/t3_compile", sheet }); } catch { t3Build = null; } }
    const html = buildDocument({ sheet, sizes, info, catalog: catalog && catalog.blocks, t3Build, t3Points, user: hass.user && hass.user.name, site: hass.config && hass.config.location_name });
    const blob = new Blob([html], { type: "text/html" });
    const url = URL.createObjectURL(blob);
    const win = window.open(url, "_blank");
    const a = document.createElement("a"); a.href = url; a.download = `${sheet.id}-documentation.html`;
    if (!win) a.click();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    notify(win ? "Documentation opened in a new tab: use Print / Save as PDF" : "Documentation downloaded");
  };
  const selEdge = selection && selection.kind === "edge" && edges.find((e) => e.id === selection.id);
  const imported = !!(meta && meta.t3000 && meta.t3000.imported);
  const latestRev = info && info.revisions.length ? info.revisions[info.revisions.length - 1].rev : null;
  const lastDeploy = info && [...info.deployments].reverse().find((d) => d.target === "test");

  const pickIssue = (i) => {
    if (i.wire) { setSelection({ kind: "edge", id: i.wire }); setEdges((e) => e.map((x) => ({ ...x, selected: x.id === i.wire }))); }
    else if (i.block) { setSelection({ kind: "node", id: i.block }); const n = nodeById[i.block]; if (n) rf.setCenter(n.position.x + 120, n.position.y + 60, { zoom: 1.1, duration: 300 }); }
  };

  return (
    <Ctx.Provider value={ctx}>
      <div className="app" ref={wrapper}>
        <header className="top">
          <div className="brand">BAS Wire Sheet</div>
          <select value={sheetId || ""} onChange={(e) => loadSheet(e.target.value)} data-testid="sheet-select">
            {sheets.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
          {meta && <span className="mono muted">{meta.id}</span>}
          {imported && <span className="badge comm" data-testid="imported-badge" title="Generated from a controller program. It documents that program and cannot be loaded back.">From Program · Documentation</span>}
          {meta && (meta.equipment || []).map((e) => <span key={e.tag} className="badge comm" title={`Since ${e.since} · ${e.source}`}>{e.tag} · Commissioning Pending</span>)}
          <div className="seg" role="tablist">
            {Object.values(MODE).map((m) => <button key={m} className={`seg-b ${mode === m ? "on" : ""}`} onClick={() => setMode(m)} data-testid={`mode-${m}`}>{m}</button>)}
          </div>
          <span className="grow" />
          <span className={`badge role ${role}`}>{role === "editor" ? "Editor" : "Operator · View Only"}</span>
        </header>
        <div className="toolbar">
          {role === "editor" && <>
            <button className="btn" onClick={() => setDialog({ kind: "new" })}>New Sheet</button>
            <button className="btn" onClick={() => setDialog({ kind: "template" })} data-testid="new-from-template">New from Template</button>
            <button className="btn" disabled={mode !== MODE.EDIT || !hist.current.past.length} onClick={undo} title="Ctrl+Z">Undo</button>
            <button className="btn" disabled={mode !== MODE.EDIT || !hist.current.future.length} onClick={redo} title="Ctrl+Shift+Z">Redo</button>
            <button className="btn primary" disabled={!sheet || mode !== MODE.EDIT || sheetLocked} onClick={saveDraft} data-testid="save-draft">Save Draft{dirty ? " *" : ""}</button>
            <button className="btn" disabled={!sheet || errors.length > 0 || sheetLocked} onClick={() => setDialog({ kind: "commit" })} title={errors.length ? "Fix validation errors first" : ""} data-testid="save-revision">Save Revision</button>
            <label className="btn">Import<input type="file" accept="application/json,.json" hidden onChange={(e) => e.target.files[0] && importSheet(e.target.files[0])} /></label>
          </>}
          <button className="btn" disabled={!sheet} onClick={exportSheet}>Export</button>
          <button className="btn" disabled={!sheet} onClick={exportDocument} data-testid="export-doc">Document</button>
          <button className="btn" disabled={!info} onClick={() => setDialog({ kind: "revisions" })}>Revisions{latestRev ? ` (r${latestRev})` : ""}</button>
          {role === "editor" && <>
            <span className="sep" />
            <button className="btn" disabled={!latestRev || !targets.length || (sheet && sheet.mode === "t3000") || sheetLocked} onClick={() => setDialog({ kind: "deploy", rev: latestRev })} data-testid="deploy">Deploy to Test</button>
            <button className="btn" disabled={!lastDeploy || sheetLocked} onClick={() => setDialog({ kind: "rollback" })}>Rollback Test</button>
            <button className="btn" disabled title={production.reason}>Publish to Production</button>
            {sheet && sheet.mode === "t3000" && <button className="btn" disabled={imported} title={imported ? "Generated from a controller program: documentation only" : ""} onClick={() => setDialog({ kind: "t3compile" })} data-testid="t3-compile">Compile T3000</button>}
            {sheet && sheet.mode === "t3000" && <button className="btn" disabled={!latestRev || imported || sheetLocked} title={imported ? "Generated from a controller program: documentation only" : latestRev ? "" : "Save a revision first"} onClick={() => setDialog({ kind: "t3upload", rev: latestRev })} data-testid="t3-upload">Controller…</button>}
            <button className="btn" disabled={!sheet || sheetLocked} onClick={() => setDialog({ kind: "delete" })} data-testid="delete-sheet">Delete Sheet</button>
          </>}
          <span className="grow" />
          <span className={`vstate ${errors.length ? "bad" : "ok"}`} onClick={() => setSelection(null)}>{errors.length ? `${errors.length} Validation Error(s)` : "Validation Passed"}</span>
          {lastDeploy && <span className="muted small">Test: r{lastDeploy.rev ?? "—"} {lastDeploy.status}</span>}
        </div>
        <div className="main">
          <aside className="lib">
            <div className="pane-h">Block Library</div>
            <input className="lib-search" placeholder="Search blocks" value={libFilter} onChange={(e) => setLibFilter(e.target.value)} data-testid="lib-search" />
            {catalog.categories.map((cat) => {
              const items = Object.entries(catalog.blocks).filter(([t, d]) => d.category === cat && (!(meta && meta.mode === "t3000") || !["entity_input", "data_freshness"].includes(t))
                && (!libFilter || `${d.label} ${d.description} ${cat}`.toLowerCase().includes(libFilter.toLowerCase())));
              if (!items.length) return null;
              return (
              <div key={cat} className="lib-cat" style={{ "--cat": (CATEGORY_STYLE[cat] || {}).color }}>
                <div className="lib-cat-h"><span className="lib-dot" />{cat}</div>
                {items.map(([t, d]) => (
                  <div key={t} className={`lib-item ${readOnly ? "dis" : ""}`} draggable={!readOnly} title={d.description}
                    onDragStart={(e) => { e.dataTransfer.setData("application/x-bas-block", t); e.dataTransfer.effectAllowed = "move"; }}
                    onDoubleClick={() => !readOnly && addBlock(t, rf.screenToFlowPosition({ x: 500, y: 300 }))} data-testid={`lib-${t}`}>
                    <span className="lib-icon">{BLOCK_ICON[t] || "■"}</span><span className="lib-label">{d.label}</span>
                    <span className="lib-desc">{d.description}</span>
                  </div>
                ))}
              </div>
              );
            })}
            {readOnly && <div className="muted small pad">{role === "editor" ? "Switch to Edit mode to change the sheet." : "View only. Editing requires editor permission."}</div>}
          </aside>
          <section className="canvas" onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; }} onDrop={onDrop}>
            {mode !== MODE.EDIT && <div className={`canvas-mode ${mode === MODE.SIM ? "sim" : "live"}`}>{mode === MODE.SIM ? "SIMULATION · values are simulated" : liveOk ? (liveSnap.controller ? `LIVE MONITORING · ${CONTROLLER_TEXT[liveSnap.controller] || liveSnap.controller}` : (liveSnap.runtime && liveSnap.runtime.host === "home-assistant" ? `LIVE MONITORING · running in Home Assistant · r${liveSnap.revision} · updated ${fmtAge(now - liveNow.received_at)} ago` : liveSnap.browser ? `LIVE MONITORING · calculated in this browser from Home Assistant states · timers started ${fmtAge(now - liveSnap.started_at)} ago` : `LIVE MONITORING · runtime rev ${liveSnap.revision} · updated ${fmtAge(now - liveNow.received_at)} ago`)) : "LIVE MONITORING · No live telemetry from the runtime"}</div>}
            <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes}
              onNodesChange={onNodesChange} onEdgesChange={onEdgesChange} onConnect={onConnect}
              onNodeDragStart={() => { dragStart.current = snapshotState(); }}
              onNodeDragStop={() => { if (dragStart.current) { hist.current.past.push(dragStart.current); hist.current.future = []; dragStart.current = null; setDirty(true); } }}
              onNodeClick={(_, n) => setSelection({ kind: "node", id: n.id })}
              onEdgeClick={(_, e) => setSelection({ kind: "edge", id: e.id })}
              onPaneClick={(ev) => { const path = ev.nativeEvent && ev.nativeEvent.composedPath ? ev.nativeEvent.composedPath() : []; if (path.some((el) => el.classList && (el.classList.contains("react-flow__node") || el.classList.contains("react-flow__edge")))) return; setSelection(null); }}
              nodesDraggable={!readOnly} nodesConnectable={!readOnly} elementsSelectable deleteKeyCode={null}
              minZoom={0.2} maxZoom={2.5} proOptions={{ hideAttribution: true }} fitView fitViewOptions={{ maxZoom: 1 }} colorMode="dark">
              <Background gap={20} size={1} color="#243041" />
              <Controls showInteractive={false} />
              <MiniMap pannable zoomable nodeColor={(n) => ((issuesByBlock[n.id] || []).some((i) => i.severity === "error") ? "#e5484d" : issuesByBlock[n.id] ? "#e08a2e" : "#3b4a5e")} maskColor="rgba(10,14,20,.7)" />
            </ReactFlow>
          </section>
          <aside className="insp-pane">
            <div className="pane-h">Inspector</div>
            {selNode ? (
              <>
              <BlockInspector node={selNode}
                onParam={(k, v) => updateBlock(selNode.id, (b) => { b.params[k] = v; return b; })}
                onLabel={(v) => updateBlock(selNode.id, (b) => { b.label = v; return b; })}
                onDelete={deleteSelection} />
              {trendEntity && <div className="insp"><TrendChart key={trendEntity} ws={ws} entityId={trendEntity} title={selNode.data.block.label || trendEntity} /></div>}
              </>
            ) : selEdge ? (
              <WireInspector edge={selEdge} onDelete={deleteSelection} />
            ) : meta ? (
              <div className="insp">
                <h3>Sheet</h3>
                <label className="field"><span>Name</span><input value={meta.name} disabled={readOnly} onChange={(e) => { pushHist(); setMeta({ ...meta, name: e.target.value }); }} /></label>
                <label className="field"><span>Description</span><textarea rows={4} value={meta.description || ""} disabled={readOnly} onChange={(e) => { pushHist(); setMeta({ ...meta, description: e.target.value }); }} /></label>
                <div className="kv"><span>Mode</span><b>{sheet && sheet.mode === "t3000" ? "T3000 Program" : "Monitoring Only"}</b></div>
                {meta.mode === "t3000" && <T3Fields value={meta.t3000} disabled={readOnly} onChange={(t) => { pushHist(); setMeta({ ...meta, t3000: t }); }} />}
                <div className="kv"><span>Draft Saved</span><b>{info ? `${fmtTime(Date.parse(info.draft_updated_at))} by ${info.draft_updated_by}` : "—"}</b></div>
                <div className="kv"><span>Latest Revision</span><b>{latestRev ? `r${latestRev}` : "None"}</b></div>
                {info && <SheetRuntime ws={ws} sheetId={sheetId} info={info} role={role} latestRev={latestRev} mode={meta.mode} onChanged={async () => setInfo(await ws({ type: "bas_wiresheet/get", sheet_id: sheetId }))} />}
                {role === "editor" && info && <SheetAccess ws={ws} sheetId={sheetId} info={info} onSaved={async () => setInfo(await ws({ type: "bas_wiresheet/get", sheet_id: sheetId }))} />}
                {(meta.equipment || []).map((e) => <div key={e.tag} className="kv"><span>{e.tag}</span><b><span className="badge comm">Commissioning Pending</span> since {e.since}</b></div>)}
                <div className="pane-h sub">Validation</div>
                {issues.length ? <IssueList issues={issues} onPick={pickIssue} /> : <div className="ok-txt">No issues. The sheet can be simulated and saved as a revision.</div>}
              </div>
            ) : <div className="muted pad">No sheet loaded.</div>}
          </aside>
        </div>
        {sheet && mode !== MODE.EDIT && (
          <div className="bottom">
            {mode === MODE.SIM && <SimPanel sim={sim} sheet={sheet} />}
            {mode === MODE.LIVE && meta && meta.mode === "t3000" && <div className="muted small pad" data-testid="t3-live-note">Point values come from the controller through Home Assistant and logic between blocks is recalculated from them. Timers, latches, PI and local variables are read from the running controller program every 10 s (read only) when it matches this sheet; otherwise they show No Telemetry.</div>}
            {mode === MODE.LIVE && !liveOk && <div className="notel pad">No Telemetry: no result from the Node-RED runtime for this sheet{liveNow ? ` (last ${fmtAge(now - liveNow.received_at)} ago)` : ""}. Deploy a revision to the test runtime to monitor.</div>}
            <PointsTable sheet={sheet} snapshot={snapshot} equipment={equipment} />
          </div>
        )}
        {toast && <div className={`toast ${toast.kind}`}>{toast.msg}</div>}
        {dialog && dialog.kind === "template" && <TemplateDialog ws={ws} hass={hass} Modal={Modal} onClose={() => setDialog(null)} onCreate={async (sh) => {
          const r = await ws({ type: "bas_wiresheet/save_draft", sheet: sh, create: true });
          await refreshList(); await loadSheet(sh.id); setDialog(null); notify(r && r.valid === false ? `Created ${sh.id} with validation issues` : `Created ${sh.id}`);
        }} />}
        {dialog && dialog.kind === "new" && <NewDialog onClose={() => setDialog(null)} onCreate={async (id, name, t3) => { if (await newSheet(id, name, t3)) setDialog(null); }} />}
        {dialog && dialog.kind === "commit" && <CommitDialog onClose={() => setDialog(null)} onCommit={async (n) => { try { await commit(n); setDialog(null); } catch (e) { notify(e.message, "bad"); } }} />}
        {dialog && dialog.kind === "revisions" && info && (
          <RevisionsDialog info={info} role={role} onClose={() => setDialog(null)}
            onView={async (rev) => { const d = await ws({ type: "bas_wiresheet/get", sheet_id: sheetId, revision: rev }); await loadSheet(sheetId, d.revision_sheet); setMode(MODE.EDIT); setDialog(null); notify(`Viewing revision ${rev} (not saved as draft)`, "warn"); setDirty(true); }}
            onRestore={async (rev) => { await ws({ type: "bas_wiresheet/restore", sheet_id: sheetId, revision: rev }); await loadSheet(sheetId); setDialog(null); notify(`Revision ${rev} restored as draft`); }}
            onDeploy={(rev) => setDialog({ kind: "deploy", rev })} />
        )}
        {dialog && dialog.kind === "t3upload" && <T3UploadDialog ws={ws} sheetId={sheetId} rev={dialog.rev} info={info} onClose={() => setDialog(null)} onChanged={async () => setInfo(await ws({ type: "bas_wiresheet/get", sheet_id: sheetId }))} />}
        {dialog && dialog.kind === "delete" && <DeleteDialog ws={ws} sheet={sheet} onClose={() => setDialog(null)} onDone={async () => { setDialog(null); const r = await ws({ type: "bas_wiresheet/list" }); await refreshList(); if (r.sheets.length) await loadSheet(r.sheets[0].id); else window.location.reload(); notify("Sheet deleted"); }} />}
        {dialog && dialog.kind === "t3compile" && <T3CompileDialog ws={ws} sheet={sheet} onClose={() => setDialog(null)} />}
        {dialog && dialog.kind === "deploy" && <DeployDialog ws={ws} sheetId={sheetId} rev={dialog.rev} onClose={() => setDialog(null)} onDone={async (r) => { setDialog(null); setInfo(await ws({ type: "bas_wiresheet/get", sheet_id: sheetId })); notify(`Revision ${r.rev} deployed to test Node-RED (${r.status})`); }} />}
        {dialog && dialog.kind === "rollback" && (
          <Modal title="Rollback Test Deployment" onClose={() => setDialog(null)} actions={<button className="btn danger" onClick={async () => {
            try { const r = await ws({ type: "bas_wiresheet/rollback", sheet_id: sheetId }); setDialog(null); setInfo(await ws({ type: "bas_wiresheet/get", sheet_id: sheetId })); notify(r.removed ? "Rolled back: generated tab removed from test Node-RED" : `Rolled back to revision ${r.restored_rev}`); }
            catch (e) { notify(e.message, "bad"); }
          }}>Roll Back</button>}>
            <p>Restores the backup taken before the last deployment of this sheet to the test Node-RED instance (r{lastDeploy && lastDeploy.rev}). If no tab existed before, the generated tab is removed. Other Node-RED tabs are never touched.</p>
          </Modal>
        )}
      </div>
    </Ctx.Provider>
  );
}

function T3Fields({ value, onChange, disabled }) {
  const v = value || {};
  const set = (k, x) => onChange({ ...v, [k]: x });
  return (
    <>
      <label className="field"><span>Controller (BACnet Instance)</span><input type="number" value={v.device_instance ?? ""} disabled={disabled} onChange={(e) => set("device_instance", e.target.value === "" ? null : Number(e.target.value))} data-testid="t3-device" /></label>
      <label className="field"><span>Program (1-16)</span><input type="number" min={1} max={16} value={v.program_slot ?? ""} disabled={disabled} onChange={(e) => set("program_slot", e.target.value === "" ? null : Number(e.target.value))} data-testid="t3-slot" /></label>
      <label className="field"><span>Work Variables</span><input value={(v.work_vars || []).join(", ")} disabled={disabled} placeholder="VAR125-128" onChange={(e) => set("work_vars", e.target.value.split(",").map((x) => x.trim().toUpperCase()).filter(Boolean))} data-testid="t3-work" /><small>Sandbox variables the program may use for timers and intermediate results.</small></label>
    </>
  );
}

function NewDialog({ onClose, onCreate }) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState("monitoring");
  const [t3, setT3] = useState({ device_instance: null, program_slot: 16, work_vars: ["VAR125-128"] });
  const id = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 64);
  const ready = id && (kind === "monitoring" || (t3.device_instance !== null && t3.program_slot));
  return (
    <Modal title="New Sheet" onClose={onClose} actions={<button className="btn primary" disabled={!ready} onClick={() => onCreate(id, name, kind === "t3000" ? t3 : null)} data-testid="create-sheet">Create</button>}>
      <label className="field"><span>Name</span><input autoFocus value={name} onChange={(e) => setName(e.target.value)} data-testid="new-name" /></label>
      <label className="field"><span>Type</span><select value={kind} onChange={(e) => setKind(e.target.value)} data-testid="new-kind"><option value="monitoring">Monitoring (Home Assistant)</option><option value="t3000">T3000 Controller Program</option></select></label>
      {kind === "t3000" && <T3Fields value={t3} onChange={setT3} />}
      <div className="kv"><span>Sheet ID</span><b className="mono">{id || "—"}</b></div>
    </Modal>
  );
}
function CommitDialog({ onClose, onCommit }) {
  const [note, setNote] = useState("");
  return (
    <Modal title="Save Revision" onClose={onClose} actions={<button className="btn primary" onClick={() => onCommit(note)} data-testid="commit">Save Revision</button>}>
      <p>Saves the current draft as a new immutable revision. Only valid sheets can be saved as revisions.</p>
      <label className="field"><span>Change Note</span><input autoFocus value={note} onChange={(e) => setNote(e.target.value)} /></label>
    </Modal>
  );
}
function RevisionsDialog({ info, role, onClose, onView, onRestore, onDeploy }) {
  return (
    <Modal title="Revisions" onClose={onClose}>
      <table className="simtab">
        <thead><tr><th>Revision</th><th>Saved</th><th>By</th><th>Note</th><th></th></tr></thead>
        <tbody>
          {[...info.revisions].reverse().map((r) => (
            <tr key={r.rev}>
              <td className="mono">r{r.rev}</td><td>{fmtTime(Date.parse(r.saved_at))}</td><td>{r.saved_by}</td><td>{r.note}</td>
              <td>
                <button className="btn sm" onClick={() => onView(r.rev)}>View</button>
                {role === "editor" && <button className="btn sm" onClick={() => onRestore(r.rev)}>Restore as Draft</button>}
                {role === "editor" && <button className="btn sm" onClick={() => onDeploy(r.rev)}>Deploy to Test</button>}
              </td>
            </tr>
          ))}
          {!info.revisions.length && <tr><td colSpan={5} className="muted">No revisions yet.</td></tr>}
        </tbody>
      </table>
      <div className="pane-h sub">Deployment History</div>
      <table className="simtab">
        <thead><tr><th>When</th><th>Target</th><th>Action</th><th>Revision</th><th>Status</th><th>By</th></tr></thead>
        <tbody>{[...info.deployments].reverse().map((d, k) => <tr key={k}><td>{fmtTime(Date.parse(d.deployed_at))}</td><td>{d.target}</td><td>{d.kind}</td><td>{d.rev != null ? `r${d.rev}` : "—"}</td><td>{d.status}</td><td>{d.by}</td></tr>)}</tbody>
      </table>
    </Modal>
  );
}
function T3CompileDialog({ ws, sheet, onClose }) {
  const [r, setR] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => { ws({ type: "bas_wiresheet/t3_compile", sheet }).then(setR).catch((e) => setErr(e.message)); }, []); // eslint-disable-line
  const problems = r ? r.issues.filter((i) => i.severity === "error") : [];
  return (
    <Modal title="Compile for T3000 controller" onClose={onClose}>
      {err && <div className="errtxt">{err}</div>}
      {!r && !err && <div className="muted">Compiling…</div>}
      {problems.map((i, k) => <div className="errtxt" key={k}>{i.block ? `${i.block}: ` : ""}{i.message}</div>)}
      {r && r.sha256 && (
        <div data-testid="t3-result">
          <div className="kv"><span>Controller</span><b>{sheet.t3000.device_instance} · Program {sheet.t3000.program_slot}</b></div>
          <div className="kv"><span>Size</span><b>{r.length} of 2000 bytes</b></div>
          <div className="kv"><span>Writes</span><b className="mono">{r.writes.join(", ")}</b></div>
          <div className="kv"><span>Work Variables</span><b className="mono">{r.work_vars.join(", ") || "None"}</b></div>
          <div className="kv"><span>SHA-256</span><b className="mono small">{r.sha256}</b></div>
          <pre className="mono small" data-testid="t3-listing">{r.listing}</pre>
          <p className="muted small">Compiled with T3000's own compiler and verified by round trip. Nothing has been sent to a controller.</p>
        </div>
      )}
    </Modal>
  );
}

function DeleteDialog({ ws, sheet, onClose, onDone }) {
  const [err, setErr] = useState(null);
  return (
    <Modal title={`Delete ${sheet.name}`} onClose={onClose} actions={<button className="btn danger" data-testid="confirm-delete" onClick={async () => { try { await ws({ type: "bas_wiresheet/delete_sheet", sheet_id: sheet.id }); onDone(); } catch (e) { setErr(e.message); } }}>Delete</button>}>
      <p>This permanently deletes the sheet and all its revisions. A sheet that is still deployed or loaded on a controller cannot be deleted.</p>
      {err && <div className="errtxt">{err}</div>}
    </Modal>
  );
}

function SheetAccess({ ws, sheetId, info, onSaved }) {
  const [data, setData] = useState(null);
  const [sel, setSel] = useState([]);
  const [err, setErr] = useState(null);
  const [msg, setMsg] = useState(null);
  useEffect(() => {
    let stop = false;
    ws({ type: "bas_wiresheet/sheet_access", sheet_id: sheetId }).then((r) => { if (!stop) { setData(r); setSel(r.editors); } }).catch((e) => !stop && setErr(e.message));
    return () => { stop = true; };
  }, [sheetId]); // eslint-disable-line
  if (!data) return err ? <div className="errtxt">{err}</div> : null;
  const can = info.can_edit !== false;
  const toggle = (id) => setSel((x) => (x.includes(id) ? x.filter((y) => y !== id) : [...x, id]));
  const save = async () => {
    setErr(null); setMsg(null);
    try { const r = await ws({ type: "bas_wiresheet/sheet_access", sheet_id: sheetId, editors: sel }); setData({ ...data, editors: r.editors }); setMsg("Access saved"); await onSaved(); }
    catch (e) { setErr(e.message); }
  };
  const changed = JSON.stringify([...sel].sort()) !== JSON.stringify([...data.editors].sort());
  return (
    <div data-testid="sheet-access">
      <div className="pane-h sub">Who Can Edit</div>
      {!can && <div className="errtxt" data-testid="sheet-locked">This sheet is restricted: you can view, simulate and monitor it, but not change it.</div>}
      <div className="muted small">{data.editors.length ? "Only the selected users (and administrators) can change, load or deploy this sheet." : "Every editor can change this sheet. Select users to restrict it."}</div>
      {data.users.map((u) => (
        <label key={u.id} className="field" style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
          <input type="checkbox" disabled={!can} checked={sel.includes(u.id)} onChange={() => toggle(u.id)} data-testid={`access-${u.name}`} />
          <span>{u.name}{u.admin ? " (administrator)" : ""}</span>
        </label>
      ))}
      {can && <button className="btn sm" disabled={!changed} onClick={save} data-testid="access-save">Save Access</button>}
      {msg && <div className="ok-txt small">{msg}</div>}
      {err && <div className="errtxt small" data-testid="access-error">{err}</div>}
    </div>
  );
}

function T3UploadDialog({ ws, sheetId, rev, info, onClose, onChanged }) {
  const last = info ? [...info.deployments].reverse().find((d) => d.kind === "t3000") : null;
  const [status, setStatus] = useState(last ? last.status : null);
  const [plan, setPlan] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [replace, setReplace] = useState(false);
  const [ack, setAck] = useState(false);
  const act = async (fn) => { setBusy(true); setErr(null); try { await fn(); } catch (e) { setErr(e.message); } setBusy(false); };
  const doPlan = () => act(async () => { setPlan(await ws({ type: "bas_wiresheet/t3_upload_plan", sheet_id: sheetId, revision: rev, allow_replace: replace })); setAck(false); });
  const doApply = () => act(async () => { const r = await ws({ type: "bas_wiresheet/t3_upload_apply", token: plan.token }); setPlan(null); await onChanged(); if (r.ok) setStatus("loaded"); else throw new Error(`Upload failed: ${r.error}. Previous program ${r.restored ? "restored" : "NOT restored: " + r.restore_error}`); });
  const control = (action) => act(async () => { const r = await ws({ type: "bas_wiresheet/t3_program_control", sheet_id: sheetId, action }); setStatus(r.status); await onChanged(); });
  const loaded = ["loaded", "running", "stopped"].includes(status);
  return (
    <Modal title={`Controller Program · r${rev}`} onClose={onClose}>
      {err && <div className="errtxt" data-testid="t3-error">{err}</div>}
      <div className="kv"><span>Status</span><b data-testid="t3-status">{status === "running" ? "Running" : status === "loaded" ? "Loaded, stopped" : status === "stopped" ? "Stopped" : "Not loaded"}</b></div>
      {!plan && !loaded && <>
        <label className="field"><span><input type="checkbox" checked={replace} onChange={(e) => setReplace(e.target.checked)} /> Allow replacing an existing stopped program</span></label>
        <button className="btn primary" disabled={busy} onClick={doPlan} data-testid="t3-plan">Check Controller</button>
        <p className="muted small">Checking only reads the controller. Nothing is written.</p>
      </>}
      {plan && <div data-testid="t3-plan-result">
        <div className="kv"><span>Controller</span><b>{plan.instance} · Program {plan.program}</b></div>
        <div className="kv"><span>New / Existing</span><b>{plan.new_bytes} / {plan.existing_bytes} bytes</b></div>
        <div className="kv"><span>Writes</span><b className="mono">{plan.writes.join(", ")}</b></div>
        <div className="kv"><span>Work Variables</span><b className="mono">{plan.work_vars.join(", ") || "None"}</b></div>
        <pre className="mono small">{plan.listing}</pre>
        <label className="field"><span><input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} data-testid="t3-ack" /> I reviewed this program. Load it to the controller and leave it stopped.</span></label>
        <button className="btn primary" disabled={!ack || busy} onClick={doApply} data-testid="t3-apply">Load Program</button>
      </div>}
      {loaded && <div className="row">
        <button className="btn" disabled={busy || status === "running"} onClick={() => control("start")} data-testid="t3-start">Start</button>
        <button className="btn" disabled={busy || status !== "running"} onClick={() => control("stop")} data-testid="t3-stop">Stop</button>
        <button className="btn danger" disabled={busy || status === "running"} onClick={() => control("remove")} data-testid="t3-remove">Remove from Controller</button>
      </div>}
      {busy && <div className="muted">Working with the controller…</div>}
    </Modal>
  );
}

function DeployDialog({ ws, sheetId, rev, onClose, onDone }) {
  const [p, setP] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => { ws({ type: "bas_wiresheet/deploy_preview", sheet_id: sheetId, revision: rev }).then(setP).catch((e) => setErr(e.message)); }, []); // eslint-disable-line
  return (
    <Modal title={`Deploy r${rev} to Test Node-RED`} onClose={onClose}
      actions={p && <button className="btn primary" disabled={busy} data-testid="confirm-deploy" onClick={async () => { setBusy(true); try { onDone(await ws({ type: "bas_wiresheet/deploy", token: p.token })); } catch (e) { setErr(e.message); setBusy(false); } }}>Deploy to {p.target_name}</button>}>
      {err && <div className="errtxt">{err}</div>}
      {!p && !err && <div className="muted">Building preview…</div>}
      {p && (
        <div data-testid="deploy-preview">
          <div className="kv"><span>Target</span><b>{p.target_name} (test)</b></div>
          <div className="kv"><span>Tab</span><b className="mono">{p.label} · {p.tab_id}</b></div>
          <div className="kv"><span>Currently Deployed</span><b>{p.currently_deployed_rev ? `r${p.currently_deployed_rev}` : "Nothing"}</b></div>
          <div className="kv"><span>Backup</span><b>{p.backup}</b></div>
          <div className="kv"><span>Change Summary</span><b>{p.diff.summary}</b></div>
          <table className="simtab"><thead><tr><th>Node</th><th>Type</th><th>Change</th><th>Fields</th></tr></thead>
            <tbody>{p.diff.nodes.map((n) => <tr key={n.id}><td>{n.name}</td><td className="mono">{n.type}</td><td className={`chg-${n.change}`}>{n.change}</td><td className="mono small">{(n.fields || []).join(", ")}</td></tr>)}
              {!p.diff.nodes.length && <tr><td colSpan={4} className="muted">No node changes.</td></tr>}</tbody></table>
          <p className="muted small">Only this generated tab is created or replaced. The runtime is monitoring-only: it reads entity states and fires the bas_wiresheet_result event. Production publishing is locked pending explicit approval.</p>
        </div>
      )}
    </Modal>
  );
}

export default function App({ hass }) {
  const [boot, setBoot] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => { hass.callWS({ type: "bas_wiresheet/catalog" }).then(setBoot).catch((e) => setErr(e.message)); }, []); // eslint-disable-line
  if (err) return <div className="app"><div className="notel pad">BAS Wire Sheet backend error: {err}</div></div>;
  if (!boot) return <div className="app"><div className="muted pad">Loading BAS Wire Sheet…</div></div>;
  return <Shell hass={hass} boot={boot} />;
}

function Shell({ hass, boot }) {
  const [view, setView] = useState("sheets");
  const [openSheet, setOpenSheet] = useState(null);
  return (
    <div className="shell">
      <nav className="shell-nav">
        <button className={`seg-b ${view === "sheets" ? "on" : ""}`} onClick={() => setView("sheets")} data-testid="view-sheets">Wire Sheets</button>
        <button className={`seg-b ${view === "t3000" ? "on" : ""}`} onClick={() => setView("t3000")} data-testid="view-t3000">T3000 Explorer</button>
        <button className={`seg-b ${view === "history" ? "on" : ""}`} onClick={() => setView("history")} data-testid="view-history">History</button>
      </nav>
      {view === "history" ? <History hass={hass} role={boot.role} /> : view === "sheets" ? <ReactFlowProvider><Editor key={openSheet || "editor"} hass={hass} boot={boot} initialSheet={openSheet} /></ReactFlowProvider> : <div className="app"><Explorer hass={hass} role={boot.role} onOpenSheet={(id) => { setOpenSheet(id); setView("sheets"); }} /></div>}
    </div>
  );
}
