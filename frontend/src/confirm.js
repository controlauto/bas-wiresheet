// Typed confirmations. Off when the operator turned the integration's restrictions off (t3000: unrestricted).
export const CONFIRM = { on: true };
export const typedOk = (typed, need) => !CONFIRM.on || String(typed || "").trim().toUpperCase() === String(need).toUpperCase();
