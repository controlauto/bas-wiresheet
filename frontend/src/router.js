// Orthogonal wire auto-routing (Visuino-like): wires leave outputs to the right, enter inputs from the left, run in
// horizontal/vertical segments around blocks, avoid lanes already used by other wires and keep bends to a minimum.
// Grid A* with direction in the state; obstacles are the block rectangles plus a margin.

const DIRS = [[1, 0], [0, 1], [-1, 0], [0, -1]]; // E S W N

class Heap {
  constructor() { this.a = []; }
  push(x) { const a = this.a; a.push(x); let i = a.length - 1; while (i > 0) { const p = (i - 1) >> 1; if (a[p][0] <= a[i][0]) break; [a[p], a[i]] = [a[i], a[p]]; i = p; } }
  pop() {
    const a = this.a, top = a[0], last = a.pop();
    if (a.length) { a[0] = last; let i = 0; for (;;) { const l = 2 * i + 1, r = l + 1; let m = i; if (l < a.length && a[l][0] < a[m][0]) m = l; if (r < a.length && a[r][0] < a[m][0]) m = r; if (m === i) break; [a[m], a[i]] = [a[i], a[m]]; i = m; } }
    return top;
  }
  get size() { return this.a.length; }
}

/** Simple 3-segment orthogonal fallback (used while dragging or when no route is found). */
export function elbowPoints(sx, sy, tx, ty, stub = 16) {
  const a = [sx, sy], b = [sx + stub, sy], z = [tx, ty], y = [tx - stub, ty];
  if (y[0] >= b[0]) { const mx = Math.round((b[0] + y[0]) / 2); return [a, b, [mx, sy], [mx, ty], y, z]; }
  const my = Math.round((sy + ty) / 2);
  return [a, b, [b[0], my], [y[0], my], y, z];
}

/** Rounded-corner SVG path through orthogonal points. */
export function roundedPath(pts, r = 6) {
  const p = simplify(pts);
  if (p.length < 2) return "";
  let d = `M${p[0][0]},${p[0][1]}`;
  for (let i = 1; i < p.length - 1; i++) {
    const [x0, y0] = p[i - 1], [x1, y1] = p[i], [x2, y2] = p[i + 1];
    const l1 = Math.hypot(x1 - x0, y1 - y0), l2 = Math.hypot(x2 - x1, y2 - y1);
    const rr = Math.min(r, l1 / 2, l2 / 2);
    const ax = x1 - ((x1 - x0) / (l1 || 1)) * rr, ay = y1 - ((y1 - y0) / (l1 || 1)) * rr;
    const bx = x1 + ((x2 - x1) / (l2 || 1)) * rr, by = y1 + ((y2 - y1) / (l2 || 1)) * rr;
    d += ` L${ax},${ay} Q${x1},${y1} ${bx},${by}`;
  }
  const last = p[p.length - 1];
  return `${d} L${last[0]},${last[1]}`;
}

export function simplify(pts) {
  const out = [];
  for (const q of pts) {
    if (out.length && out[out.length - 1][0] === q[0] && out[out.length - 1][1] === q[1]) continue;
    out.push(q);
    while (out.length >= 3) {
      const [a, b, c] = out.slice(-3);
      if ((a[0] === b[0] && b[0] === c[0]) || (a[1] === b[1] && b[1] === c[1])) out.splice(out.length - 2, 1);
      else break;
    }
  }
  return out;
}

/** Label position: middle of the longest segment. */
export function labelPoint(pts) {
  const p = simplify(pts);
  let best = [p[0][0], p[0][1]], len = -1;
  for (let i = 1; i < p.length; i++) {
    const l = Math.abs(p[i][0] - p[i - 1][0]) + Math.abs(p[i][1] - p[i - 1][1]);
    if (l > len) { len = l; best = [(p[i][0] + p[i - 1][0]) / 2, (p[i][1] + p[i - 1][1]) / 2]; }
  }
  return best;
}

/**
 * Route every wire.
 * rects: [{id, x, y, w, h}] block rectangles (flow coordinates)
 * wires: [{id, sx, sy, tx, ty, source, target}] handle points
 * Returns {id: points[]}.
 */
export function routeAll(rects, wires, opts = {}) {
  const G = opts.grid || 10, M = opts.margin || 12, STUB = opts.stub || 16;
  const BEND = opts.bendCost || 5, SHARE = opts.shareCost || 4, NEAR = opts.nearCost || 1;
  const out = {};
  if (!rects.length || !wires.length) return out;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const r of rects) { minX = Math.min(minX, r.x); minY = Math.min(minY, r.y); maxX = Math.max(maxX, r.x + r.w); maxY = Math.max(maxY, r.y + r.h); }
  for (const w of wires) { minX = Math.min(minX, w.sx, w.tx); minY = Math.min(minY, w.sy, w.ty); maxX = Math.max(maxX, w.sx, w.tx); maxY = Math.max(maxY, w.sy, w.ty); }
  minX -= 120; minY -= 120; maxX += 120; maxY += 120;
  const W = Math.ceil((maxX - minX) / G) + 1, H = Math.ceil((maxY - minY) / G) + 1;
  if (W * H > (opts.maxCells || 600000)) return null; // too big: caller falls back to elbows
  const cx = (x) => Math.round((x - minX) / G), cy = (y) => Math.round((y - minY) / G);
  const px = (i) => minX + i * G, py = (j) => minY + j * G;
  const blocked = new Uint8Array(W * H), near = new Uint8Array(W * H), used = new Uint16Array(W * H * 2), owner = new Int32Array(W * H * 2); // used: [h, v]; owner: source port of the lane
  const srcIds = {}; let srcN = 0;
  const srcKey = (w) => { const k = `${w.source}|${w.sx}|${w.sy}`; return srcIds[k] || (srcIds[k] = ++srcN); };
  for (const r of rects) {
    const x0 = Math.max(0, cx(r.x - M)), x1 = Math.min(W - 1, cx(r.x + r.w + M)), y0 = Math.max(0, cy(r.y - M)), y1 = Math.min(H - 1, cy(r.y + r.h + M));
    for (let j = y0; j <= y1; j++) for (let i = x0; i <= x1; i++) blocked[j * W + i] = 1;
    const nx0 = Math.max(0, x0 - 2), nx1 = Math.min(W - 1, x1 + 2), ny0 = Math.max(0, y0 - 2), ny1 = Math.min(H - 1, y1 + 2);
    for (let j = ny0; j <= ny1; j++) for (let i = nx0; i <= nx1; i++) near[j * W + i] = 1;
  }
  // longest wires first: they need the most freedom
  const order = [...wires].sort((a, b) => (Math.abs(b.tx - b.sx) + Math.abs(b.ty - b.sy)) - (Math.abs(a.tx - a.sx) + Math.abs(a.ty - a.sy)) || (a.id < b.id ? -1 : 1));
  const dist = new Float64Array(W * H * 4), stamp = new Uint32Array(W * H * 4), prev = new Int32Array(W * H * 4);
  let gen = 0;
  for (const w of order) {
    gen += 1;
    const me = srcKey(w);
    const si = cx(w.sx + STUB), sj = cy(w.sy), ti = cx(w.tx - STUB), tj = cy(w.ty);
    if (si < 0 || sj < 0 || ti < 0 || tj < 0 || si >= W || ti >= W || sj >= H || tj >= H) { out[w.id] = elbowPoints(w.sx, w.sy, w.tx, w.ty, STUB); continue; }
    // allow the stub cells even inside the margin of their own block
    const free = (i, j) => !blocked[j * W + i] || (j === sj && Math.abs(i - si) <= 1) || (j === tj && Math.abs(i - ti) <= 1);
    const h = (i, j) => Math.abs(i - ti) + Math.abs(j - tj);
    const heap = new Heap();
    const start = (sj * W + si) * 4 + 0; // heading east
    dist[start] = 0; stamp[start] = gen; prev[start] = -1;
    heap.push([h(si, sj), start]);
    let found = -1, expanded = 0;
    const limit = opts.maxExpand || 60000;
    while (heap.size && expanded < limit) {
      const [f, s] = heap.pop();
      const d = dist[s], cell = s >> 2, dir = s & 3, i = cell % W, j = (cell / W) | 0;
      if (f - h(i, j) > d + 1e-9) continue;
      expanded += 1;
      if (i === ti && j === tj && dir === 0) { found = s; break; }
      for (let nd = 0; nd < 4; nd++) {
        if (nd === ((dir + 2) & 3)) continue; // no U-turn
        const ni = i + DIRS[nd][0], nj = j + DIRS[nd][1];
        if (ni < 0 || nj < 0 || ni >= W || nj >= H || !free(ni, nj)) continue;
        const nc = nj * W + ni, axis = nd & 1;
        // a lane already used by the same output is free (fan-out runs as one bus, like Visuino); other wires keep apart
        const sameBus = owner[nc * 2 + axis] === me;
        let cost = (sameBus ? 0.6 : 1) + (nd !== dir ? BEND : 0) + (sameBus ? 0 : used[nc * 2 + axis] * SHARE) + used[nc * 2 + (1 - axis)] * 0.3 + (near[nc] && !sameBus ? NEAR : 0);
        if (ni === ti && nj === tj && nd !== 0) cost += BEND; // arrive heading east into the input
        const ns = nc * 4 + nd, nd2 = d + cost;
        if (stamp[ns] !== gen || nd2 < dist[ns]) { stamp[ns] = gen; dist[ns] = nd2; prev[ns] = s; heap.push([nd2 + h(ni, nj), ns]); }
      }
    }
    if (found < 0) { out[w.id] = elbowPoints(w.sx, w.sy, w.tx, w.ty, STUB); continue; }
    const cells = [];
    for (let s = found; s >= 0; s = prev[s]) { const cell = s >> 2; cells.push([cell % W, (cell / W) | 0, s & 3]); if (s === start) break; }
    cells.reverse();
    for (const [i, j, d] of cells) { const k = (j * W + i) * 2 + (d & 1); if (owner[k] !== me) { used[k] += 1; owner[k] = owner[k] ? -1 : me; } }
    const pts = [[w.sx, w.sy], [px(si), w.sy]];
    for (const [i, j] of cells) pts.push([px(i), py(j)]);
    pts.push([px(ti), w.ty], [w.tx, w.ty]);
    // snap the first/last grid points to the exact handle rows
    pts[2] = [pts[2][0], w.sy]; pts[pts.length - 3] = [pts[pts.length - 3][0], w.ty];
    out[w.id] = simplify(orthogonalize(pts));
  }
  return out;
}

// grid snapping can leave tiny diagonal steps near the handles: turn them into right angles
function orthogonalize(pts) {
  const out = [pts[0]];
  for (let k = 1; k < pts.length; k++) {
    const [x0, y0] = out[out.length - 1], [x1, y1] = pts[k];
    if (x0 !== x1 && y0 !== y1) out.push([x1, y0]);
    out.push([x1, y1]);
  }
  return out;
}

/** Layered left-to-right layout: column = longest path from a source, rows ordered by the mean row of the inputs. */
export function tidyLayout(nodes, edges, opts = {}) {
  const gapX = opts.gapX || 150, gapY = opts.gapY || 46;
  const ids = nodes.map((n) => n.id), byId = Object.fromEntries(nodes.map((n) => [n.id, n]));
  const ins = Object.fromEntries(ids.map((i) => [i, []])), outs = Object.fromEntries(ids.map((i) => [i, []]));
  edges.forEach((e) => { if (ins[e.target] && outs[e.source]) { ins[e.target].push(e.source); outs[e.source].push(e.target); } });
  const layer = {}, visiting = new Set();
  const depth = (id) => {
    if (layer[id] !== undefined) return layer[id];
    if (visiting.has(id)) return 0;
    visiting.add(id);
    layer[id] = ins[id].length ? Math.max(...ins[id].map(depth)) + 1 : 0;
    return layer[id];
  };
  ids.forEach(depth);
  const cols = {};
  ids.forEach((id) => { (cols[layer[id]] = cols[layer[id]] || []).push(id); });
  const keys = Object.keys(cols).map(Number).sort((a, b) => a - b);
  const rowOf = {};
  keys.forEach((k) => {
    const col = cols[k];
    col.sort((a, b) => {
      const ma = ins[a].length ? ins[a].reduce((s, x) => s + (rowOf[x] ?? 0), 0) / ins[a].length : byId[a].position.y;
      const mb = ins[b].length ? ins[b].reduce((s, x) => s + (rowOf[x] ?? 0), 0) / ins[b].length : byId[b].position.y;
      return ma - mb;
    });
    let y = 0;
    col.forEach((id) => { rowOf[id] = y; const h = (byId[id].measured && byId[id].measured.height) || 90; y += h + gapY; });
  });
  const colX = {}; let x = 0;
  keys.forEach((k) => { colX[k] = x; x += Math.max(...cols[k].map((id) => (byId[id].measured && byId[id].measured.width) || 200)) + gapX; });
  return Object.fromEntries(ids.map((id) => [id, { x: colX[layer[id]], y: rowOf[id] }]));
}
