#!/usr/bin/env python3
"""Funnel analytics report (Phase 12) from your exported application state.

Reads data/state.local.json (export it from the dashboard) + docs/jobs.json, computes
the funnel — per-application dimensions, conversion rates overall and by lane / résumé /
source / warm-cold / sponsorship / posting-age / experience — and writes a Markdown
report to data/funnel-report.md (git-ignored). After every 30 applications it appends
the diagnostic (what's converting, where the funnel fails, which lane needs volume, …).

  python3 scripts/funnel.py            # write data/funnel-report.md
  python3 scripts/funnel.py --print    # also print it
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lib import funnel as fn, state as state_mod  # noqa: E402

STATE = ROOT / "data" / "state.local.json"
JOBS = ROOT / "docs" / "jobs.json"
OUT = ROOT / "data" / "funnel-report.md"


def _rate_row(label, r):
    return (f"| {label} | {r['applications']} | {r['app_to_call']}% | {r['app_to_interview']}% "
            f"| {r['interview_to_offer']}% | {r['offers']} |")


def _table(title, by):
    lines = [f"### {title}", "| | apps | app→call | app→interview | interview→offer | offers |",
             "|---|---|---|---|---|---|"]
    for k, r in by.items():
        lines.append(_rate_row(k or "—", r))
    return "\n".join(lines)


def render(apps, conv, diag=None):
    ov = conv["overall"]
    L = ["# Funnel report",
         f"_{ov['applications']} application(s) · {ov['qualified']} qualified · baseline "
         f"app→interview {round(fn.BASELINE_APP_TO_INTERVIEW*100,2)}%_\n",
         "## Overall funnel",
         f"- Applications: **{ov['applications']}**  (qualified: {ov['qualified']})",
         f"- Recruiter calls: {ov['calls']}  ·  app→call **{ov['app_to_call']}%**",
         f"- Interviews: {ov['interviews']}  ·  app→interview **{ov['app_to_interview']}%**",
         f"- Finals: {ov['finals']}  ·  interview→final {ov['interview_to_final']}%",
         f"- Offers: {ov['offers']}  ·  final→offer {ov['final_to_offer']}%  "
         f"(interview→offer {ov['interview_to_offer']}%)\n"]
    L.append(_table("By lane", conv["by_lane"]))
    L.append(_table("By résumé version", conv["by_resume"]))
    L.append(_table("By source", conv["by_source"]))
    L.append(_table("By warm / cold", conv["by_warm_cold"]))
    L.append(_table("By sponsorship", conv["by_sponsorship"]))
    L.append(_table("By posting age at apply", conv["by_age"]))
    L.append(_table("By experience requirement", conv["by_experience"]))
    if diag:
        L.append("\n## Diagnostic (every 30 applications)")
        qs = [("What is converting?", "what_is_converting"),
              ("What is not converting?", "what_is_not_converting"),
              ("Where is the funnel failing?", "where_funnel_fails"),
              ("Which lane should get more volume?", "which_lane_more_volume"),
              ("Which résumé needs revision?", "which_resume_needs_revision"),
              ("Is immigration filtering early enough?", "immigration_filtered_early_enough"),
              ("Are applications submitted quickly enough?", "applications_fast_enough"),
              ("Versus baseline", "vs_baseline")]
        for label, key in qs:
            L.append(f"- **{label}** {diag.get(key,'')}")
    return "\n\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--print", dest="show", action="store_true")
    args = ap.parse_args()

    events = state_mod.load(STATE).get("events", [])
    try:
        jobs = json.loads(JOBS.read_text()).get("jobs", [])
    except (OSError, json.JSONDecodeError):
        jobs = []
    jobs_by_id = {j.get("id"): j for j in jobs}

    apps = fn.build_applications(events, jobs_by_id)
    if not apps:
        print("funnel: no applications recorded yet — apply to jobs in the dashboard, then "
              "Export state (cockpit) to data/state.local.json.")
        return 0
    conv = fn.conversions(apps)
    diag = fn.diagnostic(apps, conv) if fn.should_report(len(apps)) else fn.diagnostic(apps, conv)
    report = render(apps, conv, diag)
    OUT.write_text(report)
    milestone = " (30-application milestone)" if fn.should_report(len(apps)) else ""
    print(f"funnel: {len(apps)} application(s){milestone} → wrote {OUT.relative_to(ROOT)}")
    print(f"  app→call {conv['overall']['app_to_call']}% · app→interview "
          f"{conv['overall']['app_to_interview']}% · {diag['vs_baseline']}")
    if args.show:
        print("\n" + report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
