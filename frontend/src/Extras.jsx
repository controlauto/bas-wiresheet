// History view, trend chart and printable documentation. All read only.
import React, { useEffect, useMemo, useRef, useState } from "react";
import { fmtTime } from "./model.js";

/* ---------------- History ---------------- */

export const ACTION_LABEL = {
  program_load: "Program loaded", program_start: "Program started", program_stop: "Program stopped", program_remove: "Program removed",
  code_update: "Program code updated", code_restore: "Program code restored", point_write: "Point written", sheet_access: "Sheet access changed",
  revision: "Revision saved", deploy: "Deployed to test", rollback: "Test rollback",
  program_changed_outside: "Program changed outside the panel", program_checked: "Panel change confirmed",
  sheet_run_start: "Sheet started in Home Assistant", sheet_run_stop: "Sheet stopped in Home Assistant", sheet_alert: "Sheet alert", sheet_alert_cleared: "Sheet alert cleared",
};
const CONTROLLER_ACTIONS = new Set(["program_load", "program_start", "program_stop", "program_remove", "code_update", "code_restore", "point_write",
  "program_changed_outside", "program_checked"]);

export function downloadBase64(name, b64, type) {
  const bin = atob(b64); const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i += 1) bytes[i] = bin.charCodeAt(i);
  const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([bytes], { type })); a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}

async function exportPrograms(ws, instance, source, setMsg) {
  setMsg(source === "controller" ? `Reading the programs of ${instance}…` : `Preparing ${instance}…`);
  try {
    const r = await ws({ type: "bas_wiresheet/program_export", instance, source });
    downloadBase64(r.filename, r.zip_b64, "application/zip");
    const unsafe = r.programs.filter((p) => p.listing_compiles_to_same_logic === false).map((p) => p.program);
    setMsg(`Downloaded ${r.filename}: ${r.programs.filter((p) => !p.empty).length} program(s)${unsafe.length ? `; do not load PRG ${unsafe.join(", ")} text into a controller (see README)` : ""}`);
  } catch (e) { setMsg(e.message); }
}

function ProgramWatchBar({ ws, role }) {
  const [st, setSt] = useState(null);
  const [msg, setMsg] = useState(null);
  const load = () => ws({ type: "bas_wiresheet/program_watch_status" }).then(setSt).catch(() => {});
  useEffect(() => { load(); const h = setInterval(load, 10000); return () => clearInterval(h); }, []); // eslint-disable-line
  if (!st) return null;
  const last = st.runs.length ? st.runs[st.runs.length - 1] : null;
  const run = async () => { setMsg(null); try { const r = await ws({ type: "bas_wiresheet/program_watch_run" }); setMsg(`Reading ${r.devices.length} controller(s)…`); load(); } catch (e) { setMsg(e.message); } };
  return (
    <div className="pad small" data-testid="program-watch" style={{ borderBottom: "1px solid var(--line)" }}>
      <b>Program Watch</b> · {st.enabled ? `every night at ${String(st.time).slice(0, 5)}` : "off"} · controllers {st.devices.join(", ") || "none"} · {st.programs.filter((p) => p.used).length} program(s) on file
      {last && <> · last read {fmtTime(Date.parse(last.at))} ({last.reason}): {last.external ? <span className="chip bad">{last.external} changed outside the panel</span> : <span className="chip ok">no outside changes</span>}{Object.entries(last.devices).filter(([, v]) => v !== "ok").map(([k, v]) => <span key={k} className="chip dq" style={{ marginLeft: 4 }}>{k}: {v}</span>)}</>}
      {st.running && <span className="chip unk" style={{ marginLeft: 6 }}>Reading…</span>}
      {role === "editor" && <button className="btn sm" style={{ marginLeft: 8 }} disabled={st.running} onClick={run} data-testid="program-watch-run">Check Programs Now</button>}
      {st.devices.map((inst) => (
        <span key={inst} style={{ marginLeft: 8 }}>
          <button className="btn sm" onClick={() => exportPrograms(ws, inst, "stored", setMsg)} data-testid={`export-stored-${inst}`} title="Last Program Watch copy, no controller traffic">Export {inst}</button>
          <button className="btn sm" onClick={() => exportPrograms(ws, inst, "controller", setMsg)} data-testid={`export-live-${inst}`} title="Read all 16 programs now (read only)">Export {inst} from Controller</button>
        </span>
      ))}
      {msg && <span className="muted"> {msg}</span>}
    </div>
  );
}

export function History({ hass, role }) {
  const ws = (m) => hass.callWS(m);
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [kind, setKind] = useState("controller");
  const [filter, setFilter] = useState("");
  const load = () => ws({ type: "bas_wiresheet/history", limit: 2000 }).then((r) => { setData(r); setErr(null); }).catch((e) => setErr(e.message));
  useEffect(() => { load(); const h = setInterval(load, 30000); return () => clearInterval(h); }, []); // eslint-disable-line
  const rows = useMemo(() => {
    if (!data) return [];
    const f = filter.trim().toLowerCase();
    return data.items.filter((r) => (kind === "all" || (kind === "controller" ? CONTROLLER_ACTIONS.has(r.action) : !CONTROLLER_ACTIONS.has(r.action)))
      && (!f || [r.by, r.action, ACTION_LABEL[r.action], r.target_label, r.sheet_name, r.detail].join(" ").toLowerCase().includes(f)));
  }, [data, kind, filter]);
  return (
    <div className="app" data-testid="history-view">
      <div className="xp-head">
        <div className="xp-title">Change History</div>
        <span className="badge role operator">Read Only</span>
        <div className="seg" style={{ marginLeft: 12 }}>
          {[["controller", "Controller Changes"], ["sheets", "Sheet Changes"], ["all", "All"]].map(([k, l]) => (
            <button key={k} className={`seg-b ${kind === k ? "on" : ""}`} onClick={() => setKind(k)} data-testid={`history-kind-${k}`}>{l}</button>
          ))}
        </div>
        <input className="xp-filter" placeholder="Filter by user, controller, sheet or detail" value={filter} onChange={(e) => setFilter(e.target.value)} data-testid="history-filter" />
        <button className="btn sm" onClick={load}>Refresh</button>
      </div>
      <ProgramWatchBar ws={ws} role={role} />
      {err && <div className="errtxt pad">{err}</div>}
      <div className="xp-tablewrap xp-scroll">
        <table className="points xp-table" data-testid="history-table">
          <thead><tr><th>Time</th><th>User</th><th>Action</th><th>Controller / Target</th><th>Sheet</th><th>Result</th><th>Detail</th></tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={`${r.at}-${i}`}>
                <td className="small mono">{r.at ? fmtTime(Date.parse(r.at)) : "—"}</td>
                <td>{r.by || "—"}</td>
                <td>{ACTION_LABEL[r.action] || r.action}</td>
                <td className="small">{r.target_label || <span className="muted">—</span>}</td>
                <td className="small">{r.sheet_name || <span className="muted">—</span>}</td>
                <td>{r.ok === false ? <span className="chip bad">Failed</span> : <span className="chip ok">OK</span>}</td>
                <td className="small">{r.detail}</td>
              </tr>
            ))}
            {data && !rows.length && <tr><td colSpan={7} className="muted">No entries.</td></tr>}
          </tbody>
        </table>
      </div>
      <div className="muted small pad">{data ? `${rows.length} of ${data.total} entries.` : "Loading…"} Controller changes made from this panel since version 0.7.3 are recorded with their result; earlier program loads, starts, stops and removals come from each sheet's record, earlier code updates from the stored previous versions. Changes made in T3000 itself are found by Program Watch at its next read.</div>
    </div>
  );
}

/* ---------------- Trend chart ---------------- */

const SERIES = "#3987e5"; // reference palette slot 1 (dark), validated on the panel surface #141b24
const RANGES = [1, 6, 24];

function asNumber(s) {
  if (s === "on") return 1;
  if (s === "off") return 0;
  const n = Number(s);
  return s !== "" && s !== null && Number.isFinite(n) ? n : null;
}

export function TrendChart({ ws, entityId, title }) {
  const [hours, setHours] = useState(6);
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [hover, setHover] = useState(null);
  const [table, setTable] = useState(false);
  const svg = useRef(null);
  useEffect(() => {
    let stop = false;
    const load = () => ws({ type: "bas_wiresheet/point_history", entity_id: entityId, hours })
      .then((r) => { if (!stop) { setData(r); setErr(null); } }).catch((e) => !stop && setErr(e.message));
    setData(null); load();
    const h = setInterval(load, 30000);
    return () => { stop = true; clearInterval(h); };
  }, [entityId, hours]); // eslint-disable-line
  const W = 268, H = 132, L = 40, R = 8, T = 10, B = 22;
  const model = useMemo(() => {
    if (!data) return null;
    const pts = data.points.map(([t, s]) => ({ t, s, v: asNumber(s) }));
    const binary = pts.length > 0 && pts.every((p) => p.v === null || p.s === "on" || p.s === "off");
    const nums = pts.filter((p) => p.v !== null).map((p) => p.v);
    if (!nums.length) return { pts, empty: true };
    let lo = Math.min(...nums), hi = Math.max(...nums);
    if (binary) { lo = 0; hi = 1; } else if (hi - lo < 1e-9) { lo -= 1; hi += 1; } else { const pad = (hi - lo) * 0.08; lo -= pad; hi += pad; }
    const x = (t) => L + ((t - data.start) / (data.end - data.start)) * (W - L - R);
    const y = (v) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
    // step through time: each state holds until the next change; unavailable breaks the line
    const segs = []; let cur = [];
    pts.forEach((p, i) => {
      const t0 = Math.max(p.t, data.start), t1 = i + 1 < pts.length ? pts[i + 1].t : data.end;
      if (p.v === null) { if (cur.length) segs.push(cur); cur = []; return; }
      if (binary) { cur.push([x(t0), y(p.v)], [x(t1), y(p.v)]); }
      else { cur.push([x(t0), y(p.v)]); if (i + 1 === pts.length) cur.push([x(t1), y(p.v)]); }
    });
    if (cur.length) segs.push(cur);
    const gaps = pts.filter((p) => p.v === null).length;
    return { pts, binary, lo, hi, x, y, segs, gaps };
  }, [data]);
  const unit = data && data.unit ? ` ${data.unit}` : "";
  const fmtV = (v, s) => (model && model.binary ? String(s).toUpperCase() : v === null ? String(s) : `${Number(v.toFixed(2))}${unit}`);
  const onMove = (e) => {
    if (!model || model.empty || !svg.current) return;
    const r = svg.current.getBoundingClientRect();
    const px = ((e.clientX - r.left) / r.width) * W;
    const t = data.start + ((px - L) / (W - L - R)) * (data.end - data.start);
    let best = null;
    model.pts.forEach((p) => { if (p.t <= t) best = p; });
    setHover(best && px >= L ? { p: best, px: Math.min(Math.max(px, L), W - R) } : null);
  };
  return (
    <div className="trend" data-testid="trend">
      <div className="pane-h sub">Trend · {title || entityId}</div>
      <div className="row" style={{ gap: 4, margin: "4px 0" }}>
        {RANGES.map((h) => <button key={h} className={`btn sm ${hours === h ? "primary" : ""}`} onClick={() => setHours(h)} data-testid={`trend-${h}h`}>{h} h</button>)}
        <button className="btn sm" onClick={() => setTable(!table)} data-testid="trend-table-toggle">{table ? "Chart" : "Values"}</button>
      </div>
      {err && <div className="errtxt small">{err}</div>}
      {!data && !err && <div className="muted small">Loading history…</div>}
      {model && model.empty && <div className="muted small" data-testid="trend-empty">No numeric values recorded in the last {hours} h.</div>}
      {model && !model.empty && !table && (
        <svg ref={svg} viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label={`${title || entityId}, last ${hours} hours`} onMouseMove={onMove} onMouseLeave={() => setHover(null)} data-testid="trend-svg" style={{ display: "block" }}>
          <rect x={L} y={T} width={W - L - R} height={H - T - B} fill="none" stroke="#283444" />
          {[model.lo, (model.lo + model.hi) / 2, model.hi].map((v, i) => (
            <g key={i}>
              {i === 1 && <line x1={L} x2={W - R} y1={model.y(v)} y2={model.y(v)} stroke="#1f2a37" />}
              <text x={L - 4} y={model.y(v) + 3} textAnchor="end" fontSize="9" fill="#8b97a8">{model.binary ? (v === 1 ? "ON" : v === 0 ? "OFF" : "") : Number(v.toFixed(1))}</text>
            </g>
          ))}
          <text x={L} y={H - 6} fontSize="9" fill="#8b97a8">{new Date(data.start).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</text>
          <text x={W - R} y={H - 6} fontSize="9" fill="#8b97a8" textAnchor="end">now</text>
          {model.segs.map((s, i) => <polyline key={i} points={s.map((p) => p.join(",")).join(" ")} fill="none" stroke={SERIES} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round" />)}
          {hover && (
            <g data-testid="trend-hover">
              <line x1={hover.px} x2={hover.px} y1={T} y2={H - B} stroke="#8b97a8" strokeDasharray="2 2" />
              {hover.p.v !== null && <circle cx={hover.px} cy={model.y(hover.p.v)} r="4" fill={SERIES} stroke="#141b24" strokeWidth="2" />}
              <rect x={hover.px > W / 2 ? hover.px - 116 : hover.px + 6} y={T + 2} width="110" height="28" rx="3" fill="#0d1117" stroke="#283444" />
              <text x={(hover.px > W / 2 ? hover.px - 116 : hover.px + 6) + 6} y={T + 13} fontSize="9" fill="#d6dde6">{fmtV(hover.p.v, hover.p.s)}</text>
              <text x={(hover.px > W / 2 ? hover.px - 116 : hover.px + 6) + 6} y={T + 25} fontSize="9" fill="#8b97a8">since {new Date(hover.p.t).toLocaleTimeString()}</text>
            </g>
          )}
        </svg>
      )}
      {model && !model.empty && table && (
        <table className="points small" data-testid="trend-values"><thead><tr><th>Since</th><th>Value</th></tr></thead>
          <tbody>{model.pts.slice(-40).reverse().map((p) => <tr key={p.t}><td className="mono">{fmtTime(p.t)}</td><td className="mono">{fmtV(p.v, p.s)}</td></tr>)}</tbody></table>
      )}
      {model && !model.empty && <div className="muted small">{model.pts.length} recorded change(s){model.gaps ? ` · ${model.gaps} unavailable period(s) shown as gaps` : ""} · current {fmtV(asNumber(data.current), data.current)}</div>}
    </div>
  );
}

/* ---------------- Printable documentation ---------------- */

const esc = (s) => String(s === undefined || s === null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function diagramSvg(sheet, sizes) {
  const blocks = sheet.blocks || [];
  if (!blocks.length) return "<p>No blocks.</p>";
  const box = (b) => ({ x: b.position.x, y: b.position.y, w: (sizes[b.id] && sizes[b.id].w) || 200, h: (sizes[b.id] && sizes[b.id].h) || 80 });
  const all = blocks.map(box);
  const minX = Math.min(...all.map((b) => b.x)) - 20, minY = Math.min(...all.map((b) => b.y)) - 20;
  const maxX = Math.max(...all.map((b) => b.x + b.w)) + 20, maxY = Math.max(...all.map((b) => b.y + b.h)) + 20;
  const by = Object.fromEntries(blocks.map((b) => [b.id, box(b)]));
  const wires = (sheet.wires || []).map((w) => {
    const a = by[w.from.block], c = by[w.to.block];
    if (!a || !c) return "";
    const x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = c.x, y2 = c.y + c.h / 2, dx = Math.max(40, Math.abs(x2 - x1) / 2);
    return `<path d="M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}" fill="none" stroke="#555" stroke-width="1.5"/>`;
  }).join("");
  const nodes = blocks.map((b) => {
    const r = by[b.id], p = b.params || {};
    const sub = p.entity_id || p.point || (p.delay_s !== undefined ? `Delay ${p.delay_s} s` : "") || (p.value !== undefined ? `Value ${p.value}` : "");
    return `<g><rect x="${r.x}" y="${r.y}" width="${r.w}" height="${r.h}" rx="4" fill="#fff" stroke="#333"/>`
      + `<text x="${r.x + 8}" y="${r.y + 15}" font-size="10" fill="#666">${esc(b.type.replace(/_/g, " ").toUpperCase())}</text>`
      + `<text x="${r.x + 8}" y="${r.y + 31}" font-size="12" font-weight="600">${esc(b.label || b.id)}</text>`
      + (sub ? `<text x="${r.x + 8}" y="${r.y + 46}" font-size="10" font-family="monospace" fill="#333">${esc(sub)}</text>` : "") + "</g>";
  }).join("");
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${minX} ${minY} ${maxX - minX} ${maxY - minY}" width="100%" style="max-height:900px">${wires}${nodes}</svg>`;
}

export function buildDocument({ sheet, sizes, info, catalog, t3Build, t3Points, user, site }) {
  const blocks = sheet.blocks || [];
  const byId = Object.fromEntries(blocks.map((b) => [b.id, b]));
  const label = (id) => (byId[id] ? byId[id].label || id : id);
  const t3 = sheet.t3000 || {};
  const points = blocks.filter((b) => ["entity_input", "t3_read", "t3_write"].includes(b.type)).map((b) => {
    const p = b.params || {};
    const tp = p.point && (t3Points || []).find((x) => `${x.category}${x.index}` === String(p.point).toUpperCase());
    return `<tr><td>${esc(label(b.id))}</td><td>${esc(b.type === "t3_write" ? "Written by program" : "Read")}</td><td class="m">${esc(p.entity_id || p.point)}</td><td class="m">${esc(tp ? tp.entity_id : p.entity_id || "")}</td><td>${esc(p.unit || (tp && tp.units) || "")}</td></tr>`;
  }).join("");
  const params = (b) => Object.entries(b.params || {}).filter(([k]) => !["justification", "sample_evidence"].includes(k)).map(([k, v]) => `${esc(k)} = ${esc(typeof v === "object" ? JSON.stringify(v) : v)}`).join("<br>");
  const blockRows = blocks.map((b) => `<tr><td class="m">${esc(b.id)}</td><td>${esc((catalog && catalog[b.type] && catalog[b.type].label) || b.type)}</td><td>${esc(b.label || "")}</td><td class="m small">${params(b)}</td></tr>`).join("");
  const wireRows = (sheet.wires || []).map((w) => `<tr><td>${esc(label(w.from.block))}</td><td class="m">${esc(w.from.port)}</td><td>${esc(label(w.to.block))}</td><td class="m">${esc(w.to.port)}</td></tr>`).join("");
  const revs = (info && info.revisions || []).slice().reverse().map((r) => `<tr><td>r${r.rev}</td><td>${esc(fmtTime(Date.parse(r.saved_at)))}</td><td>${esc(r.saved_by)}</td><td>${esc(r.note)}</td></tr>`).join("");
  const latest = info && info.revisions && info.revisions.length ? info.revisions[info.revisions.length - 1].rev : null;
  const t3Section = sheet.mode === "t3000" ? `
<h2>T3000 Program</h2>
<table class="kv"><tr><th>Controller (BACnet instance)</th><td>${esc(t3.device_instance)}</td></tr><tr><th>Program slot</th><td>${esc(t3.program_slot)}</td></tr>
<tr><th>Work variables</th><td>${esc((t3.work_vars || []).join(", ") || "—")}</td></tr><tr><th>Origin</th><td>${t3.imported ? "Generated from the controller program (documentation only)" : "Written in BAS Wire Sheet"}</td></tr>
${t3Build && t3Build.sha256 ? `<tr><th>Compiled size / SHA-256</th><td>${esc(t3Build.length)} bytes · <span class="m">${esc(t3Build.sha256)}</span></td></tr>` : ""}</table>
${t3Build && t3Build.source ? `<h3>Control Basic listing</h3><pre>${esc(t3Build.source)}</pre>` : ""}` : "";
  const equip = (sheet.equipment || []).map((e) => `<li>${esc(e.tag)}: ${esc(e.status)} since ${esc(e.since)}</li>`).join("");
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><title>${esc(sheet.name)} - Wire Sheet Documentation</title>
<style>body{font:12px/1.45 "Segoe UI",Roboto,Arial,sans-serif;color:#111;margin:24px 32px}h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:22px 0 6px;border-bottom:1px solid #999;padding-bottom:2px}h3{font-size:13px}
table{border-collapse:collapse;width:100%;margin:4px 0}th,td{border:1px solid #bbb;padding:3px 6px;text-align:left;vertical-align:top}th{background:#eee}.m{font-family:Consolas,monospace}.small{font-size:10px}
pre{background:#f4f4f4;border:1px solid #ccc;padding:8px;font-size:11px;white-space:pre-wrap}.kv th{width:220px}.muted{color:#555}.noprint{margin-bottom:12px}@media print{.noprint{display:none}h2{break-after:avoid}table,svg{break-inside:auto}}</style></head><body>
<div class="noprint"><button onclick="window.print()">Print / Save as PDF</button></div>
<h1>${esc(sheet.name)}</h1>
<div class="muted">BAS Wire Sheet documentation · ${esc(site || "Home Assistant")} · generated ${esc(new Date().toLocaleString())}${user ? ` by ${esc(user)}` : ""}</div>
<h2>Summary</h2>
<table class="kv"><tr><th>Sheet id</th><td class="m">${esc(sheet.id)}</td></tr><tr><th>Mode</th><td>${sheet.mode === "t3000" ? "T3000 controller program" : "Monitoring only"}</td></tr>
<tr><th>Latest revision</th><td>${latest ? `r${latest}` : "None (draft only)"}</td></tr><tr><th>Draft saved</th><td>${info ? `${esc(fmtTime(Date.parse(info.draft_updated_at)))} by ${esc(info.draft_updated_by)}` : "—"}</td></tr>
<tr><th>Blocks / wires</th><td>${blocks.length} / ${(sheet.wires || []).length}</td></tr></table>
${sheet.description ? `<p>${esc(sheet.description)}</p>` : ""}${equip ? `<p><b>Equipment status</b></p><ul>${equip}</ul>` : ""}
<h2>Diagram</h2>${diagramSvg(sheet, sizes || {})}
<h2>Points</h2><table><tr><th>Block</th><th>Use</th><th>Entity / point</th><th>Home Assistant entity</th><th>Unit</th></tr>${points || '<tr><td colspan="5">None</td></tr>'}</table>
${t3Section}
<h2>Blocks</h2><table><tr><th>Id</th><th>Type</th><th>Label</th><th>Parameters</th></tr>${blockRows}</table>
<h2>Connections</h2><table><tr><th>From block</th><th>Output</th><th>To block</th><th>Input</th></tr>${wireRows || '<tr><td colspan="4">None</td></tr>'}</table>
<h2>Revision history</h2><table><tr><th>Revision</th><th>Saved</th><th>By</th><th>Note</th></tr>${revs || '<tr><td colspan="4">No revisions</td></tr>'}</table>
<p class="muted small">Values in this document describe the configuration only. Live readings are not included.</p>
</body></html>`;
}

/* ---------------- Run in Home Assistant ---------------- */

export function SheetRuntime({ ws, sheetId, info, role, latestRev, mode, onChanged }) {
  const [st, setSt] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const load = () => ws({ type: "bas_wiresheet/sheet_run_status" }).then((r) => setSt(r.sheets[sheetId] || null)).catch(() => {});
  useEffect(() => { load(); const h = setInterval(load, 5000); return () => clearInterval(h); }, [sheetId]); // eslint-disable-line
  if (mode !== "monitoring") return null;
  const hr = info && info.ha_runtime;
  const act = async (enabled) => {
    setBusy(true); setErr(null);
    try { await ws({ type: "bas_wiresheet/sheet_run", sheet_id: sheetId, enabled }); await load(); await onChanged(); } catch (e) { setErr(e.message); }
    setBusy(false);
  };
  const can = role === "editor" && info && info.can_edit !== false;
  return (
    <div data-testid="sheet-runtime">
      <div className="pane-h sub">Run in Home Assistant</div>
      {st ? <>
        <div className="kv"><span>Status</span><b data-testid="sheet-runtime-status">Running r{st.revision} · {st.status}</b></div>
        <div className="kv"><span>Sensor</span><b className="mono small">sensor.bas_wiresheet_{sheetId.replace(/-/g, "_")}</b></div>
        <div className="muted small">{st.notifications ? "Notifies when a point stays Stale, Unavailable or without telemetry for 60 s." : "No notifications: the sheet contains Commissioning Pending equipment."}</div>
      </> : <div className="muted small">{hr ? "Enabled, starting…" : "Not running. Live Monitoring is calculated only while the panel is open."}</div>}
      {can && <div className="row" style={{ gap: 6, marginTop: 4 }}>
        {(!st || (latestRev && st.revision !== latestRev)) && <button className="btn sm primary" disabled={busy || !latestRev} onClick={() => act(true)} data-testid="sheet-runtime-start" title={latestRev ? "" : "Save a revision first"}>{st ? `Run r${latestRev}` : "Run Continuously"}</button>}
        {st && <button className="btn sm" disabled={busy} onClick={() => act(false)} data-testid="sheet-runtime-stop">Stop</button>}
      </div>}
      {err && <div className="errtxt small">{err}</div>}
    </div>
  );
}

/* ---------------- New from Template ---------------- */

export function templateSheet(tpl, id, name, values) {
  const skip = new Set(tpl.template.inputs.filter((i) => i.optional && !values[i.key]).map((i) => i.key));
  let txt = JSON.stringify(tpl.sheet);
  tpl.template.inputs.forEach((i) => { if (!skip.has(i.key)) txt = txt.split(`{{${i.key}}}`).join(values[i.key]); });
  const s = JSON.parse(txt);
  const drop = new Set(s.blocks.filter((b) => [...skip].some((k) => b.id.endsWith(`_${k}`))).map((b) => b.id));
  s.blocks = s.blocks.filter((b) => !drop.has(b.id));
  s.wires = s.wires.filter((w) => !drop.has(w.from.block) && !drop.has(w.to.block));
  return { ...s, id, name, description: `${tpl.template.description} Created from template "${tpl.template.name}".` };
}

export function TemplateDialog({ ws, hass, onClose, onCreate, Modal }) {
  const [list, setList] = useState(null);
  const [sel, setSel] = useState(null);
  const [name, setName] = useState("");
  const [values, setValues] = useState({});
  const [err, setErr] = useState(null);
  useEffect(() => { ws({ type: "bas_wiresheet/templates" }).then((r) => { setList(r.templates); if (r.templates.length) setSel(r.templates[0].template.id); }).catch((e) => setErr(e.message)); }, []); // eslint-disable-line
  const tpl = list && list.find((t) => t.template.id === sel);
  const id = name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60);
  const missing = tpl ? tpl.template.inputs.filter((i) => !i.optional && !values[i.key]) : [];
  const unknown = tpl ? tpl.template.inputs.filter((i) => values[i.key] && !hass.states[values[i.key]]) : [];
  const ready = tpl && id && !missing.length && !unknown.length;
  return (
    <Modal title="New Sheet from Template" onClose={onClose} actions={<button className="btn primary" disabled={!ready} onClick={() => onCreate(templateSheet(tpl, id, name.trim(), values)).catch((e) => setErr(e.message))} data-testid="tpl-create">Create Sheet</button>}>
      {err && <div className="errtxt">{err}</div>}
      {list && <>
        <label className="field"><span>Template</span><select value={sel || ""} onChange={(e) => { setSel(e.target.value); setValues({}); }} data-testid="tpl-select">
          {list.map((t) => <option key={t.template.id} value={t.template.id}>{t.template.name}</option>)}</select></label>
        {tpl && <p className="muted small">{tpl.template.description}</p>}
        <label className="field"><span>Sheet name</span><input value={name} onChange={(e) => setName(e.target.value)} placeholder="HX-1 Monitoring" data-testid="tpl-name" /><small className="mono">{id || "—"}</small></label>
        <datalist id="tpl-entities">{Object.keys(hass.states).sort().map((k) => <option key={k} value={k}>{hass.states[k].attributes.friendly_name}</option>)}</datalist>
        {tpl && tpl.template.inputs.map((i) => (
          <label key={i.key} className="field"><span>{i.label}{i.optional ? " (optional)" : ""}</span>
            <input list="tpl-entities" value={values[i.key] || ""} onChange={(e) => setValues({ ...values, [i.key]: e.target.value.trim() })} placeholder="sensor.…" data-testid={`tpl-in-${i.key}`} />
            {values[i.key] && !hass.states[values[i.key]] && <small className="errtxt">Not a Home Assistant entity</small>}
          </label>
        ))}
        <p className="muted small">The sheet is created as a draft: check stale times and limits, then Save Revision. Monitoring only; nothing is written to equipment.</p>
      </>}
    </Modal>
  );
}
