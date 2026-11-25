// Explorer tabs for T3000 schedules (weekly and annual routines) and PID controllers.
import { typedOk } from "./confirm.js";
import React, { useEffect, useState } from "react";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const toMin = (t) => { if (!t) return null; const [h, m] = t.split(":").map(Number); return h * 60 + m; };

function DayBar({ pairs }) {
  // 24 h bar: ON from each ON time to its OFF time (or to midnight when the OFF time is empty)
  const segs = (pairs || []).map((p) => [toMin(p.on), toMin(p.off)]).filter(([a]) => a !== null)
    .map(([a, b]) => [a, b === null || b < a ? 1440 : b]);
  return (
    <div className="sch-bar" title={segs.map(([a, b]) => `${String(Math.floor(a / 60)).padStart(2, "0")}:${String(a % 60).padStart(2, "0")}–${b === 1440 ? "24:00" : `${String(Math.floor(b / 60)).padStart(2, "0")}:${String(b % 60).padStart(2, "0")}`}`).join(", ") || "Off all day"}>
      {segs.map(([a, b], i) => <span key={i} style={{ left: `${(a / 1440) * 100}%`, width: `${((b - a) / 1440) * 100}%` }} />)}
      {[6, 12, 18].map((h) => <i key={h} style={{ left: `${(h / 24) * 100}%` }} />)}
    </div>
  );
}

function WeeklyEditor({ ws, instance, index, canWrite, onSaved }) {
  const [rec, setRec] = useState(null);
  const [days, setDays] = useState(null);
  const [meta, setMeta] = useState({});
  const [typed, setTyped] = useState("");
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const name = `WR${index}`;
  const load = () => ws({ type: "bas_wiresheet/t3_schedule_get", instance, kind: "weekly", index })
    .then((r) => { setRec(r); setDays(r.days.map((d) => d.map((p) => ({ on: p.on || "", off: p.off || "" })))); setMeta({ description: r.description, label: r.label, auto_manual: r.auto_manual, value: r.value }); })
    .catch((e) => setErr(e.message));
  useEffect(() => { setRec(null); load(); }, [index]); // eslint-disable-line
  if (err && !rec) return <div className="errtxt pad">{err}</div>;
  if (!rec) return <div className="muted pad">Reading {name} from the controller…</div>;
  const set = (j, k, which, v) => setDays(days.map((d, dj) => (dj !== j ? d : d.map((p, pk) => (pk !== k ? p : { ...p, [which]: v })))));
  const copyMon = () => setDays(days.map((d, j) => (j >= 1 && j <= 4 ? days[0].map((p) => ({ ...p })) : d)));
  const save = async () => {
    setBusy(true); setErr(null); setMsg(null);
    const changes = { days: days.map((d) => d.map((p) => ({ on: p.on || null, off: p.off || null }))) };
    ["description", "label", "auto_manual", "value"].forEach((k) => { if (String(meta[k]) !== String(rec[k])) changes[k] = meta[k]; });
    try { await ws({ type: "bas_wiresheet/t3_schedule_set", instance, kind: "weekly", index, changes, confirm: typed }); setMsg("Saved and verified on the controller."); setTyped(""); await load(); onSaved(); }
    catch (e) { setErr(e.message); }
    setBusy(false);
  };
  return (
    <div className="sch-editor" data-testid={`weekly-editor-${index}`}>
      <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
        <b className="mono">{name}</b>
        <label className="field inl"><span>Description</span><input maxLength={20} disabled={!canWrite} value={meta.description} onChange={(e) => setMeta({ ...meta, description: e.target.value })} /></label>
        <label className="field inl"><span>Label</span><input className="mono" maxLength={8} disabled={!canWrite} value={meta.label} onChange={(e) => setMeta({ ...meta, label: e.target.value.toUpperCase() })} data-testid="wr-label" /></label>
        <label className="field inl"><span>Auto / Manual</span><select disabled={!canWrite} value={meta.auto_manual} onChange={(e) => setMeta({ ...meta, auto_manual: e.target.value })}><option value="auto">Auto</option><option value="manual">Manual</option></select></label>
        {meta.auto_manual === "manual" && <label className="field inl"><span>Manual value</span><select disabled={!canWrite} value={meta.value} onChange={(e) => setMeta({ ...meta, value: e.target.value })}><option value="on">ON</option><option value="off">OFF</option></select></label>}
        <span className={`chip ${rec.value === "on" ? "ok" : "unk"}`}>Now {rec.value.toUpperCase()}</span>
        {!rec.flags_supported && <span className="chip dq" title="Older firmware: an entry at 00:00 counts as empty">No flag table</span>}
      </div>
      <table className="points sch-table">
        <thead><tr><th>Day</th><th>Timeline 00–24 h</th>{[1, 2, 3, 4].map((k) => <th key={k} colSpan={2}>ON {k} / OFF {k}</th>)}</tr></thead>
        <tbody>
          {rec.days_names.map((dname, j) => (
            <tr key={dname}>
              <td>{dname}</td>
              <td style={{ minWidth: 220 }}><DayBar pairs={days[j].map((p) => ({ on: p.on || null, off: p.off || null }))} /></td>
              {days[j].map((p, k) => (
                <React.Fragment key={k}>
                  <td><input type="time" className="mono" disabled={!canWrite} value={p.on} onChange={(e) => set(j, k, "on", e.target.value)} data-testid={`wr-${j}-${k}-on`} /></td>
                  <td><input type="time" className="mono" disabled={!canWrite} value={p.off} onChange={(e) => set(j, k, "off", e.target.value)} data-testid={`wr-${j}-${k}-off`} /></td>
                </React.Fragment>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {canWrite && <div className="row" style={{ gap: 8, alignItems: "flex-end", marginTop: 6 }}>
        <button className="btn sm" onClick={copyMon}>Copy Monday to Tue–Fri</button>
        <label className="field inl"><span>Type {name} to confirm</span><input className="mono" style={{ width: 80 }} data-confirm="1" value={typed} onChange={(e) => setTyped(e.target.value)} data-testid="wr-confirm" /></label>
        <button className="btn primary" disabled={busy || !typedOk(typed, name)} onClick={save} data-testid="wr-save">Save to Controller</button>
      </div>}
      {busy && <div className="muted">Writing and reading back…</div>}
      {msg && <div className="ok-txt small" data-testid="wr-done">{msg}</div>}
      {err && <div className="errtxt small">{err}</div>}
    </div>
  );
}

function AnnualEditor({ ws, instance, index, canWrite, onSaved }) {
  const [rec, setRec] = useState(null);
  const [days, setDays] = useState(new Set());
  const [meta, setMeta] = useState({});
  const [typed, setTyped] = useState("");
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);
  const name = `AR${index}`;
  const load = () => ws({ type: "bas_wiresheet/t3_schedule_get", instance, kind: "annual", index })
    .then((r) => { setRec(r); setDays(new Set(r.days)); setMeta({ description: r.description, label: r.label }); }).catch((e) => setErr(e.message));
  useEffect(() => { setRec(null); load(); }, [index]); // eslint-disable-line
  if (err && !rec) return <div className="errtxt pad">{err}</div>;
  if (!rec) return <div className="muted pad">Reading {name} from the controller…</div>;
  const toggle = (k) => { if (!canWrite) return; const n = new Set(days); if (n.has(k)) n.delete(k); else n.add(k); setDays(n); };
  const save = async () => {
    setErr(null); setMsg(null);
    const changes = { days: [...days].sort() };
    ["description", "label"].forEach((k) => { if (meta[k] !== rec[k]) changes[k] = meta[k]; });
    try { await ws({ type: "bas_wiresheet/t3_schedule_set", instance, kind: "annual", index, changes, confirm: typed }); setMsg("Saved and verified on the controller."); setTyped(""); await load(); onSaved(); }
    catch (e) { setErr(e.message); }
  };
  return (
    <div className="sch-editor" data-testid={`annual-editor-${index}`}>
      <div className="row" style={{ gap: 8, alignItems: "flex-end", flexWrap: "wrap" }}>
        <b className="mono">{name}</b>
        <label className="field inl"><span>Description</span><input maxLength={20} disabled={!canWrite} value={meta.description} onChange={(e) => setMeta({ ...meta, description: e.target.value })} /></label>
        <label className="field inl"><span>Label</span><input className="mono" maxLength={8} disabled={!canWrite} value={meta.label} onChange={(e) => setMeta({ ...meta, label: e.target.value.toUpperCase() })} /></label>
        <span className="muted small">{days.size} holiday(s) in {rec.year}. Click a day to mark or clear it.</span>
      </div>
      <div className="cal">
        {MONTHS.map((mn, m) => {
          const n = new Date(rec.year, m + 1, 0).getDate(), first = (new Date(rec.year, m, 1).getDay() + 6) % 7;
          return (
            <div key={mn} className="cal-m"><div className="cal-h">{mn}</div>
              <div className="cal-g">{["M", "T", "W", "T", "F", "S", "S"].map((d, i) => <i key={i}>{d}</i>)}
                {Array.from({ length: first }).map((_, i) => <span key={`e${i}`} />)}
                {Array.from({ length: n }).map((_, d) => { const k = `${String(m + 1).padStart(2, "0")}-${String(d + 1).padStart(2, "0")}`; return <b key={k} className={days.has(k) ? "on" : ""} onClick={() => toggle(k)} data-testid={`ar-${k}`}>{d + 1}</b>; })}
              </div>
            </div>
          );
        })}
      </div>
      {canWrite && <div className="row" style={{ gap: 8, alignItems: "flex-end", marginTop: 6 }}>
        <label className="field inl"><span>Type {name} to confirm</span><input className="mono" style={{ width: 80 }} data-confirm="1" value={typed} onChange={(e) => setTyped(e.target.value)} data-testid="ar-confirm" /></label>
        <button className="btn primary" disabled={!typedOk(typed, name)} onClick={save} data-testid="ar-save">Save to Controller</button>
      </div>}
      {msg && <div className="ok-txt small" data-testid="ar-done">{msg}</div>}
      {err && <div className="errtxt small">{err}</div>}
    </div>
  );
}

export function Schedules({ ws, device, canWrite }) {
  const [list, setList] = useState(null);
  const [err, setErr] = useState(null);
  const [open, setOpen] = useState(null);
  const load = () => ws({ type: "bas_wiresheet/t3_schedules", instance: device.instance }).then(setList).catch((e) => setErr(e.message));
  useEffect(() => { setList(null); setOpen(null); load(); }, [device.instance]); // eslint-disable-line
  if (err && !list) return <div className="errtxt pad">{err}</div>;
  if (!list) return <div className="muted pad">Reading schedules from the controller…</div>;
  return (
    <div className="pad xp-scroll" data-testid="t3-schedules">
      <h3 style={{ marginTop: 0 }}>Weekly Routines</h3>
      <table className="points"><thead><tr><th>#</th><th>Label</th><th>Description</th><th>State</th><th>Auto/Man</th><th /></tr></thead>
        <tbody>{list.weekly.map((w) => (
          <tr key={w.index} className={open && open.kind === "weekly" && open.index === w.index ? "sel-row" : ""}>
            <td className="mono">WR{w.index}</td><td className="mono">{w.label || "—"}</td><td>{w.description || "—"}</td>
            <td><span className={`chip ${w.value === "on" ? "ok" : "unk"}`}>{w.value.toUpperCase()}</span></td><td>{w.auto_manual === "manual" ? <span className="chip dq">Manual</span> : "Auto"}</td>
            <td><button className="btn sm" onClick={() => setOpen({ kind: "weekly", index: w.index })} data-testid={`wr-open-${w.index}`}>{canWrite ? "Edit" : "View"}</button></td>
          </tr>))}</tbody></table>
      {open && open.kind === "weekly" && <WeeklyEditor ws={ws} instance={device.instance} index={open.index} canWrite={canWrite} onSaved={load} />}
      <h3>Annual Routines (Holidays)</h3>
      <table className="points"><thead><tr><th>#</th><th>Label</th><th>Description</th><th>State</th><th /></tr></thead>
        <tbody>{list.annual.map((a) => (
          <tr key={a.index}><td className="mono">AR{a.index}</td><td className="mono">{a.label || "—"}</td><td>{a.description || "—"}</td>
            <td><span className={`chip ${a.value === "on" ? "ok" : "unk"}`}>{a.value.toUpperCase()}</span></td>
            <td><button className="btn sm" onClick={() => setOpen({ kind: "annual", index: a.index })} data-testid={`ar-open-${a.index}`}>{canWrite ? "Edit" : "View"}</button></td></tr>))}</tbody></table>
      {open && open.kind === "annual" && <AnnualEditor ws={ws} instance={device.instance} index={open.index} canWrite={canWrite} onSaved={load} />}
      <p className="muted small">Weekly routines switch ON at each ON time and OFF at the next OFF time; Holiday 1 and Holiday 2 apply on the days marked in the annual routines linked to them in T3000. Every change is read back and recorded in History.</p>
    </div>
  );
}

function PidRow({ ws, instance, loop, canWrite, onSaved }) {
  const [edit, setEdit] = useState(null);
  const [typed, setTyped] = useState("");
  const [err, setErr] = useState(null);
  const [msg, setMsg] = useState(null);
  const name = `PID${loop.index}`;
  const keys = ["auto_manual", "output", "setpoint_value", "action", "proportional", "reset", "i_time_unit", "rate", "bias"];
  const start = () => { setEdit(Object.fromEntries(keys.map((k) => [k, loop[k]]))); setMsg(null); setErr(null); };
  const save = async () => {
    const changes = Object.fromEntries(keys.filter((k) => String(edit[k]) !== String(loop[k])).map((k) => [k, ["output", "setpoint_value", "proportional", "reset", "rate", "bias"].includes(k) ? Number(edit[k]) : edit[k]]));
    if (!Object.keys(changes).length) { setErr("Nothing changed"); return; }
    try { await ws({ type: "bas_wiresheet/t3_pid_set", instance, index: loop.index, changes, confirm: typed }); setEdit(null); setTyped(""); await onSaved(); setMsg("Saved and verified."); }
    catch (e) { setErr(e.message); }
  };
  return (<>
    <tr className={loop.used ? "" : "muted"}>
      <td className="mono">{name}</td><td className="mono">{loop.input || "—"}</td><td className="mono">{loop.used ? loop.input_value : "—"}</td>
      <td className="mono">{loop.setpoint || "value"} · {loop.setpoint_value}</td><td className="mono">{loop.output.toFixed(1)} %</td>
      <td>{loop.auto_manual === "manual" ? <span className="chip dq">Manual</span> : "Auto"}</td><td className="mono">{loop.action}</td>
      <td className="mono">{loop.proportional}</td><td className="mono">{loop.reset}/{loop.i_time_unit}</td><td className="mono">{loop.rate.toFixed(2)}</td><td className="mono">{loop.bias}</td>
      <td>{canWrite && <button className="btn sm" onClick={() => (edit ? setEdit(null) : start())} data-testid={`pid-edit-${loop.index}`}>{edit ? "Close" : "Tune"}</button>}{msg && <span className="ok-txt small"> {msg}</span>}</td>
    </tr>
    {edit && <tr><td colSpan={12}><div className="xp-config"><div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
      <label className="field inl"><span>Auto / Manual</span><select value={edit.auto_manual} onChange={(e) => setEdit({ ...edit, auto_manual: e.target.value })} data-testid="pid-am"><option value="auto">Auto</option><option value="manual">Manual</option></select></label>
      {edit.auto_manual === "manual" && <label className="field inl"><span>Output %</span><input type="number" min="0" max="100" step="0.1" value={edit.output} onChange={(e) => setEdit({ ...edit, output: e.target.value })} /></label>}
      <label className="field inl"><span>Set value</span><input type="number" step="0.1" value={edit.setpoint_value} onChange={(e) => setEdit({ ...edit, setpoint_value: e.target.value })} title={loop.setpoint ? `Linked to ${loop.setpoint}: the controller may overwrite it` : ""} /></label>
      <label className="field inl"><span>Action</span><select value={edit.action} onChange={(e) => setEdit({ ...edit, action: e.target.value })}><option value="-">- (heating: output up when below SP)</option><option value="+">+ (cooling: output up when above SP)</option></select></label>
      <label className="field inl"><span>Prop (P)</span><input type="number" min="0" max="255" value={edit.proportional} onChange={(e) => setEdit({ ...edit, proportional: e.target.value })} data-testid="pid-p" /></label>
      <label className="field inl"><span>Reset (I)</span><input type="number" min="0" max="255" value={edit.reset} onChange={(e) => setEdit({ ...edit, reset: e.target.value })} /></label>
      <label className="field inl"><span>I per</span><select value={edit.i_time_unit} onChange={(e) => setEdit({ ...edit, i_time_unit: e.target.value })}><option value="hour">Hour</option><option value="min">Minute</option></select></label>
      <label className="field inl"><span>Rate (D)</span><input type="number" min="0" max="2" step="0.01" value={edit.rate} onChange={(e) => setEdit({ ...edit, rate: e.target.value })} /></label>
      <label className="field inl"><span>Bias %</span><input type="number" min="0" max="100" value={edit.bias} onChange={(e) => setEdit({ ...edit, bias: e.target.value })} /></label>
      <label className="field inl"><span>Type {name}</span><input className="mono" style={{ width: 80 }} data-confirm="1" value={typed} onChange={(e) => setTyped(e.target.value)} data-testid="pid-confirm" /></label>
      <button className="btn primary" disabled={!typedOk(typed, name)} onClick={save} data-testid="pid-save">Save to Controller</button>
    </div>{err && <div className="errtxt small">{err}</div>}</div></td></tr>}
  </>);
}

export function PidLoops({ ws, device, canWrite }) {
  const [loops, setLoops] = useState(null);
  const [err, setErr] = useState(null);
  const [all, setAll] = useState(false);
  const load = () => ws({ type: "bas_wiresheet/t3_pids", instance: device.instance }).then((r) => setLoops(r.loops)).catch((e) => setErr(e.message));
  useEffect(() => { setLoops(null); load(); }, [device.instance]); // eslint-disable-line
  if (err && !loops) return <div className="errtxt pad">{err}</div>;
  if (!loops) return <div className="muted pad">Reading PID controllers from the controller…</div>;
  const shown = loops.filter((l) => all || l.used);
  return (
    <div className="pad xp-scroll" data-testid="t3-pids">
      <div className="row" style={{ gap: 10, alignItems: "center" }}>
        <b>{loops.filter((l) => l.used).length} of 16 PID controllers configured</b>
        <label className="small"><input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} data-testid="pid-all" /> Show unused</label>
        <button className="btn sm" onClick={load}>Refresh</button>
      </div>
      <table className="points" style={{ marginTop: 6 }}>
        <thead><tr><th>#</th><th>Input</th><th>Value</th><th>Setpoint</th><th>Output</th><th>A/M</th><th>Action</th><th>P</th><th>I</th><th>D</th><th>Bias</th><th /></tr></thead>
        <tbody>{shown.map((l) => <PidRow key={l.index} ws={ws} instance={device.instance} loop={l} canWrite={canWrite} onSaved={load} />)}
          {!shown.length && <tr><td colSpan={12} className="muted">No PID controller is configured on this controller (control loops may be written in Control Basic programs instead). Tick Show unused to set one up.</td></tr>}</tbody>
      </table>
      <p className="muted small">T3000 PID controllers run on the controller. Prop is the proportional band, Reset the integral repeats per hour or minute, Rate the derivative (0.00–2.00), Bias the output offset. Changes are read back and recorded in History.</p>
    </div>
  );
}

/* ---------------- RS-485 ports (Modbus Master / Slave, MS/TP) ---------------- */
export function Ports({ ws, device, canWrite }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [edit, setEdit] = useState(null);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(null);
  const load = () => ws({ type: "bas_wiresheet/t3_settings", instance: device.instance }).then((r) => { setData(r); setErr(null); }).catch((e) => setErr(e.message));
  useEffect(() => { setData(null); load(); }, [device.instance]); // eslint-disable-line
  const save = async () => {
    setBusy(true); setErr(null); setDone(null);
    const cur = data.ports.find((p) => p.port === edit.port);
    const msg = { type: "bas_wiresheet/t3_port_set", instance: device.instance, port: edit.port, confirm: typed };
    if (Number(edit.mode) !== cur.mode_code) msg.mode = Number(edit.mode);
    if (Number(edit.baud) !== cur.baud) msg.baud = Number(edit.baud);
    try { const r = await ws(msg); setDone(r.changed ? `${edit.port} saved and verified on the controller.` : "No change."); setEdit(null); setTyped(""); await load(); }
    catch (e) { setErr(e.message); }
    setBusy(false);
  };
  return (
    <div className="pad xp-scroll" data-testid="t3-ports">
      <h3 style={{ margin: "0 0 4px" }}>RS-485 Ports</h3>
      <p className="muted small">Set a port to <b>Modbus Master</b> to read and write any Modbus RTU device on it (meters, drives, other brands) with the Modbus Read / Modbus Write blocks of a T3000 sheet. <b>Modbus Slave</b> lets another Modbus master read this controller. Read from the controller; changes are read back to verify.</p>
      {err && <div className="errtxt">{err}</div>}
      {!data && !err && <div className="muted">Reading the controller settings…</div>}
      {data && <>
        <div className="muted small">Panel <b className="mono" data-testid="t3-panel-number">{data.panel}</b> · {data.panel_name} · {data.ip} · Modbus points in programs are written <span className="mono">{data.panel}.&lt;slave id&gt;.MB_REG&lt;n&gt;</span></div>
        <table className="points" data-testid="t3-ports-table"><thead><tr><th>Port</th><th>Mode</th><th>Baud Rate</th><th /></tr></thead>
          <tbody>{data.ports.map((p) => (<React.Fragment key={p.port}>
            <tr><td className="mono">{p.port}</td>
              <td>{p.mode_code === 7 ? <span className="chip ok">{p.mode}</span> : p.mode_code === 2 ? <span className="chip unk">{p.mode}</span> : p.mode}</td>
              <td className="mono">{p.baud || "?"}</td>
              <td>{canWrite && <button className="btn sm" onClick={() => { setEdit(edit && edit.port === p.port ? null : { port: p.port, mode: p.mode_code, baud: p.baud }); setTyped(""); setDone(null); }} data-testid={`port-edit-${p.port}`}>Change</button>}</td></tr>
            {edit && edit.port === p.port && <tr><td colSpan={4}><div className="xp-config row" style={{ gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
              <label className="field" style={{ margin: 0 }}><span>Mode</span><select value={edit.mode} onChange={(e) => setEdit({ ...edit, mode: Number(e.target.value) })} data-testid="port-mode">
                {Object.entries(data.modes).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                {!(String(p.mode_code) in data.modes) && <option value={p.mode_code}>{p.mode}</option>}</select></label>
              <label className="field" style={{ margin: 0 }}><span>Baud Rate</span><select value={edit.baud} onChange={(e) => setEdit({ ...edit, baud: Number(e.target.value) })} data-testid="port-baud">
                {data.bauds.map((b) => <option key={b} value={b}>{b}</option>)}</select></label>
              <label className="field" style={{ margin: 0 }}><span>Type {p.port} to confirm</span><input className="mono" style={{ width: 80 }} data-confirm="1" value={typed} onChange={(e) => setTyped(e.target.value)} data-testid="port-confirm" /></label>
              <button className="btn primary" disabled={busy || !typedOk(typed, p.port) || (Number(edit.mode) === p.mode_code && Number(edit.baud) === p.baud)} onClick={save} data-testid="port-save">Save to Controller</button>
              <button className="btn" onClick={() => setEdit(null)}>Cancel</button>
              {(p.mode_code === 1 || p.mode_code === 9) && Number(edit.mode) !== p.mode_code && <div className="errtxt small" style={{ flexBasis: "100%" }}>This port runs BACnet MS/TP now: devices on it lose communication when the mode changes.</div>}
            </div></td></tr>}
          </React.Fragment>))}</tbody></table>
        {done && <div className="ok-txt small" data-testid="port-done">{done}</div>}
      </>}
    </div>
  );
}

const fmtWhen = (iso) => (iso ? new Date(iso).toLocaleString() : "—");

/* ---------------- controller alarm log ---------------- */
export function Alarms({ ws, device, canWrite }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const load = () => ws({ type: "bas_wiresheet/t3_alarms", instance: device.instance }).then((r) => { setData(r); setErr(null); }).catch((e) => setErr(e.message));
  useEffect(() => { setData(null); load(); const h = setInterval(load, 30000); return () => clearInterval(h); }, [device.instance]); // eslint-disable-line
  const act = async (index, action) => {
    setBusy(true); setErr(null);
    try { await ws({ type: "bas_wiresheet/t3_alarm_set", instance: device.instance, index, action }); await load(); } catch (e) { setErr(e.message); }
    setBusy(false);
  };
  const active = data ? data.alarms.filter((a) => a.active) : [];
  return (
    <div className="pad xp-scroll" data-testid="t3-alarms">
      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <h3 style={{ margin: 0 }}>Controller Alarms</h3>
        {data && <span className={`chip ${active.length ? "bad" : "ok"}`} data-testid="t3-alarm-count">{active.length ? `${active.length} active` : "No active alarms"}</span>}
        <button className="btn sm" onClick={load}>Read Again</button>
      </div>
      <p className="muted small">The controller's own alarm log (16 entries), raised by its firmware (for example open or short-circuited inputs) or by programs (ALARM statements). {data && (data.poll_s ? <>Home Assistant reads it every {data.poll_s} s, sends a notification for each new alarm and keeps <span className="mono">{data.sensor}</span> (number of active alarms).</> : "Background reading is off (alarm_poll_s: 0).")}</p>
      {err && <div className="errtxt">{err}</div>}
      {!data && !err && <div className="muted">Reading the alarm log…</div>}
      {data && (data.alarms.length ? <table className="points" data-testid="t3-alarm-table"><thead><tr><th>#</th><th>Message</th><th>Raised</th><th>Source</th><th>State</th><th /></tr></thead>
        <tbody>{data.alarms.map((a) => (
          <tr key={a.index} className={a.active && !a.acknowledged ? "alarm-row" : ""} data-testid={`t3-alarm-${a.index}`}>
            <td className="mono">{a.index}</td><td>{a.message}</td><td className="small">{fmtWhen(a.time)}</td>
            <td className="small">{a.source === "firmware" ? "Controller firmware" : a.program ? `Program ${a.program}` : "Program"}{a.point ? ` · ${a.point}` : ""}</td>
            <td>{a.active ? (a.acknowledged ? <span className="chip dq">Acknowledged</span> : <span className="chip bad">Active</span>) : a.restored ? <span className="chip ok">Restored</span> : <span className="muted">—</span>}</td>
            <td className="nowrap">{canWrite && <>
              {a.active && !a.acknowledged && <button className="btn sm" disabled={busy} onClick={() => act(a.index, "acknowledge")} data-testid={`t3-alarm-ack-${a.index}`}>Acknowledge</button>}
              <button className="btn sm" style={{ marginLeft: 4 }} disabled={busy} onClick={() => act(a.index, "delete")} data-testid={`t3-alarm-del-${a.index}`}>Delete</button></>}</td>
          </tr>))}</tbody></table>
        : <div className="muted pad">The alarm log is empty.</div>)}
    </div>
  );
}

/* ---------------- monitors (trend logs) ---------------- */
export function Monitors({ ws, device, TrendChart }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [open, setOpen] = useState(null);
  useEffect(() => { setData(null); ws({ type: "bas_wiresheet/t3_monitors", instance: device.instance }).then(setData).catch((e) => setErr(e.message)); }, [device.instance]); // eslint-disable-line
  const used = data ? data.monitors.filter((m) => m.configured) : [];
  const iv = (s) => (s >= 3600 ? `${(s / 3600).toFixed(s % 3600 ? 1 : 0)} h` : s >= 60 ? `${Math.round(s / 60)} min` : `${s} s`);
  return (
    <div className="pad xp-scroll" data-testid="t3-monitors">
      <h3 style={{ margin: "0 0 4px" }}>Trend Logs (Monitors)</h3>
      <p className="muted small">The controller's monitors (up to 12, 14 points each). Charts use Home Assistant's recorder history of the same points.</p>
      {err && <div className="errtxt">{err}</div>}
      {!data && !err && <div className="muted">Reading the monitors…</div>}
      {data && !used.length && <div className="muted pad" data-testid="t3-monitors-empty">No monitor is configured on this controller. The points' history is still available from Home Assistant: open a point in Inputs, Outputs or Variables, or use History.</div>}
      {used.length > 0 && <table className="points" data-testid="t3-monitor-table"><thead><tr><th>#</th><th>Label</th><th>Points</th><th>Interval</th><th>State</th><th /></tr></thead>
        <tbody>{used.map((m) => (<React.Fragment key={m.index}>
          <tr><td className="mono">AMON{m.index}</td><td>{m.label || "—"}</td><td className="mono small">{m.inputs.join(", ")}</td><td>{iv(m.interval_s)}</td>
            <td>{m.on ? <span className="chip ok">Logging</span> : <span className="chip unk">Off</span>}</td>
            <td><button className="btn sm" onClick={() => setOpen(open === m.index ? null : m.index)} data-testid={`t3-monitor-chart-${m.index}`}>{open === m.index ? "Hide Chart" : "Chart"}</button></td></tr>
          {open === m.index && <tr><td colSpan={6}>{Object.keys(m.entities).length
            ? Object.entries(m.entities).map(([p, ent]) => <div key={p}><div className="small mono">{p} · {ent}</div><TrendChart ws={ws} entityId={ent} title={p} /></div>)
            : <div className="muted small">None of these points has a Home Assistant entity to chart.</div>}</td></tr>}
        </React.Fragment>))}</tbody></table>}
    </div>
  );
}

/* ---------------- full controller backups ---------------- */
export function Backups({ ws, device, canWrite, canRestore }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [note, setNote] = useState("");
  const [job, setJob] = useState(null);
  const [cmp, setCmp] = useState({ before: "", after: "" });
  const [changes, setChanges] = useState(null);
  const [pick, setPick] = useState({});
  const [typed, setTyped] = useState("");
  const load = () => ws({ type: "bas_wiresheet/t3_snapshots", instance: device.instance }).then((r) => { setData(r); if (r.jobs.length && !job) follow(r.jobs[0].id); }).catch((e) => setErr(e.message));
  useEffect(() => { setData(null); setChanges(null); load(); }, [device.instance]); // eslint-disable-line
  const follow = (id) => {
    const tick = async () => {
      try {
        const j = await ws({ type: "bas_wiresheet/t3_job", job: id }); setJob(j);
        if (j.state === "running") setTimeout(tick, 1500); else load();
      } catch (e) { setErr(e.message); }
    };
    tick();
  };
  const take = async () => { setErr(null); try { const r = await ws({ type: "bas_wiresheet/t3_snapshot_take", instance: device.instance, note }); setNote(""); follow(r.job); } catch (e) { setErr(e.message); } };
  const compare = async (before, after) => {
    setErr(null); setChanges(null); setPick({}); setCmp({ before, after });
    try { setChanges(await ws({ type: "bas_wiresheet/t3_snapshot_compare", instance: device.instance, before, after })); } catch (e) { setErr(e.message); }
  };
  const download = async (id) => {
    const s = await ws({ type: "bas_wiresheet/t3_snapshot_download", instance: device.instance, snapshot: id });
    const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([JSON.stringify(s, null, 1)], { type: "application/json" }));
    a.download = `t3-${device.instance}-backup-${id}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  };
  const update = async (id, fields) => { try { await ws({ type: "bas_wiresheet/t3_snapshot_update", instance: device.instance, snapshot: id, ...fields }); load(); } catch (e) { setErr(e.message); } };
  const restore = async () => {
    setErr(null);
    try { const r = await ws({ type: "bas_wiresheet/t3_snapshot_restore", instance: device.instance, snapshot: cmp.before, items: Object.keys(pick).filter((k) => pick[k]), confirm: typed }); setTyped(""); follow(r.job); }
    catch (e) { setErr(e.message); }
  };
  const running = job && job.state === "running";
  const items = changes ? [...new Set(changes.changes.map((c) => c.item))] : [];
  const picked = Object.keys(pick).filter((k) => pick[k]);
  return (
    <div className="pad xp-scroll" data-testid="t3-backups">
      <h3 style={{ margin: "0 0 4px" }}>Controller Backups</h3>
      <p className="muted small">A backup reads the whole controller: 16 programs, all inputs, outputs and variables, PID loops, weekly and annual schedules, monitors and settings (about 2 minutes, read only). Compare two backups to see what changed, and put selected items back. Up to 12 backups per controller are kept; pinned ones are never dropped.</p>
      {err && <div className="errtxt" data-testid="t3-backup-error">{err}</div>}
      {canWrite && <div className="row" style={{ gap: 8, alignItems: "flex-end", marginBottom: 8 }}>
        <label className="field" style={{ margin: 0, flex: 1 }}><span>Note</span><input value={note} maxLength={200} onChange={(e) => setNote(e.target.value)} placeholder="e.g. before summer changeover" data-testid="t3-backup-note" /></label>
        <button className="btn primary" disabled={running} onClick={take} data-testid="t3-backup-take">Back Up Now</button>
      </div>}
      {job && <div className={job.state === "failed" ? "errtxt" : "muted"} data-testid="t3-backup-job">
        {job.kind === "restore" ? "Restore" : "Backup"}: {job.state === "running" ? <><progress max={job.total} value={job.done} style={{ verticalAlign: "middle" }} /> {Math.round(100 * job.done / job.total)} %</> : job.state === "failed" ? `failed: ${job.error}` : "done"}
        {job.state === "done" && job.result && job.result.results && <span> · {job.result.results.filter((r) => r.ok).length} of {job.result.results.length} items restored and verified{job.result.ok ? "" : " (see History for the failures)"} · backup taken before restoring: {job.result.pre_restore}</span>}
      </div>}
      {data && (data.snapshots.length ? <table className="points" data-testid="t3-backup-table"><thead><tr><th>Taken</th><th>By</th><th>Note</th><th>Programs</th><th>PID</th><th /></tr></thead>
        <tbody>{data.snapshots.map((s, i) => (
          <tr key={s.id} data-testid={`t3-backup-${i}`}><td className="small">{fmtWhen(s.taken_at)}{s.pinned ? " 📌" : ""}</td><td className="small">{s.by}{s.reason === "pre-restore" ? " (auto)" : ""}</td><td className="small">{s.note || "—"}</td>
            <td className="small">{s.programs_used} used · {s.programs_on} on</td><td className="small">{s.pid_used}</td>
            <td className="nowrap">
              {i + 1 < data.snapshots.length && <button className="btn sm" onClick={() => compare(data.snapshots[i + 1].id, s.id)} data-testid={`t3-backup-cmp-${i}`} title="Compare with the backup before it">Compare with previous</button>}
              <button className="btn sm" style={{ marginLeft: 4 }} onClick={() => download(s.id)}>Download</button>
              {canWrite && <button className="btn sm" style={{ marginLeft: 4 }} onClick={() => update(s.id, { pinned: !s.pinned })}>{s.pinned ? "Unpin" : "Pin"}</button>}
              {canWrite && <button className="btn sm danger" style={{ marginLeft: 4 }} onClick={() => update(s.id, { delete: true })}>Delete</button>}
            </td></tr>))}</tbody></table>
        : <div className="muted pad">No backups of this controller yet.</div>)}
      {data && data.snapshots.length > 1 && <div className="row" style={{ gap: 8, alignItems: "flex-end", margin: "8px 0" }}>
        <label className="field" style={{ margin: 0 }}><span>Compare</span><select value={cmp.before} onChange={(e) => setCmp({ ...cmp, before: e.target.value })} data-testid="t3-cmp-before"><option value="">Older backup…</option>{data.snapshots.map((s) => <option key={s.id} value={s.id}>{fmtWhen(s.taken_at)}{s.note ? ` · ${s.note}` : ""}</option>)}</select></label>
        <label className="field" style={{ margin: 0 }}><span>with</span><select value={cmp.after} onChange={(e) => setCmp({ ...cmp, after: e.target.value })} data-testid="t3-cmp-after"><option value="">Newer backup…</option>{data.snapshots.map((s) => <option key={s.id} value={s.id}>{fmtWhen(s.taken_at)}{s.note ? ` · ${s.note}` : ""}</option>)}</select></label>
        <button className="btn" disabled={!cmp.before || !cmp.after || cmp.before === cmp.after} onClick={() => compare(cmp.before, cmp.after)} data-testid="t3-cmp-run">Compare</button>
      </div>}
      {changes && <div className="box" data-testid="t3-cmp-result">
        <div className="box-h">{changes.changes.length ? `${changes.changes.length} change(s) from ${fmtWhen(changes.before.taken_at)} to ${fmtWhen(changes.after.taken_at)}` : "No differences"}</div>
        {changes.changes.length > 0 && <table className="points"><thead><tr>{canRestore && <th>Put back</th>}<th>Item</th><th>Field</th><th>Before</th><th>After</th></tr></thead>
          <tbody>{changes.changes.map((c, i) => (<React.Fragment key={i}>
            <tr>{canRestore && <td>{items.indexOf(c.item) >= 0 && changes.changes.findIndex((x) => x.item === c.item) === i && <input type="checkbox" checked={!!pick[c.item]} onChange={(e) => setPick({ ...pick, [c.item]: e.target.checked })} data-testid={`t3-pick-${c.item}`} />}</td>}
              <td className="mono">{c.item.replace(":", "")}</td><td>{c.field}</td><td className="mono small">{JSON.stringify(c.before)}</td><td className="mono small">{JSON.stringify(c.after)}</td></tr>
            {c.diff && c.diff.length > 0 && <tr><td colSpan={canRestore ? 5 : 4}><pre className="mono small diff">{c.diff.map((l, k) => <div key={k} className={l[0] === "+" ? "add" : l[0] === "-" ? "del" : l[0] === "@" ? "hunk" : ""}>{l}</div>)}</pre></td></tr>}
          </React.Fragment>))}</tbody></table>}
        {canRestore && changes.changes.length > 0 && <div className="row" style={{ gap: 8, alignItems: "flex-end", marginTop: 8 }}>
          <span className="small">Put the checked items back as they were on <b>{fmtWhen(changes.before.taken_at)}</b>. A new backup is taken first.</span>
          <label className="field" style={{ margin: 0 }}><span>Type RESTORE</span><input className="mono" style={{ width: 100 }} data-confirm="1" value={typed} onChange={(e) => setTyped(e.target.value)} data-testid="t3-restore-confirm" /></label>
          <button className="btn primary" disabled={running || !picked.length || !typedOk(typed, "RESTORE")} onClick={restore} data-testid="t3-restore-run">Restore {picked.length} item(s)</button>
        </div>}
      </div>}
    </div>
  );
}
