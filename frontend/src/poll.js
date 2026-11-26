// Polling that never overlaps: the next read starts only after the previous one finished, at least `ms` later and at
// least twice as long as the last read took (a slow controller gets fewer requests), and not while the tab is hidden.
export function pollLoop(fn, ms, { immediate = true } = {}) {
  let stop = false, timer = null;
  const run = async () => {
    if (stop) return;
    if (typeof document !== "undefined" && document.hidden) { timer = setTimeout(run, ms); return; }
    const t0 = Date.now();
    try { await fn(); } catch { /* the caller shows its own errors */ }
    if (!stop) timer = setTimeout(run, Math.max(ms, 2 * (Date.now() - t0)));
  };
  if (immediate) run(); else timer = setTimeout(run, ms);
  return () => { stop = true; if (timer) clearTimeout(timer); };
}
