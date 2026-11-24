// Explorer tabs for T3000 schedules (weekly and annual routines) and PID controllers.
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
        <label className="field inl"><span>Type {name} to confirm</span><input className="mono" style={{ width: 80 }} value={typed} onChange={(e) => setTyped(e.target.value)} data-testid="wr-confirm" /></label>
        <button className="btn primary" disabled={busy || typed.trim().toUpperCase() !== name} onClick={save} data-testid="wr-save">Save to Controller</button>
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
        <label className="field inl"><span>Type {name} to confirm</span><input className="mono" style={{ width: 80 }} value={typed} onChange={(e) => setTyped(e.target.value)} data-testid="ar-confirm" /></label>
        <button className="btn primary" disabled={typed.trim().toUpperCase() !== name} onClick={save} data-testid="ar-save">Save to Controller</button>
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
      <label className="field inl"><span>Type {name}</span><input className="mono" style={{ width: 80 }} value={typed} onChange={(e) => setTyped(e.target.value)} data-testid="pid-confirm" /></label>
      <button className="btn primary" disabled={typed.trim().toUpperCase() !== name} onClick={save} data-testid="pid-save">Save to Controller</button>
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
