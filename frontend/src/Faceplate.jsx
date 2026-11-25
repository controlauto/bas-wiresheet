// Controller faceplates for T3000 program sheets: the controller's physical inputs and outputs drawn as terminal
// strips (label, live value, status light, hand/auto switch), its ports and status lights. Wires attach to terminals:
// a wire from an input terminal reads that input, a wire into an output terminal writes that output.
import React from "react";
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
          const row = rowFor(c.t3Points, p.pin);
          const label = (row && (own(row.label, p.pin) || own(row.description, p.pin))) || (kind === "IN" ? "" : p.io === "do" ? "Digital" : "Analog");
          const val = pinValue(c, id, p.pin, row, p.io);
          const manual = row && row.auto_manual === "Manual";
          const t = types[p.pin];
          const color = t && c.catalog.port_types[t] ? c.catalog.port_types[t].color : "#8b97a8";
          const wired = used.has(p.pin);
          const cls = `fp-row ${wired ? "wired" : ""} ${badPins.has(p.pin) ? "bad" : ""} ${val.stale ? "stale" : ""}`;
          const tip = `${p.pin}${label ? ` ${label}` : ""}${row ? ` · ${row.value}${row.units ? ` ${row.units}` : ""} · ${row.auto_manual}` : ""}${t ? ` · wired as ${t}` : ""}`;
          if (kind === "IN") {
            return (
              <div key={p.pin} className={cls} title={tip} data-testid={`pin-${id}-${p.pin}`}>
                <Screws />
                <span className={`fp-led blue ${val.on ? "lit" : ""}`} />
                <span className="fp-strip"><b className="fp-n">{p.n}</b><span className="fp-lbl">{label || p.pin}</span></span>
                <span className={`fp-lcd ${val.cls}`}>{val.text}</span>
                <Handle type="source" position={Position.Right} id={p.pin} className="fp-h" style={{ background: wired ? color : "#3a4a5f" }} />
              </div>
            );
          }
          return (
            <div key={p.pin} className={cls} title={tip} data-testid={`pin-${id}-${p.pin}`}>
              <Handle type="target" position={Position.Left} id={p.pin} className="fp-h" style={{ background: wired ? color : "#3a4a5f" }} />
              <span className="fp-strip"><b className="fp-n">{p.n}</b><span className="fp-lbl">{label}</span></span>
              <span className={`fp-lcd ${val.cls}`}>{val.text}</span>
              <span className={`fp-hoa ${manual ? "hand" : ""}`} title={manual ? "Manual (hand) on the controller" : "Auto"}><i /></span>
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
