"""Phase 8 — complete application packet for a Priority-A / Priority-B job.

Assembles the 14-item packet the brief specifies into one reviewable artifact, so
the candidate opens a job with everything already prepared and only reviews + sends.
NOTHING is auto-sent. Deterministic and stdlib-only: it composes the structured JD
(Phase 3), the immigration object (Phase 2), the priority breakdown (Phase 6), and —
for the requirement-evidence matrix (Phase 3's deferred piece) — the VERIFIED career
facts (Phase 4), so the matrix cites only facts the candidate has stood behind.

Items: 1 job summary · 2 five key requirements · 3 requirement-evidence matrix ·
4 genuine gaps · 5 immigration risk + verification action · 6 recommended résumé
template · 7 proposed résumé changes · 8 validation preview · 9 employee/alumni
outreach draft · 10 recruiter outreach draft · 11 referral follow-up draft ·
12 sponsorship-verification wording · 13 application checklist · 14 follow-up date.
"""

import datetime as _dt
import re

from lib import factbank as _fb
from lib import jobspec as _jobspec

_WORD = re.compile(r"[a-z0-9][a-z0-9\+/\.#-]*")
_STOP = set("the a an and or of to in for on with at by from as is are be this that you "
            "your we our their they it its into across over per using use used including "
            "etc via which who whom whose than then years year experience degree bachelor "
            "master plus strong ability able work working role team preferred required".split())

# The three validated résumé templates (Phase 5) and the lane signals that pick one.
TEMPLATES = {
    "enterprise_ai_pmm": "Enterprise AI / Technical PMM",
    "analytics_growth": "Marketing Analytics / Growth",
    "customer_portfolio": "Customer / Portfolio Marketing & Enablement",
}
_TPL_SIGNALS = [
    ("enterprise_ai_pmm", ("ai", "ml", "platform", "developer", "technical", "api",
                            "infrastructure", "security", "data platform", "llm")),
    ("analytics_growth", ("analyst", "analytics", "growth", "demand", "data", "insights",
                          "lifecycle", "acquisition", "performance", "operations", "revops")),
    ("customer_portfolio", ("customer", "portfolio", "enablement", "field", "partner",
                            "community", "advocacy", "retention", "solutions")),
]


def _tokens(text):
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 2}


def _jaccard(a, b):
    return len(a & b) / len(a | b) if (a or b) else 0.0


def recommend_template(job, spec):
    hay = (job.get("title", "") + " " + " ".join(spec.get("responsibilities", []))
           + " " + " ".join(spec.get("basic_qualifications", []))).lower()
    best, best_n = "analytics_growth", 0
    for key, sig in _TPL_SIGNALS:
        n = sum(1 for s in sig if s in hay)
        if n > best_n:
            best_n, best = n, key
    return key_and_label(best)


def key_and_label(key):
    return {"key": key, "label": TEMPLATES[key]}


def key_requirements(spec, limit=5):
    """The N most important requirements: basic quals first (eligibility), then
    preferred. Returns [{text, importance}]."""
    out = []
    for i, q in enumerate(spec.get("basic_qualifications", []) or []):
        out.append({"text": q, "importance": "critical" if i < 3 else "high"})
    for q in spec.get("preferred_qualifications", []) or []:
        out.append({"text": q, "importance": "medium"})
    return out[:limit] if limit else out


def requirement_evidence(spec, verified_facts, candidate_skills):
    """The requirement-to-evidence matrix (Phase 3 schema), grounded in VERIFIED facts.
    Each row: requirement, importance, candidate_fact_ids, strength, reason, resume_action.
    Direct evidence -> strong (cite fact_ids); transferable skills -> partial; none -> gap."""
    rows = []
    reqs = key_requirements(spec, limit=0)   # all, ranked
    for r in reqs:
        rt = _tokens(r["text"])
        fids, best = [], 0.0
        for f in verified_facts:
            ov = _jaccard(rt, _tokens(f.get("action", "")))
            if ov >= 0.28:
                fids.append(f["fact_id"])
            best = max(best, ov)
        skill_cover = len(rt & candidate_skills) / len(rt) if rt else 0.0
        if fids:
            strength, reason = "strong", f"direct evidence in {len(fids)} verified fact(s)"
            action = "Lead with the cited fact(s) — reword to the role's language, no new claims."
        elif skill_cover >= 0.34:
            strength, reason = "partial", "transferable — covered by your skills, not yet a verified bullet"
            action = "Reframe your closest real experience toward this; verify a fact to cite it."
        else:
            strength, reason = "gap", "no direct or transferable evidence found"
            action = "Genuine gap — do not claim it; address in the cover note or upskill."
        rows.append({"requirement": r["text"][:160], "importance": r["importance"],
                     "candidate_fact_ids": fids, "strength": strength,
                     "reason": reason, "resume_action": action})
    return rows


def _proposed_changes(job, spec, candidate_skills):
    """Which real skills to foreground (present in the JD AND the candidate) and which
    requested keywords to address — never invent."""
    jd_terms = _tokens(" ".join([job.get("title", "")] + spec.get("responsibilities", [])
                                 + spec.get("basic_qualifications", [])))
    surface = sorted((jd_terms & candidate_skills))[:10]
    missing = [k for k in (job.get("missing_keywords") or [])][:8]
    return {
        "surface_skills": surface,
        "address_keywords": missing,
        "note": ("Surface the skills above (you have them); address the keywords only where "
                 "truthful. Summary ≤2 sentences; ≥3 substantive bullets per role; do not invent."),
    }


def _immigration_block(job):
    imm = job.get("immigration") or {}
    return {
        "risk": imm.get("risk", "unknown"),
        "job_text": imm.get("job_text", ""),
        "action": imm.get("action", ""),
        "evidence": [e.get("text", "") for e in (imm.get("evidence") or [])][:2],
    }


def _drafts(job):
    company = job.get("company", "the company")
    title = job.get("title", "the role")
    return {
        "employee_alumni": (
            f"Hi [name] — I'm exploring the {title} role at {company} and your path there stood "
            f"out (we [shared school / mutual connection / your recent post on ___]). I've spent "
            f"~4 years in B2B SaaS marketing and analytics. Would you be open to a quick 15-min "
            f"chat, or to pointing me toward the hiring manager? Happy to share my resume."),
        "recruiter": (
            f"Hi [recruiter] — I applied to the {title} role at {company}. My background in product "
            f"marketing, GTM and marketing analytics maps closely to the core requirements "
            f"(happy to walk through specifics). I'd welcome a brief conversation about fit and "
            f"next steps."),
        "referral_followup": (
            f"Thanks again [name] for [referring me / the chat] on the {title} role at {company} — "
            f"really appreciate it. Just flagging that I've submitted my application; if there's "
            f"anything useful I can send along for the hiring manager, let me know."),
    }


def _sponsorship_wording(job):
    risk = (job.get("immigration") or {}).get("risk", "yellow")
    if risk == "red":
        return ("This posting indicates a hard immigration barrier (see immigration block). "
                "Do not pursue unless you can verify that is wrong.")
    base = ("One logistical note so we're aligned early: I'm currently authorized to work on F-1 "
            "OPT, and this role would eventually require H-1B sponsorship. Could you confirm "
            f"{job.get('company','the company')} is open to sponsoring for this position?")
    if risk == "green":
        return "The posting already indicates sponsorship — confirm in writing: " + base
    return base


def _checklist(job, matrix, immigration, template):
    aligned = any(r["strength"] in ("strong", "partial") and r["importance"] in ("critical", "high")
                  for r in matrix)
    gaps = sum(1 for r in matrix if r["strength"] == "gap")
    return [
        {"item": "Role is in an approved lane (surfaced on-target)", "done": True},
        {"item": f"Basic qualifications reasonably align ({gaps} gap(s))", "done": aligned},
        {"item": "No unresolved hard immigration prohibition",
         "done": immigration["risk"] != "red"},
        {"item": f"Résumé tailored with the '{template['label']}' template", "done": False},
        {"item": "Validation passed (run scripts/tailor_resume.py --id …)", "done": False},
        {"item": "Application submission recorded (mark applied in the dashboard)", "done": False},
    ]


def build(job, verified_facts=None, candidate_skills=None, today=None):
    """Assemble the full 14-item packet for a job. Pure — pass verified facts and the
    candidate's skill vocabulary; returns a dict the CLI/dashboard renders."""
    today = today or _dt.date.today()
    verified = _fb.usable(verified_facts or [])
    skills = candidate_skills or set()
    spec = job.get("spec") or _jobspec.structure(job, include_full=False, max_bullets=12)
    prio = job.get("priority") or {}

    matrix = requirement_evidence(spec, verified, skills)
    immigration = _immigration_block(job)
    template = recommend_template(job, spec)
    gaps = [r["requirement"] for r in matrix if r["strength"] == "gap"]
    covered = sum(1 for r in matrix if r["strength"] != "gap")

    band = prio.get("band", "?")
    minutes = "30–40 min" if band == "A" else "10–15 min" if band == "B" else "—"
    follow_up = (today + _dt.timedelta(days=7)).isoformat()

    return {
        "job_id": job.get("id", ""),
        "band": band,
        "target_time": minutes,
        "summary": {                                            # 1
            "title": job.get("title", ""), "company": job.get("company", ""),
            "location": job.get("location", ""), "url": job.get("url", ""),
            "salary": spec.get("salary"), "required_years": spec.get("required_years"),
            "priority_total": prio.get("total"), "priority_why": prio.get("why", ""),
        },
        "key_requirements": key_requirements(spec, 5),          # 2
        "requirement_evidence_matrix": matrix,                  # 3
        "genuine_gaps": gaps,                                   # 4
        "immigration": immigration,                             # 5
        "recommended_template": template,                       # 6
        "proposed_resume_changes": _proposed_changes(job, spec, skills),  # 7
        "validation_preview": {                                 # 8
            "requirements_with_evidence": f"{covered}/{len(matrix)}",
            "note": ("Final hard validation runs when you generate the résumé: "
                     f"python3 scripts/tailor_resume.py --id {job.get('id','')}"),
            "verified_facts_available": len(verified),
        },
        "outreach_drafts": _drafts(job),                        # 9,10,11
        "sponsorship_verification": _sponsorship_wording(job),  # 12
        "checklist": _checklist(job, matrix, immigration, template),  # 13
        "follow_up_date": follow_up,                            # 14
        "auto_send": False,
    }
