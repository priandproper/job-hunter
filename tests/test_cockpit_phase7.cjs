/* Phase 7 tests — cockpit metrics (pure functions). Run: node tests/test_cockpit_phase7.cjs */
"use strict";
const assert = require("assert");
const CM = require("../docs/cockpit_metrics.js");

const tests = {};
function test(name, fn) { tests[name] = fn; }

// 1) Daily plan reflects the 2A / 4B / 5 outreach / 1 OPT model, capped by availability.
test("daily_plan_defaults_and_capping", () => {
  const p = CM.dailyPlan(null, { availA: 5, availB: 5 });
  assert.deepStrictEqual(p, { a: 2, b: 4, outreach: 5, opt: 1 });
  const capped = CM.dailyPlan(null, { availA: 1, availB: 2 });
  assert.strictEqual(capped.a, 1);
  assert.strictEqual(capped.b, 2);
});

// 2) Targets are configurable (overrides merge over defaults).
test("targets_configurable", () => {
  const p = CM.dailyPlan({ dailyA: 3, dailyOutreach: 8 }, { availA: 9, availB: 9 });
  assert.strictEqual(p.a, 3);
  assert.strictEqual(p.outreach, 8);
  assert.strictEqual(p.b, 4); // untouched default
  assert.strictEqual(CM.mergeTargets({ monthly: 150 }).monthly, 150);
});

// 3) Pace: ahead / on track / behind against the 120 monthly target.
test("pace_status", () => {
  assert.strictEqual(CM.paceStatus(120, 120).status, "ahead");
  assert.strictEqual(CM.paceStatus(130, 120).status, "ahead");
  assert.strictEqual(CM.paceStatus(100, 120).status, "on track"); // 0.83
  assert.strictEqual(CM.paceStatus(50, 120).status, "behind");
  assert.strictEqual(CM.paceStatus(96, 120).pct, 80);
});

// 4) Qualified application: gates on band, immigration, quals alignment, submission.
function job(band, imm, basicFrac) {
  return {
    priority: { band, components: [{ name: "basic_qualifications", points: basicFrac * 25, max: 25 }] },
    immigration: { risk: imm },
  };
}
test("qualified_application_gates", () => {
  assert.strictEqual(CM.qualifiedApplication(job("A", "green", 0.8), { applied: true }), true);
  assert.strictEqual(CM.qualifiedApplication(job("A", "green", 0.8), { applied: false }), false); // not submitted
  assert.strictEqual(CM.qualifiedApplication(job("Reject", "green", 0.8), { applied: true }), false); // off-lane
  assert.strictEqual(CM.qualifiedApplication(job("A", "red", 0.8), { applied: true }), false); // hard immigration
  assert.strictEqual(CM.qualifiedApplication(job("B", "yellow", 0.3), { applied: true }), false); // quals misalign
  assert.strictEqual(CM.qualifiedApplication(job("B", "yellow", 0.6), { applied: true, validated: false }), false); // validation failed
  assert.strictEqual(CM.qualifiedApplication(job("B", "yellow", 0.6), { applied: true, validated: true }), true);
});

// 5) Sponsor mix counts green/yellow/red (unknown -> yellow).
test("sponsor_mix", () => {
  const m = CM.sponsorMix([
    { immigration: { risk: "green" } }, { immigration: { risk: "green" } },
    { immigration: { risk: "yellow" } }, { immigration: { risk: "red" } }, {},
  ]);
  assert.deepStrictEqual(m, { green: 2, yellow: 2, red: 1 });
});

// 6) Warm vs cold split by referral availability.
test("warm_cold_split", () => {
  const jobs = [{ id: 1 }, { id: 2 }, { id: 3 }];
  const s = CM.warmColdSplit(jobs, (j) => j.id !== 2);
  assert.deepStrictEqual(s, { warm: 2, cold: 1 });
});

// 7) Application-to-call conversion.
test("conversion", () => {
  assert.strictEqual(CM.conversion(20, 5).pct, 25);
  assert.strictEqual(CM.conversion(0, 0).pct, 0);
});

let failed = 0;
Object.keys(tests).sort().forEach((name) => {
  try { tests[name](); console.log("  ok   " + name); }
  catch (e) { failed++; console.log("  FAIL " + name + ": " + e.message); }
});
console.log("\n" + (Object.keys(tests).length - failed) + "/" + Object.keys(tests).length + " passed");
process.exit(failed ? 1 : 0);
