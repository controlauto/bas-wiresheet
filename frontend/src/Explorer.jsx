import React, { useEffect, useMemo, useRef, useState } from "react";
import { fmtAge, fmtTime, FRESH_CLASS } from "./model.js";

const TABS = [
  { id: "IN", label: "Inputs" },
  { id: "OUT", label: "Outputs" },
  { id: "VAR", label: "Variables" },
  { id: "PRG", label: "Programs" },
];
const PORT_LABEL = {
  "physical-digital": "Digital (physical)",
  "physical-analog": "Analog (physical)",
  physical: "Physical",
  virtual: "Virtual",
  unknown: "Unknown",
};

function ImportResult({ result, onOpenSheet }) {
  const r = result.report;
  return (
    <div className="box" data-testid="t3-import-result">
      <div className="box-h">Wire Sheet Generated</div>
      <div className="kv"><span>Lines Converted</span><b>{r.mapped} of {r.lines}</b></div>
      <div className="kv"><span>Blocks / Wires</span><b>{r.blocks} / {r.wires}</b></div>
      {r.unmapped.length > 0 && <div className="errtxt">{r.unmapped.length} line(s) could not be converted and appear as Unmapped Code blocks:<ul>{r.unmapped.slice(0, 8).map((u) => <li key={u.line} className="mono small">{u.text}</li>)}</ul></div>}
      {r.notes.map((n) => <div className="muted small" key={n}>{n}</div>)}
      <p className="muted small">This sheet documents the program. It cannot be loaded back to a controller.</p>
      <button className="btn primary" onClick={() => onOpenSheet(result.sheet_id)} data-testid="t3-open-sheet">Open Wire Sheet</button>
    </div>
  );
}

function CodeEditor({ ws, device, view, onDone }) {
  const [text, setText] = useState(view.listing);
  const [check, setCheck] = useState(null);
  const [typed, setTyped] = useState("");
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(null);
  const need = `PROGRAM ${view.program}`;
  const run = async (fn) => { setBusy(true); setErr(null); try { await fn(); } catch (e) { setErr(e.message); } setBusy(false); };
  const doCheck = () => run(async () => { setDone(null); setTyped(""); setCheck(await ws({ type: "bas_wiresheet/t3_code_check", instance: device.instance, slot: view.program, source: text })); });
  const apply = () => run(async () => {
    const r = await ws({ type: "bas_wiresheet/t3_code_apply", token: check.token, confirm: typed });
    setCheck(null); setTyped("");
    if (!r.ok) throw new Error(`Update failed: ${r.error}. Previous program ${r.restored ? "restored" : "NOT restored: " + r.restore_error}`);
    setDone("Program updated and verified on the controller."); onDone();
  });
  const restore = () => run(async () => {
    const r = await ws({ type: "bas_wiresheet/t3_code_restore", instance: device.instance, slot: view.program, confirm: typed });
    if (!r.ok) throw new Error(`Restore failed: ${r.error}`);
    setTyped(""); setCheck(null); setDone("Previous version restored."); onDone();
  });
  return (
    <div className="box" data-testid="t3-code-editor">
      <div className="box-h">Edit Program {view.program}</div>
      <textarea className="mono small" rows={16} style={{ width: "100%" }} value={text} spellCheck={false} onChange={(e) => { setText(e.target.value); setCheck(null); }} data-testid="t3-code-text" />
      <button className="btn" disabled={busy} onClick={doCheck} data-testid="t3-code-check">Compile and Compare</button>
      <span className="muted small"> Reads the controller only.</span>
      {err && <div className="errtxt" data-testid="t3-code-error">{err}</div>}
      {done && <div className="ok-txt" data-testid="t3-code-done">{done}</div>}
      {check && !check.changed && <div className="muted" data-testid="t3-code-same">No logic changes.</div>}
      {check && check.changed && <div data-testid="t3-code-review">
        <pre className="mono small">{check.diff.join("\n")}</pre>
        <div className="kv"><span>Size</span><b>{check.new_bytes} of 2000 bytes</b></div>
        <div className="kv"><span>New Written Points</span><b className="mono">{check.added_writes.join(", ") || "None"}</b></div>
        <div className="kv"><span>No Longer Written</span><b className="mono">{check.removed_writes.join(", ") || "None"}</b></div>
        <div className="kv"><span>Program State</span><b>{check.running ? "Running" : "Stopped"}</b></div>
        {check.running && <div className="errtxt">The program stops for a few seconds while it is written and outputs hold their last value. Its timers and local variables restart.</div>}
        <label className="field"><span>Type {need} to confirm</span><input value={typed} onChange={(e) => setTyped(e.target.value)} data-testid="t3-code-confirm" /></label>
        <button className="btn primary" disabled={busy || typed.trim().toUpperCase() !== need} onClick={apply} data-testid="t3-code-apply">Update Controller</button>
      </div>}
      {!check && done && <div style={{ marginTop: 8 }}>
        <label className="field"><span>Type {need} to restore the previous version</span><input value={typed} onChange={(e) => setTyped(e.target.value)} data-testid="t3-code-restore-confirm" /></label>
        <button className="btn danger" disabled={busy || typed.trim().toUpperCase() !== need} onClick={restore} data-testid="t3-code-restore">Restore Previous Version</button>
      </div>}
      {busy && <div className="muted">Working with the controller…</div>}
    </div>
  );
}

function PointWrite({ ws, deviceId, point, onClose, onDone }) {
  const name = `${point.category}${point.index}`;
  const [value, setValue] = useState(point.value);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [res, setRes] = useState(null);
  const write = async () => {
    setBusy(true); setErr(null);
    try { setRes(await ws({ type: "bas_wiresheet/t3_point_write", device_id: deviceId, entity_id: point.entity_id, value: Number(value), confirm: typed })); onDone(); }
    catch (e) { setErr(e.message); }
    setBusy(false);
  };
  const ok = value !== "" && Number.isFinite(Number(value)) && typed.trim().toUpperCase() === name;
  return (
    <tr className="prot-sandbox" data-testid={`t3-write-row-${name}`}><td colSpan={12}>
      <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <b>Write {name}</b><span className="muted small"> · current {point.value}{point.entity_id.startsWith("switch.") ? " · write 1 (on) or 0 (off)" : ""}</span>
        <label className="field" style={{ margin: 0 }}><span>New value</span><input className="mono" style={{ width: 110 }} value={value} onChange={(e) => setValue(e.target.value)} data-testid="t3-write-value" /></label>
        <label className="field" style={{ margin: 0 }}><span>Type {name} to confirm</span><input className="mono" style={{ width: 110 }} value={typed} onChange={(e) => setTyped(e.target.value)} data-testid="t3-write-confirm" /></label>
        <button className="btn primary" disabled={!ok || busy} onClick={write} data-testid="t3-write-apply">Write Value</button>
        <button className="btn" onClick={onClose}>Close</button>
        {busy && <span className="muted">Writing…</span>}
        {err && <span className="errtxt" data-testid="t3-write-error">{err}</span>}
        {res && <span className="ok-txt" data-testid="t3-write-done">Sent {res.point} = {res.requested} (was {res.previous}). The value updates after the next poll.</span>}
      </div>
      {String(point.port).startsWith("physical")
        ? <div className="errtxt small" data-testid="t3-write-physical">PHYSICAL OUTPUT: the equipment wired to {name} will respond. Check the process before writing. The write goes through the BACnet/IP integration and is recorded in History.</div>
        : <div className="muted small">Virtual point: no physical output changes directly. The write goes through the BACnet/IP integration and is recorded in History.</div>}
    </td></tr>
  );
}

function PointConfig({ ws, instance, point, onClose, onDone }) {
  const name = `${point.category}${point.index}`;
  const [cfg, setCfg] = useState(null);
  const [edit, setEdit] = useState({});
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [done, setDone] = useState(null);
  const load = () => ws({ type: "bas_wiresheet/t3_point_config", instance, category: point.category, index: point.index })
    .then((r) => { setCfg(r); setEdit({ description: r.description, label: r.label, auto_manual: r.auto_manual, digital_analog: r.digital_analog, range: r.range }); })
    .catch((e) => setErr(e.message));
  useEffect(() => { load(); }, []); // eslint-disable-line
  const changes = cfg ? Object.fromEntries(Object.entries(edit).filter(([k, v]) => String(v) !== String(cfg[k]))) : {};
  const save = async () => {
    setBusy(true); setErr(null); setDone(null);
    try { const r = await ws({ type: "bas_wiresheet/t3_point_config_write", instance, category: point.category, index: point.index, changes, confirm: typed });
      setDone(r.changed ? "Saved and verified on the controller." : "No change."); setTyped(""); await load(); onDone(); }
    catch (e) { setErr(e.message); }
    setBusy(false);
  };
  const units = cfg && (edit.digital_analog === "analog" ? cfg.units_analog : cfg.units_digital);
  return (
    <tr className="prot-normal" data-testid={`t3-config-row-${name}`}><td colSpan={12}>
      <div className="xp-config">
        <b>Configure {name}</b>
        {!cfg && !err && <span className="muted"> Reading from the controller…</span>}
        {cfg && <div className="row" style={{ gap: 8, flexWrap: "wrap", alignItems: "flex-end" }}>
          <label className="field" style={{ margin: 0 }}><span>Description (max {cfg.description_max})</span><input maxLength={cfg.description_max} value={edit.description} onChange={(e) => setEdit({ ...edit, description: e.target.value })} data-testid="t3-cfg-description" /></label>
          <label className="field" style={{ margin: 0 }}><span>Label (max {cfg.label_max})</span><input className="mono" maxLength={cfg.label_max} value={edit.label} onChange={(e) => setEdit({ ...edit, label: e.target.value.toUpperCase() })} data-testid="t3-cfg-label" style={{ width: 110 }} /></label>
          <label className="field" style={{ margin: 0 }}><span>Auto / Manual</span><select value={edit.auto_manual} onChange={(e) => setEdit({ ...edit, auto_manual: e.target.value })} data-testid="t3-cfg-am"><option value="auto">Auto</option><option value="manual">Manual</option></select></label>
          {point.category === "VAR" && <>
            <label className="field" style={{ margin: 0 }}><span>Type</span><select value={edit.digital_analog} onChange={(e) => setEdit({ ...edit, digital_analog: e.target.value, range: 0 })} data-testid="t3-cfg-da"><option value="analog">Analog</option><option value="digital">Digital</option></select></label>
            <label className="field" style={{ margin: 0 }}><span>Units</span><select value={edit.range} onChange={(e) => setEdit({ ...edit, range: Number(e.target.value) })} data-testid="t3-cfg-range">
              {units.map((u, i) => <option key={i} value={i}>{u}</option>)}
              {edit.range >= units.length && <option value={edit.range}>custom {edit.range}</option>}
            </select></label>
          </>}
          <label className="field" style={{ margin: 0 }}><span>Type {name} to confirm</span><input className="mono" style={{ width: 90 }} value={typed} onChange={(e) => setTyped(e.target.value)} data-testid="t3-cfg-confirm" /></label>
          <button className="btn primary" disabled={busy || !Object.keys(changes).length || typed.trim().toUpperCase() !== name} onClick={save} data-testid="t3-cfg-save">Save to Controller</button>
          <button className="btn" onClick={onClose}>Close</button>
        </div>}
        {err && <div className="errtxt small" data-testid="t3-cfg-error">{err}</div>}
        {done && <div className="ok-txt small" data-testid="t3-cfg-done">{done}</div>}
        <div className="muted small">Writes only the changed fields to the controller and reads them back. Programs refer to points by label: renaming a label used in a program changes how the program listing reads. Home Assistant entity names update when the BACnet/IP integration reads the device again.</div>
      </div>
    </td></tr>
  );
}

function Discovery({ ws, onClose }) {
  const [res, setRes] = useState(null);
  const [all, setAll] = useState(false);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const run = async (refresh) => { setBusy(true); setErr(null); try { setRes(await ws({ type: "bas_wiresheet/t3_discover", refresh })); } catch (e) { setErr(e.message); } setBusy(false); };
  useEffect(() => { run(false); }, []); // eslint-disable-line
  return (
    <div className="pad xp-scroll" data-testid="t3-discovery">
      <div className="row" style={{ gap: 8, alignItems: "center" }}>
        <h3 style={{ margin: 0 }}>BACnet Network Discovery</h3>
        <button className="btn" disabled={busy} onClick={() => run(true)} data-testid="t3-discover-refresh">Search Again</button>
        <button className="btn" onClick={onClose}>Back to Points</button>
      </div>
      <p className="muted small">Sends one Who-Is broadcast through the BACnet/IP integration and reads each device's name, vendor and model. Read only; results are kept for 5 minutes.</p>
      {busy && <div className="muted">Searching the network (about 5 s)…</div>}
      {err && <div className="errtxt">{err}</div>}
      {res && <>
        <div className="muted small">{res.devices.filter((d) => d.temco).length} TEMCO controller(s) of {res.devices.length} BACnet device(s) · searched {fmtTime(res.at)}
          <label style={{ marginLeft: 10 }}><input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} data-testid="t3-discover-all" /> Show other BACnet devices</label></div>
        <table className="points" data-testid="t3-discover-table"><thead><tr><th>Instance</th><th>Name</th><th>Address</th><th>Network</th><th>Vendor</th><th>Model</th><th>Firmware</th><th>Home Assistant</th><th>Wire Sheet</th></tr></thead>
          <tbody>{res.devices.filter((d) => all || d.temco).map((d) => (
            <tr key={d.instance} data-testid={`t3-found-${d.instance}`}>
              <td className="mono">{d.instance}</td><td>{d.name || "—"}</td><td className="mono small">{d.address || "—"}</td>
              <td className="small">{d.mstp_network ? <>MS/TP {d.mstp_network} · MAC {d.mstp_mac} {d.router_configured ? <span className="chip ok">Router set</span> : <span className="chip dq" title="Add the router under t3000: mstp_routers">Router needed</span>}</> : "BACnet/IP"}</td>
              <td>{d.vendor || "—"}{d.temco ? <span className="chip ok" style={{ marginLeft: 4 }}>TEMCO</span> : null}</td><td>{d.model || "—"}</td><td className="small">{d.firmware || "—"}</td>
              <td>{d.in_home_assistant ? (d.not_answering ? <span className="chip bad">Added, did not answer</span> : <span className="chip ok">Added</span>) : <span className="chip unk">Not added</span>}</td>
              <td>{d.configured ? <span className="chip ok">Configured</span> : <span className="muted">—</span>}</td>
            </tr>))}</tbody></table>
        <p className="muted small">BAS Wire Sheet works with TEMCO controllers only. To use a new one, add it in the BACnet/IP integration; with full access it is available at once. MS/TP controllers are reached through their BACnet router: list it under <span className="mono">bas_wiresheet: t3000: mstp_routers: {"{"}network: router_ip{"}"}</span>.</p>
      </>}
    </div>
  );
}

function Programs({ ws, device, role, onOpenSheet }) {
  const [list, setList] = useState(null);
  const [view, setView] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const [file, setFile] = useState(null);
  const [made, setMade] = useState(null);
  useEffect(() => { setList(null); setView(null); setErr(null); setFile(null); setMade(null); }, [device && device.instance]);
  if (!device) return null;
  const editor = role === "editor";
  const canRead = device.programs && device.programs.read;
  const canEdit = editor && view && view.listing_safe !== false && device.programs && device.programs.write_slots.includes(view.program);
  const run = async (fn) => { setBusy(true); setErr(null); try { await fn(); } catch (e) { setErr(e.message); } setBusy(false); };
  const openFile = (f) => run(async () => {
    const bytes = new Uint8Array(await f.arrayBuffer());
    let bin = "";
    for (let i = 0; i < bytes.length; i += 8192) bin += String.fromCharCode(...bytes.subarray(i, i + 8192));
    const data = btoa(bin);
    const m = f.name.match(/^(\d+)\.prog$/i);
    const instance = m ? Number(m[1]) : device.instance;
    const r = await ws({ type: "bas_wiresheet/t3_prog_file", data, instance });
    setFile({ name: f.name, data, instance, programs: r.programs }); setMade(null);
  });
  return (
    <div className="pad xp-scroll" data-testid="t3-programs">
      {canRead ? <>
        <button className="btn" disabled={busy} onClick={() => run(async () => setList((await ws({ type: "bas_wiresheet/t3_program_list", instance: device.instance })).programs))} data-testid="t3-read-list">Read Program List</button>
        <span className="muted small"> Read only. Sends a few requests to the controller.</span>
      </> : <div className="muted">Reading from this controller is not enabled (<span className="mono">allow_program_read</span>).</div>}
      {editor && <div style={{ marginTop: 8 }}><label className="btn">Open T3000 .prog File<input type="file" accept=".prog" hidden data-testid="t3-prog-file" onChange={(e) => e.target.files[0] && openFile(e.target.files[0])} /></label><span className="muted small"> Generates a wire sheet from a saved program. No controller traffic.</span></div>}
      {busy && <div className="muted">Working…</div>}
      {err && <div className="errtxt">{err}</div>}
      {made && <ImportResult result={made} onOpenSheet={onOpenSheet} />}
      {file && <div data-testid="t3-file-list"><h3>{file.name}</h3><table className="points"><thead><tr><th>#</th><th>Description</th><th>Bytes</th><th /></tr></thead>
        <tbody>{file.programs.filter((p) => !p.empty).map((p) => <tr key={p.program}><td>PRG{p.program}</td><td>{p.description || "—"}</td><td>{p.bytes}</td>
          <td><button className="btn sm" disabled={busy} data-testid={`t3-file-sheet-${p.program}`} onClick={() => run(async () => setMade(await ws({ type: "bas_wiresheet/t3_prog_file", data: file.data, instance: file.instance, slot: p.program })))}>Create Wire Sheet</button></td></tr>)}</tbody></table></div>}
      {list && <table className="points" data-testid="t3-prg-table"><thead><tr><th>#</th><th>Label</th><th>Description</th><th>Bytes</th><th>Status</th><th /></tr></thead>
        <tbody>{list.map((p) => <tr key={p.program}><td>PRG{p.program}</td><td>{p.label || "—"}</td><td>{p.description || "—"}</td><td>{p.bytes}</td><td>{p.bytes ? (p.on ? "ON" : "OFF") : "Empty"}</td>
          <td><button className="btn sm" disabled={busy} onClick={() => run(async () => setView(await ws({ type: "bas_wiresheet/t3_program_read", instance: device.instance, slot: p.program })))} data-testid={`t3-view-${p.program}`}>View</button>
            {editor && p.bytes > 0 && <button className="btn sm" disabled={busy} onClick={() => run(async () => setMade(await ws({ type: "bas_wiresheet/t3_program_to_sheet", instance: device.instance, slot: p.program })))} data-testid={`t3-to-sheet-${p.program}`}>Create Wire Sheet</button>}</td></tr>)}</tbody></table>}
      {view && <div data-testid="t3-prg-view"><h3>Program {view.program}</h3><div className="muted small">{view.empty ? "Empty" : `${view.used} bytes · SHA-256 ${view.sha256}`}</div>{!view.empty && !(canEdit) && <pre className="mono small">{view.listing}</pre>}
        {!view.empty && <button className="btn sm" style={{ margin: "4px 0" }} data-testid="t3-download-txt" onClick={() => {
          const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([view.listing.replace(/\r?\n/g, "\r\n")], { type: "text/plain" }));
          a.download = `${device.instance}-PRG${String(view.program).padStart(2, "0")}.txt`; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
        }} title="T3000 program editor > Load File">Download .txt (T3000)</button>}
        {!view.empty && view.listing_safe === false && <div className="errtxt" data-testid="t3-listing-unsafe">T3000's listing of this program does not compile back to the same logic. Do not open and save it in T3000. Editing is disabled.</div>}
        {!view.empty && canEdit && <CodeEditor key={view.program} ws={ws} device={device} view={view} onDone={() => run(async () => setView(await ws({ type: "bas_wiresheet/t3_program_read", instance: device.instance, slot: view.program })))} />}</div>}
    </div>
  );
}

export default function Explorer({ hass, role, onOpenSheet }) {
  const hassRef = useRef(hass);
  hassRef.current = hass;
  const ws = (m) => hassRef.current.callWS(m);
  const [devices, setDevices] = useState(null);
  const [sel, setSel] = useState(null);
  const [data, setData] = useState(null);
  const [tab, setTab] = useState("IN");
  const [filter, setFilter] = useState("");
  const [err, setErr] = useState(null);
  const [now, setNow] = useState(Date.now());
  const [writing, setWriting] = useState(null);
  const [configuring, setConfiguring] = useState(null);
  const [discover, setDiscover] = useState(false);

  useEffect(() => {
    ws({ type: "bas_wiresheet/t3_devices" })
      .then((r) => { setDevices(r.devices); if (r.devices.length) setSel(r.devices[0].device_id); })
      .catch((e) => setErr(e.message));
  }, []); // eslint-disable-line

  useEffect(() => {
    if (!sel) return undefined;
    let stop = false;
    const load = () => ws({ type: "bas_wiresheet/t3_points", device_id: sel }).then((r) => { if (!stop) { setData(r); setErr(null); } }).catch((e) => !stop && setErr(e.message));
    load();
    const h = setInterval(load, 5000);
    return () => { stop = true; clearInterval(h); };
  }, [sel]); // eslint-disable-line

  useEffect(() => { const h = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(h); }, []);

  const rows = useMemo(() => {
    if (!data) return [];
    const f = filter.trim().toLowerCase();
    return data.points.filter((p) => p.category === tab && (!f || `${p.category}${p.index} ${p.label} ${p.entity_id} ${p.description || ""}`.toLowerCase().includes(f)));
  }, [data, tab, filter]);

  const dev = data && data.device;
  const selDev = (devices || []).find((d) => d.device_id === sel);
  const canWrite = role === "editor" && !!(selDev && selDev.programs && selDev.programs.point_writes);
  const writable = (p) => !!p.writable;
  const fullAccess = !!(data && data.device && data.device.access === "full");
  const reload = () => ws({ type: "bas_wiresheet/t3_points", device_id: sel }).then((r) => setData(r)).catch(() => {});
  const hbAge = dev && dev.heartbeat.last_poll ? now - dev.heartbeat.last_poll : null;
  const counts = useMemo(() => {
    const c = { IN: 0, OUT: 0, VAR: 0 };
    (data ? data.points : []).forEach((p) => { if (c[p.category] !== undefined) c[p.category] += 1; });
    return c;
  }, [data]);

  return (
    <div className="xp" data-testid="t3-explorer">
      <aside className="lib">
        <div className="pane-h">Network</div>
        {!devices && !err && <div className="muted pad">Loading devices…</div>}
        {devices && !devices.length && <div className="muted pad">No BACnet devices found in Home Assistant.</div>}
        {(devices || []).map((d) => (
          <div key={d.device_id} className={`xp-dev ${sel === d.device_id ? "on" : ""} ${d.entry.disabled ? "dis" : ""}`} onClick={() => setSel(d.device_id)} data-testid={`t3-dev-${d.instance}`}>
            <div className="xp-dev-name">{d.name}</div>
            <div className="muted small mono">BACnet {d.instance}</div>
            <div className="small">{d.model || <span className="muted">Model not configured</span>}{d.entry.disabled ? " · Disabled in HA" : ""}</div>
          </div>
        ))}
        <div className="pad">{role === "editor" && <button className="btn sm" onClick={() => setDiscover(true)} data-testid="t3-discover">Discover Devices</button>}</div>
        <div className="muted small pad">TEMCO controllers from the BACnet/IP integration (BACnet/IP and MS/TP through a router). Discover Devices lists every TEMCO controller that answers on the network.</div>
      </aside>
      <section className="xp-main">
        <div className="xp-head">
          {dev ? (
            <>
              <div className="xp-title">{dev.name} <span className="mono muted">· BACnet {dev.instance}</span></div>
              {canWrite ? (fullAccess ? <span className="badge role editor" title="Every point except safety and Commissioning Pending points" data-testid="t3-full-access">Full Access</span> : <span className="badge role editor" title="Only Test Sandbox points on virtual ports">Sandbox Writes Enabled</span>) : <span className="badge role operator">Read Only</span>}
              <span className="muted small">Model: <b>{dev.model || "Not configured"}</b>{dev.model_source ? ` (${dev.model_source})` : ""}</span>
              <span className={`chip ${hbAge !== null && hbAge <= dev.stale_after_s * 1000 ? "ok" : hbAge === null ? "unk" : "bad"}`} title={dev.heartbeat.entity_id || ""}>
                {hbAge === null ? "No Poll Heartbeat" : `Last Poll ${fmtAge(hbAge)} ago`}
              </span>
              <span className="muted small">Poll every {dev.entry.polling_interval || "?"} s · Stale after {dev.stale_after_s} s</span>
            </>
          ) : <div className="xp-title muted">Select a device</div>}
        </div>
        <div className="xp-tabs">
          {TABS.map((t) => (
            <button key={t.id} className={`seg-b ${tab === t.id ? "on" : ""}`} onClick={() => setTab(t.id)} data-testid={`t3-tab-${t.id}`}>
              {t.label}{counts[t.id] !== undefined ? ` (${counts[t.id]})` : ""}
            </button>
          ))}
          <input className="xp-filter" placeholder="Filter by number, label or entity" value={filter} onChange={(e) => setFilter(e.target.value)} />
        </div>
        {err && <div className="notel pad">{err}</div>}
        {discover ? <Discovery ws={ws} onClose={() => setDiscover(false)} /> : tab === "PRG" ? (
          <Programs ws={ws} role={role} onOpenSheet={onOpenSheet} device={(devices || []).find((d) => d.device_id === sel)} />
        ) : (
          <div className="xp-tablewrap">
            <table className="points xp-table" data-testid="t3-table">
              <thead><tr>
                <th>#</th><th>Label</th><th>Value</th><th>Units</th><th>Auto/Man</th><th>Port</th><th>Protection</th><th>Freshness</th><th>Last Report</th><th>Flags</th><th>Entity</th><th />
              </tr></thead>
              <tbody>
                {rows.map((p) => (<React.Fragment key={p.entity_id}>
                  <tr className={`prot-${p.protection}`}>
                    <td className="mono">{p.category}{p.index}</td>
                    <td>{p.label}</td>
                    <td className="mono">{p.value}</td>
                    <td>{p.units || <span className="muted">—</span>}</td>
                    <td>{p.auto_manual === "Manual" ? <span className="chip dq">Manual</span> : <span className="muted">Auto</span>}</td>
                    <td className={p.port === "virtual" ? "muted" : ""}>{PORT_LABEL[p.port] || p.port}</td>
                    <td>{p.protection === "locked" ? <span className="chip bad" title={p.protection_reason}>Locked</span> : p.protection === "sandbox" ? <span className="chip unk" title={p.protection_reason}>Test Sandbox</span> : <span className="muted">—</span>}</td>
                    <td><span className={`chip ${FRESH_CLASS[p.freshness] || "unk"}`} title={p.freshness_reason}>{p.freshness}</span></td>
                    <td className="small" title={`Last value change ${fmtTime(p.last_changed)}`}>{fmtAge(now - p.last_reported)} ago</td>
                    <td className="small">{["in_alarm", "fault", "overridden", "out_of_service"].filter((k) => p.status_flags[k]).map((k) => k.replace(/_/g, " ")).join(", ") || <span className="muted">—</span>}</td>
                    <td className="mono small muted">{p.entity_id}</td>
                    <td className="nowrap">{canWrite && writable(p) && <button className="btn sm" onClick={() => setWriting(writing === p.entity_id ? null : p.entity_id)} data-testid={`t3-write-${p.category}${p.index}`}>Write</button>}
                      {canWrite && fullAccess && p.protection !== "locked" && <button className="btn sm" style={{ marginLeft: 4 }} onClick={() => setConfiguring(configuring === p.entity_id ? null : p.entity_id)} data-testid={`t3-config-${p.category}${p.index}`}>Configure</button>}</td>
                  </tr>
                  {configuring === p.entity_id && <PointConfig ws={ws} instance={dev.instance} point={p} onClose={() => setConfiguring(null)} onDone={() => setTimeout(reload, 1500)} />}
                  {writing === p.entity_id && <PointWrite ws={ws} deviceId={sel} point={p} onClose={() => setWriting(null)} onDone={() => setTimeout(reload, 1500)} />}
                </React.Fragment>))}
                {!rows.length && data && <tr><td colSpan={12} className="muted">No points.</td></tr>}
              </tbody>
            </table>
          </div>
        )}
        <div className="muted small pad">
          {canWrite ? (fullAccess ? "Full access: every point can be written, one value at a time with typed confirmation, except safety points and Commissioning Pending equipment (Locked)." : "Only Test Sandbox points on virtual ports can be written, one value at a time with typed confirmation; everything else is read only.") : "Read only: no BACnet request is sent from this page."} Values are the states the BACnet/IP integration keeps in Home Assistant. Freshness uses each point's last report (the integration writes every point on every poll); the header shows the device's last successful poll.
          Port types follow T3000 (GetOutputType). Locked points can never be written; Test Sandbox points are the approved test points for the write phase.
        </div>
      </section>
    </div>
  );
}
