#!/usr/bin/env python3
"""Generate a complete application packet (Phase 8) for a Priority-A / Priority-B job.

Reads docs/jobs.json (run worker.py first) + your VERIFIED facts (data/facts.local.json)
and writes a reviewable Markdown packet to data/packets/<slug>.md — the 14 items the
brief specifies, all deterministic, nothing auto-sent. You open it, review, and act.

  python3 scripts/packet.py --id <job-id>        # one job
  python3 scripts/packet.py --all                # every current A/B job
  python3 scripts/packet.py --id <id> --json     # emit JSON instead of Markdown
"""

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lib import factbank, packet as packet_mod, profile as profile_mod  # noqa: E402

JOBS = ROOT / "docs" / "jobs.json"
FACTS = ROOT / "data" / "facts.local.json"
OUTDIR = ROOT / "data" / "packets"          # git-ignored working artifacts


def _candidate_skills():
    """The candidate's skill/experience vocabulary from the resume profile."""
    try:
        cfg = json.loads((ROOT / "config.json").read_text())
        prof = profile_mod.load_profile(cfg, ROOT)
    except Exception:
        return set()
    sk = set()
    for v in getattr(prof, "variants", []) or []:
        for t in prof.variant_terms(v):
            sk.update(w for w in re.findall(r"[a-z0-9][a-z0-9\+/\.#-]*", t.lower()) if len(w) > 2)
    return sk


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:50] or "job"


def _md(pk):
    s = pk["summary"]
    L = [f"# Application packet — {s['title']} · {s['company']}",
         f"_Priority {pk['band']} · target time {pk['target_time']} · nothing is sent automatically_\n"]
    # 1 summary
    L.append("## 1. Job summary")
    L.append(f"- **{s['title']}** at **{s['company']}** — {s['location'] or '—'}")
    L.append(f"- Salary: {s['salary'] or 'not stated'} · Required years: "
             f"{s['required_years'] if s['required_years'] is not None else 'not stated'}")
    if s.get("url"):
        L.append(f"- Posting: {s['url']}")
    L.append(f"- Priority: **{pk['band']} ({s.get('priority_total')})** — {s.get('priority_why','')}\n")
    # 2 key requirements
    L.append("## 2. Five most important requirements")
    for r in pk["key_requirements"]:
        L.append(f"- _[{r['importance']}]_ {r['text']}")
    L.append("")
    # 3 matrix
    L.append("## 3. Requirement → evidence matrix")
    L.append("| Requirement | Importance | Strength | Evidence (fact ids) | Résumé action |")
    L.append("|---|---|---|---|---|")
    for r in pk["requirement_evidence_matrix"]:
        fids = ", ".join(r["candidate_fact_ids"]) or "—"
        L.append(f"| {r['requirement'][:70]} | {r['importance']} | **{r['strength']}** | "
                 f"{fids} | {r['resume_action']} |")
    L.append("")
    # 4 gaps
    L.append("## 4. Genuine gaps")
    L.append("\n".join(f"- {g}" for g in pk["genuine_gaps"]) or "- None identified.")
    L.append("")
    # 5 immigration
    im = pk["immigration"]
    L.append("## 5. Immigration risk & verification action")
    L.append(f"- Risk: **{im['risk']}** · JD text: {im['job_text'] or '—'}")
    L.append(f"- Action: {im['action']}")
    for e in im["evidence"]:
        L.append(f"  - evidence: “{e}”")
    L.append("")
    # 6 template
    L.append("## 6. Recommended résumé template")
    L.append(f"- **{pk['recommended_template']['label']}**\n")
    # 7 proposed changes
    pc = pk["proposed_resume_changes"]
    L.append("## 7. Proposed résumé changes")
    L.append(f"- Surface (you have these): {', '.join(pc['surface_skills']) or '—'}")
    L.append(f"- Address truthfully: {', '.join(pc['address_keywords']) or '—'}")
    L.append(f"- {pc['note']}\n")
    # 8 validation
    v = pk["validation_preview"]
    L.append("## 8. Validation preview")
    L.append(f"- Requirements with evidence: {v['requirements_with_evidence']} · "
             f"verified facts available: {v['verified_facts_available']}")
    L.append(f"- {v['note']}\n")
    # 9-11 drafts
    d = pk["outreach_drafts"]
    L.append("## 9. Employee / alumni outreach draft\n> " + d["employee_alumni"].replace("\n", "\n> "))
    L.append("\n## 10. Recruiter outreach draft\n> " + d["recruiter"].replace("\n", "\n> "))
    L.append("\n## 11. Referral follow-up draft\n> " + d["referral_followup"].replace("\n", "\n> "))
    # 12 sponsorship
    L.append("\n## 12. Sponsorship-verification wording\n> " + pk["sponsorship_verification"])
    # 13 checklist
    L.append("\n## 13. Application checklist")
    for c in pk["checklist"]:
        L.append(f"- [{'x' if c['done'] else ' '}] {c['item']}")
    # 14 follow-up
    L.append(f"\n## 14. Recommended follow-up date\n- **{pk['follow_up_date']}** (~1 week after applying)")
    L.append("\n_Review everything above before sending. Nothing here is sent automatically._")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", dest="job_id", default="")
    ap.add_argument("--all", action="store_true", help="every current Priority-A/B job")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of Markdown")
    args = ap.parse_args()

    try:
        jobs = json.loads(JOBS.read_text()).get("jobs", [])
    except (OSError, json.JSONDecodeError):
        print("packet: no docs/jobs.json — run worker.py first."); return 1
    facts = factbank.load(FACTS)
    skills = _candidate_skills()

    if args.all:
        targets = [j for j in jobs if (j.get("priority") or {}).get("band") in ("A", "B")]
    elif args.job_id:
        targets = [j for j in jobs if j.get("id") == args.job_id]
    else:
        print("packet: pass --id <job-id> or --all."); return 2
    if not targets:
        print("packet: no matching job(s)."); return 1

    OUTDIR.mkdir(parents=True, exist_ok=True)
    n = 0
    for job in targets:
        pk = packet_mod.build(job, verified_facts=facts, candidate_skills=skills)
        slug = f"{_slug(job.get('company'))}-{_slug(job.get('title'))}"
        if args.json:
            out = OUTDIR / f"{slug}.json"
            out.write_text(json.dumps(pk, indent=2))
        else:
            out = OUTDIR / f"{slug}.md"
            out.write_text(_md(pk))
        n += 1
        print(f"packet: wrote {out.relative_to(ROOT)}  (Priority {pk['band']}, {pk['target_time']})")
    if not facts or not factbank.usable(facts):
        print("  note: no VERIFIED facts yet — the evidence matrix cites none. "
              "Verify facts in data/facts.local.json (see scripts/seed_facts.py) to ground it.")
    print(f"packet: {n} packet(s) in {OUTDIR.relative_to(ROOT)} — review before acting; nothing is sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
