import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App.jsx";
import flowCss from "@xyflow/react/dist/style.css";
import appCss from "./app.css";

class BasWireSheetPanel extends HTMLElement {
  constructor() {
    super();
    this._hass = null;
    this._root = null;
  }
  connectedCallback() {
    if (this._root) return;
    const shadow = this.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = flowCss + "\n" + appCss;
    shadow.appendChild(style);
    const mount = document.createElement("div");
    mount.className = "bws-root";
    shadow.appendChild(mount);
    this._root = createRoot(mount);
    this._render();
  }
  disconnectedCallback() {
    if (this._root) { this._root.unmount(); this._root = null; }
  }
  set hass(h) {
    const first = !this._hass;
    this._hass = h;
    // re-render on state changes only every second at most (hass updates are frequent on a BMS)
    if (first) this._render();
    else if (!this._pending) { this._pending = setTimeout(() => { this._pending = null; this._render(); }, 1000); }
  }
  get hass() { return this._hass; }
  set panel(p) { this._panel = p; }
  set narrow(n) { this._narrow = n; }
  _render() {
    if (this._root && this._hass) this._root.render(<App hass={this._hass} />);
  }
}
if (!customElements.get("bas-wiresheet-panel")) customElements.define("bas-wiresheet-panel", BasWireSheetPanel);
