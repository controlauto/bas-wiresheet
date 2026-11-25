// Controller faceplates for T3000 program sheets: the controller's physical inputs and outputs drawn as terminal
// strips (label, live value, status light, hand/auto switch), its ports and status lights. Wires attach to terminals:
// a wire from an input terminal reads that input, a wire into an output terminal writes that output.
import React, { useEffect, useState } from "react";
import { Handle, Position } from "@xyflow/react";

export const PANEL_TYPES = { t3_panel_in: "IN", t3_panel_out: "OUT" };
const DEFAULT_MODEL = "T3-BB";

export function panelModel(block, device, catalog) {
  const models = catalog.t3_models || {};
  const want = block.params && block.params.model;
  const name = want && want !== "Auto" ? want : (device && device.model) || DEFAULT_MODEL;
  return { name, auto: !want || want === "Auto", known: !!(device && device.model), ...(models[name] || models[DEFAULT_MODEL]) };
}

/* Terminals of one faceplate, in order. Outputs: digital first, then analog. */
export function panelPins(kind, model) {
  if (kind === "IN") return Array.from({ length: model.inputs || 0 }, (_, i) => ({ pin: `IN${i + 1}`, n: i + 1, io: "in" }));
  const out = [];
  for (let n = model.out_digital[0]; n <= model.out_digital[1]; n++) out.push({ pin: `OUT${n}`, n, io: "do" });
  for (let n = model.out_analog[0]; n <= model.out_analog[1]; n++) out.push({ pin: `OUT${n}`, n, io: "ao" });
  return out;
}

const own = (t, pin) => (t && String(t).trim() && String(t).trim().toUpperCase() !== pin ? String(t).trim() : null);

function rowFor(points, pin) {
  return (points || []).find((x) => `${x.category}${x.index}` === pin) || null;
}

/* Controller point table row (t3_point_table) for a terminal. */
export function tableRow(table, pin) {
  const m = /^(IN|OUT)(\d+)$/.exec(pin);
  return m && table && table[m[1]] ? table[m[1]].find((r) => r.index === Number(m[2])) || null : null;
}

/* Value shown in the terminal's display: controller value (Live / Edit) or the simulated value. */
function pinValue(c, id, pin, row, io) {
  const vid = `${id}__${pin}`;
  if (c.mode === "Simulation" && c.snapshot) {
    const b = c.snapshot.blocks && c.snapshot.blocks[vid];
    const d = c.snapshot.displays && c.snapshot.displays[vid];
    const x = (b && b.outputs && b.outputs.value) || (d && d.value) || null;
    if (!x) return { text: "----", cls: "idle" };
    if (x.q !== "good") return { text: "N/A", cls: "bad" };
    if (typeof x.v === "boolean") return { text: x.v ? "ON" : "OFF", cls: x.v ? "on" : "off", on: x.v };
    return { text: `${Number(Number(x.v).toFixed(1))}`, cls: "num", on: Number(x.v) !== 0 };
  }
  if (!row) return { text: "----", cls: "idle" };
  if (row.fromTable) {
    if (row.digital_analog === "digital") return { text: row.value ? "ON" : "OFF", cls: row.value ? "on" : "off", on: !!row.value };
    return { text: `${Number(Number(row.value).toFixed(1))}${row.units ? ` ${row.units}` : ""}`.slice(0, 11), cls: "num", on: io === "do" ? row.value !== 0 : null };
  }
  if (row.freshness === "Unavailable") return { text: "N/A", cls: "bad" };
  const v = row.value;
  if (v === "on" || v === "off") return { text: v.toUpperCase(), cls: v, on: v === "on", stale: row.freshness === "Stale" };
  const n = Number(v);
  if (!Number.isFinite(n)) return { text: String(v).slice(0, 7), cls: "txt" };
  const u = row.units ? ` ${String(row.units).replace("percent", "%").replace("degrees-fahrenheit", "°F").replace("degrees-celsius", "°C")}` : "";
  return { text: `${Math.abs(n) >= 1000 ? n.toFixed(0) : Number(n.toFixed(1))}${u}`.slice(0, 11), cls: "num", on: io === "do" ? n !== 0 : null, stale: row.freshness === "Stale" };
}

function Screws() {
  return <span className="fp-screws" aria-hidden="true"><i /><i /></span>;
}

export function Faceplate({ id, block, selected, c }) {
  const kind = PANEL_TYPES[block.type];
  const model = panelModel(block, c.t3Device, c.catalog);
  const used = (c.usedPins && c.usedPins[id]) || new Set();
  const wiredOnly = block.params && block.params.show === "Wired only";
  const pins = panelPins(kind, model).filter((p) => !wiredOnly || used.has(p.pin));
  const issues = c.issuesByBlock[id] || [];
  const badPins = new Set(issues.map((i) => i.port).filter(Boolean));
  const types = (block.params && block.params.pins) || {};
  const live = c.mode === "Live Monitoring";
  const devName = c.t3Device ? c.t3Device.name : null;
  const anyFresh = (c.t3Points || []).some((r) => r.freshness === "Fresh");
  const lastOut = model.out_analog ? model.out_analog[1] : 0;
  const title = kind === "IN" ? `INPUTS 1-${model.inputs}` : `OUTPUTS 1-${lastOut}`;
  return (
    <div className={`fp fp-${kind.toLowerCase()} ${selected ? "sel" : ""} ${issues.some((i) => i.severity === "error") ? "err" : ""}`} data-testid={`block-${id}`}>
      <div className="fp-top">
        <span className="fp-tab">{title}</span>
        <span className="fp-id">{devName || "Controller"}{c.t3Instance ? ` · ${c.t3Instance}` : ""}</span>
      </div>
      <div className="fp-rows">
        {pins.map((p) => {
          const tr = tableRow(c.t3Table, p.pin);
          const row = rowFor(c.t3Points, p.pin) || (tr ? { ...tr, fromTable: true } : null);
          const label = (tr && (own(tr.label, p.pin) || own(tr.description, p.pin))) || (row && (own(row.label, p.pin) || own(row.description, p.pin)))
            || (kind === "IN" ? "" : p.io === "do" ? "Digital" : "Analog");
          const val = pinValue(c, id, p.pin, row, p.io);
          const manual = (tr && tr.auto_manual === "manual") || (row && row.auto_manual === "Manual");
          const hw = tr && tr.hw_switch;
          const picked = c.selectedPin && c.selectedPin.id === id && c.selectedPin.pin === p.pin;
          const t = types[p.pin];
          const color = t && c.catalog.port_types[t] ? c.catalog.port_types[t].color : "#8b97a8";
          const wired = used.has(p.pin);
          const cls = `fp-row ${wired ? "wired" : ""} ${badPins.has(p.pin) ? "bad" : ""} ${val.stale ? "stale" : ""} ${picked ? "picked" : ""} ${tr && tr.decommissioned ? "decom" : ""}`;
          const tip = `${p.pin}${label ? ` ${label}` : ""}${tr ? ` · ${tr.range_name}` : ""}${manual ? " · Manual" : ""}${hw && hw !== "auto" ? ` · switch ${hw.toUpperCase()}` : ""}${t ? ` · wired as ${t}` : ""} · click to configure`;
          if (kind === "IN") {
            return (
              <div key={p.pin} className={cls} title={tip} data-pin={p.pin} data-testid={`pin-${id}-${p.pin}`} onClick={(e) => { if (!(e.target.closest && e.target.closest(".react-flow__handle")) && c.pickPin) c.pickPin(id, p.pin); }}>
                <Screws />
                <span className={`fp-led blue ${val.on ? "lit" : ""}`} />
                <span className="fp-strip nodrag"><b className="fp-n">{p.n}</b><span className="fp-lbl">{label || p.pin}</span></span>
                <span className={`fp-lcd nodrag ${val.cls}`}>{val.text}</span>
                <Handle type="source" position={Position.Right} id={p.pin} className="fp-h" style={{ background: wired ? color : "#3a4a5f" }} />
              </div>
            );
          }
          return (
            <div key={p.pin} className={cls} title={tip} data-pin={p.pin} data-testid={`pin-${id}-${p.pin}`} onClick={(e) => { if (!(e.target.closest && e.target.closest(".react-flow__handle")) && c.pickPin) c.pickPin(id, p.pin); }}>
              <Handle type="target" position={Position.Left} id={p.pin} className="fp-h" style={{ background: wired ? color : "#3a4a5f" }} />
              <span className="fp-strip nodrag"><b className="fp-n">{p.n}</b><span className="fp-lbl">{label}</span></span>
              <span className={`fp-lcd nodrag ${val.cls}`}>{val.text}</span>
              <span className={`fp-hoa ${manual || hw === "hand" ? "hand" : ""} ${hw === "off" ? "off" : ""}`} title={`${manual ? "Manual (software)" : "Auto"}${hw ? ` · hardware switch ${hw.toUpperCase()}` : ""}`}><i /></span>
              <span className={`fp-led ${p.io === "do" ? "red" : "blue"} ${val.on ? "lit" : ""}`} />
              <Screws />
            </div>
          );
        })}
        {!pins.length && <div className="fp-empty">{wiredOnly ? "No wired terminals" : "No terminals for this model"}</div>}
      </div>
      {kind === "OUT" && (
        <div className="fp-ports" data-testid={`ports-${id}`}>
          {(c.t3Settings && c.t3Settings.ports ? [{ port: "ETH", mode: c.t3Settings.ip || "Ethernet", baud: null, eth: true }, ...c.t3Settings.ports] : [{ port: "ETH", mode: "Ethernet", eth: true }]).map((pt) => (
            <div key={pt.port} className="fp-port">
              <span className="fp-pname">{pt.port}</span>
              <span className="fp-pmode">{pt.mode}{pt.baud ? ` · ${pt.baud}` : ""}</span>
              <span className={`fp-jack ${pt.eth ? "rj" : ""}`} />
              <span className={`fp-led green ${pt.eth || (pt.mode && pt.mode !== "Unused") ? "lit" : ""}`} title={pt.eth ? "Network" : pt.mode} />
            </div>
          ))}
          {c.t3SettingsError && <div className="fp-empty">Ports: {c.t3SettingsError}</div>}
        </div>
      )}
      <div className="fp-foot">
        <span className={`fp-led green ${c.t3Points && c.t3Points.length ? "lit" : ""}`} /> <span>POWER</span>
        <span className={`fp-led red ${live && anyFresh ? "lit blink" : ""}`} /> <span>HEARTBEAT</span>
        <span className="grow" />
        <span className="fp-model" data-testid={`model-${id}`}>{model.name}{model.auto ? (model.known ? "" : " (default)") : ""} · {model.inputs} IN / {model.out_digital[1] - model.out_digital[0] + 1} DO / {model.out_analog[1] - model.out_analog[0] + 1} AO</span>
      </div>
      {kind === "OUT" && <div className="fp-brand">T3000<small>CONTROLLER</small></div>}
      {issues.length > 0 && <div className="blk-issue">{issues[0].message}</div>}
    </div>
  );
}

/* ======================= terminal inspector ======================= */
const fmtCal = (v) => (v === undefined || v === null ? "" : `${v > 0 ? "+" : ""}${v}`);

export function TerminalPanel({ ws, c, node, pin, edges, canEdit, onPinType, onReloadTable, TrendChart }) {
  const kind = /^IN/.test(pin) ? "IN" : "OUT";
  const index = Number(pin.replace(/^\D+/, ""));
  const model = panelModel(node.data.block, c.t3Device, c.catalog);
  const io = kind === "IN" ? "in" : index >= model.out_digital[0] && index <= model.out_digital[1] ? "do" : "ao";
  const haRow = rowFor(c.t3Points, pin);
  const [cfg, setCfg] = useState(null);
  const [edit, setEdit] = useState({});
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [done, setDone] = useState(null);
  const instance = c.t3Instance;
  const load = () => ws({ type: "bas_wiresheet/t3_point_config", instance, category: kind, index })
    .then((r) => {
      setCfg(r); setErr(null);
      setEdit({ label: r.label, description: r.description, digital_analog: r.digital_analog, range: r.range, auto_manual: r.auto_manual,
        ...(kind === "IN" ? { calibration: r.calibration } : { value: r.digital_analog === "digital" ? (r.value ? 1 : 0) : r.value }) });
      return r;
    }).catch((e) => setErr(e.message));
  useEffect(() => { setCfg(null); setDone(null); setTyped(""); if (instance) load(); else setErr("Set the controller (BACnet instance) of this sheet first"); }, [pin, instance]); // eslint-disable-line
  const changes = cfg ? Object.fromEntries(Object.entries(edit).filter(([k, v]) => {
    if (k === "value") return edit.auto_manual === "manual" && String(v) !== String(cfg.digital_analog === "digital" ? (cfg.value ? 1 : 0) : cfg.value);
    return String(v) !== String(cfg[k]);
  })) : {};
  const save = async () => {
    setBusy(true); setErr(null); setDone(null);
    try {
      const r = await ws({ type: "bas_wiresheet/t3_point_config_write", instance, category: kind, index, changes, confirm: typed });
      setDone(r.changed ? "Saved and verified on the controller." : "No change."); setTyped(""); await load(); onReloadTable();
    } catch (e) { setErr(e.message); }
    setBusy(false);
  };
  const ranges = cfg ? (edit.digital_analog === "analog" ? cfg.units_analog : cfg.units_digital) : [];
  const wires = edges.filter((e) => (e.source === node.id && e.sourceHandle === pin) || (e.target === node.id && e.targetHandle === pin));
  const other = (e) => { const n = c.nodeById[e.source === node.id ? e.target : e.source]; return n ? (n.data.block.label || n.id) : e.source; };
  const ptype = ((node.data.block.params || {}).pins || {})[pin] || "number";
  const field = (label, el, hint) => <label className="field"><span>{label}</span>{el}{hint && <small>{hint}</small>}</label>;
  const dis = !canEdit || busy;
  return (
    <div className="insp term" data-testid="terminal-panel">
      <h3>{pin} <span className="muted small">{kind === "IN" ? "Input" : io === "do" ? "Digital output" : "Analog output"}</span></h3>
      {cfg && <div className="term-now">
        <span className="fp-lcd num" data-testid="term-value">{cfg.digital_analog === "digital" ? (cfg.value ? "ON" : "OFF") : `${Number(cfg.value.toFixed(2))}${cfg.units ? ` ${cfg.units}` : ""}`}</span>
        <span className={`chip ${cfg.auto_manual === "manual" ? "bad" : "ok"}`}>{cfg.auto_manual === "manual" ? "MANUAL" : "AUTO"}</span>
        {cfg.hw_switch && <span className={`chip ${cfg.hw_switch === "auto" ? "ok" : "bad"}`} data-testid="term-hw" title="Hand/Off/Auto switch on the controller">Switch {cfg.hw_switch.toUpperCase()}</span>}
        {cfg.decommissioned && <span className="chip bad">Decommissioned</span>}
      </div>}
      {!cfg && !err && <div className="muted small">Reading {pin} from the controller…</div>}
      {err && <div className="errtxt small" data-testid="term-error">{err}</div>}
      {cfg && <>
        {field(`Label (max ${cfg.label_max})`, <input className="mono" maxLength={cfg.label_max} value={edit.label} disabled={dis} onChange={(e) => setEdit({ ...edit, label: e.target.value.toUpperCase() })} data-testid="term-label" />)}
        {field(`Description (max ${cfg.description_max})`, <input maxLength={cfg.description_max} value={edit.description} disabled={dis} onChange={(e) => setEdit({ ...edit, description: e.target.value })} data-testid="term-description" />)}
        <div className="row" style={{ gap: 8 }}>
          {field("Signal", <select value={edit.digital_analog} disabled={dis} onChange={(e) => setEdit({ ...edit, digital_analog: e.target.value, range: 0 })} data-testid="term-da"><option value="analog">Analog</option><option value="digital">Digital</option></select>)}
          {field(kind === "IN" ? "Sensor / Range" : "Range", <select value={edit.range} disabled={dis} onChange={(e) => setEdit({ ...edit, range: Number(e.target.value) })} data-testid="term-range">
            {ranges.map((u, i) => (u ? <option key={i} value={i}>{u}</option> : null))}
            {edit.range >= ranges.length && <option value={edit.range}>custom {edit.range}</option>}
          </select>)}
        </div>
        {kind === "IN" && edit.digital_analog === "analog" && field("Calibration offset", <input type="number" step="0.1" value={edit.calibration ?? 0} disabled={dis} onChange={(e) => setEdit({ ...edit, calibration: e.target.value === "" ? 0 : Number(e.target.value) })} data-testid="term-cal" />,
          `Added to the reading (now ${fmtCal(cfg.calibration) || "0"}). Filter ${cfg.filter}.`)}
        <div className="row" style={{ gap: 8 }}>
          {field("Auto / Manual", <select value={edit.auto_manual} disabled={dis} onChange={(e) => setEdit({ ...edit, auto_manual: e.target.value })} data-testid="term-am"><option value="auto">Auto</option><option value="manual">Manual</option></select>)}
          {kind === "OUT" && edit.auto_manual === "manual" && (edit.digital_analog === "digital"
            ? field("Manual value", <select value={edit.value} disabled={dis} onChange={(e) => setEdit({ ...edit, value: Number(e.target.value) })} data-testid="term-value-in"><option value={1}>ON</option><option value={0}>OFF</option></select>)
            : field(`Manual value${cfg.units ? ` (${cfg.units})` : ""}`, <input type="number" step="any" value={edit.value} disabled={dis} onChange={(e) => setEdit({ ...edit, value: e.target.value === "" ? 0 : Number(e.target.value) })} data-testid="term-value-in" />))}
        </div>
        {canEdit && <div className="row" style={{ gap: 8, alignItems: "flex-end" }}>
          {field(`Type ${pin} to confirm`, <input className="mono" style={{ width: 90 }} value={typed} onChange={(e) => setTyped(e.target.value)} data-testid="term-confirm" />)}
          <button className="btn primary" disabled={busy || !Object.keys(changes).length || typed.trim().toUpperCase() !== pin} onClick={save} data-testid="term-save">Save to Controller</button>
        </div>}
        {!canEdit && <div className="muted small">View only: editors with full access to this controller can change the point.</div>}
        {done && <div className="ok-txt small" data-testid="term-done">{done}</div>}
        {Object.keys(changes).length > 0 && canEdit && <div className="muted small">Changes: {Object.keys(changes).join(", ")}. Written to the controller now and read back (not part of the sheet).</div>}
        {edit.auto_manual === "manual" && kind === "OUT" && <div className="warn-txt small">In Manual the controller program no longer drives {pin}: the output stays at the manual value until set back to Auto.</div>}
      </>}
      <div className="pane-h sub">Wiring</div>
      {wires.length ? wires.map((e) => <div key={e.id} className="kv"><span>{e.source === node.id ? "Feeds" : "Driven by"}</span><b>{other(e)}</b></div>)
        : <div className="muted small">Not wired. Drag from the terminal to a block to use it in the program.</div>}
      {wires.length > 0 && <label className="field"><span>Signal in the program</span>
        <select value={ptype} disabled={!canEdit} onChange={(e) => onPinType(e.target.value)} data-testid="term-ptype"><option value="number">Number</option><option value="boolean">Boolean (ON/OFF)</option></select></label>}
      {haRow && haRow.entity_id && TrendChart && <><div className="pane-h sub">Trend</div><TrendChart key={haRow.entity_id} ws={ws} entityId={haRow.entity_id} title={pin} /></>}
    </div>
  );
}
