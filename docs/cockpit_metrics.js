/* Phase 7 — cockpit metrics (pure functions).
 *
 * The daily operating model is 120 qualified applications / 30 days. These helpers
 * compute the plan, the pace, the qualified-application test, and the mix/conversion
 * indicators from plain data — no DOM, no localStorage — so they are unit-testable
 * (tests/test_cockpit_phase7.cjs, run under node) and shared by the dashboard, which
 * gathers the browser state and hands it in. Loaded in the browser as a global
 * (window.CockpitMetrics) and in node via require().
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.CockpitMetrics = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // The 30-day operating goal and the daily queue mix (all configurable).
  var DEFAULT_TARGETS = {
    dailyA: 2,          // Priority-A applications / day
    dailyB: 4,          // Priority-B applications / day
    dailyOutreach: 5,   // outreach / follow-up actions / day
    dailyOpt: 1,        // interim OPT-employment actions / day
    weekly: 30,         // qualified applications / week
    monthly: 120,       // qualified applications / 30 days
  };

  function mergeTargets(over) {
    var t = {};
    for (var k in DEFAULT_TARGETS) t[k] = DEFAULT_TARGETS[k];
    if (over) for (var j in over) if (over[j] != null && !isNaN(over[j])) t[j] = Number(over[j]);
    return t;
  }

  // What to do today: fixed targets, apply-slots capped by how many A/B roles are
  // actually available to apply to (so we never ask for more than exist).
  function dailyPlan(targets, avail) {
    targets = mergeTargets(targets);
    avail = avail || {};
    return {
      a: Math.min(targets.dailyA, avail.availA == null ? targets.dailyA : avail.availA),
      b: Math.min(targets.dailyB, avail.availB == null ? targets.dailyB : avail.availB),
      outreach: targets.dailyOutreach,
      opt: targets.dailyOpt,
    };
  }

  // Pace against the rolling 30-day target. Qualitative on purpose.
  function paceStatus(rolling30, monthlyTarget) {
    var target = monthlyTarget || DEFAULT_TARGETS.monthly;
    var pct = target ? rolling30 / target : 0;
    var status = pct >= 1.0 ? "ahead" : pct >= 0.8 ? "on track" : "behind";
    return { status: status, pct: Math.round(pct * 100), rolling30: rolling30, target: target };
  }

  // A recorded application counts as QUALIFIED only if it clears the gates we can
  // check from the job + application record. Approved lane + reasonable basic-qual
  // alignment + no hard immigration prohibition + submission recorded. Résumé-template
  // and validation gates apply only when the record carries them (optional today).
  function qualifiedApplication(job, appState) {
    appState = appState || {};
    if (!appState.applied) return false;                       // submission recorded
    var p = job && job.priority;
    if (!p) return false;
    if (p.band === "Reject") return false;                     // off-lane / hard issue
    var imm = (job.immigration && job.immigration.risk) || "yellow";
    if (imm === "red") return false;                           // hard immigration prohibition
    var basic = (p.components || []).filter(function (c) { return c.name === "basic_qualifications"; })[0];
    if (basic && basic.max && basic.points / basic.max < 0.5) return false;  // quals must reasonably align
    if (appState.validated === false) return false;            // if tracked, validation must pass
    return true;
  }

  function sponsorMix(jobs) {
    var m = { green: 0, yellow: 0, red: 0 };
    (jobs || []).forEach(function (j) {
      var r = (j.immigration && j.immigration.risk) || "yellow";
      if (m[r] == null) r = "yellow";
      m[r]++;
    });
    return m;
  }

  // warm = the job had a referral path at apply time; cold = none. hasReferral(job)->bool.
  function warmColdSplit(appliedJobs, hasReferral) {
    var w = 0, c = 0;
    (appliedJobs || []).forEach(function (j) { if (hasReferral(j)) w++; else c++; });
    return { warm: w, cold: c };
  }

  // application -> call (recruiter screen/interview) conversion.
  function conversion(applications, calls) {
    var pct = applications ? calls / applications : 0;
    return { applications: applications, calls: calls, ratio: pct, pct: Math.round(pct * 100) };
  }

  return {
    DEFAULT_TARGETS: DEFAULT_TARGETS, mergeTargets: mergeTargets, dailyPlan: dailyPlan,
    paceStatus: paceStatus, qualifiedApplication: qualifiedApplication,
    sponsorMix: sponsorMix, warmColdSplit: warmColdSplit, conversion: conversion,
  };
});
