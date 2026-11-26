import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
// poll.js is an ES module bundled by esbuild; the repository has no "type": "module", so load it from source
const src = readFileSync(new URL("../frontend/src/poll.js", import.meta.url), "utf8");
const { pollLoop } = await import("data:text/javascript," + encodeURIComponent(src));

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

test("polls never overlap and slow reads space the next one out", async () => {
  let running = 0, maxRunning = 0, calls = 0;
  const starts = [];
  const cancel = pollLoop(async () => {
    calls += 1; running += 1; maxRunning = Math.max(maxRunning, running); starts.push(Date.now());
    await sleep(60);                                   // a slow controller read
    running -= 1;
  }, 10);
  await sleep(500);
  cancel();
  const n = calls;
  await sleep(200);
  assert.equal(maxRunning, 1, "never two reads at the same time");
  assert.ok(n >= 2 && n <= 4, `about every 2 x 60 ms + 60 ms, got ${n}`);
  for (let i = 1; i < starts.length; i++) assert.ok(starts[i] - starts[i - 1] >= 175, "next read waits twice the last read time");
  assert.equal(calls, n, "no read after cancel");
});

test("a failing read does not stop the loop", async () => {
  let calls = 0;
  const cancel = pollLoop(async () => { calls += 1; throw new Error("busy"); }, 20);
  await sleep(130);
  cancel();
  assert.ok(calls >= 3, `kept polling, got ${calls}`);
});
