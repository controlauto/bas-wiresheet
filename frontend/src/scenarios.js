// Bundled copies of ../../scenarios/*.json (same files the node tests use).
import a from "../../scenarios/stable-temp-every-sample.json";
import b from "../../scenarios/stable-temp-on-change.json";
import c from "../../scenarios/report-loss-and-recovery.json";
import d from "../../scenarios/invalid-data.json";
import e from "../../scenarios/source-unavailable.json";
import f from "../../scenarios/missing-timestamp.json";
import g from "../../scenarios/heartbeat-countdown.json";
const all = [a, b, c, d, e, f, g];
export default Object.fromEntries(all.map((s) => [s.name, s]));
