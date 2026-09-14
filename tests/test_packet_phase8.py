#!/usr/bin/env python3
"""Phase 8 tests — the complete application packet.

Covers all 14 items being present, the requirement-evidence matrix grounding in
VERIFIED facts only (Phase 3+4), gap detection, template recommendation, the
immigration/sponsorship wording by risk, the checklist, band-based completion time,
and that nothing is auto-sent.

Zero-dependency: `python3 tests/test_packet_phase8.py` (pytest-collectable too).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import packet as pk  # noqa: E402
from lib import factbank as fb  # noqa: E402

SKILLS = {"sql", "tableau", "product", "marketing", "gtm", "segmentation", "funnel",
          "analytics", "positioning", "messaging", "excel", "campaign", "conversion"}


def _job(band="A", risk="yellow", title="Product Marketing Manager"):
    return {
        "id": "job1", "title": title, "company": "Acme", "location": "Boston, MA",
        "url": "https://x/y", "missing_keywords": ["abm", "salesforce"],
        "immigration": {"risk": risk, "job_text": "not_stated",
                        "action": "Verify with recruiter.", "evidence": []},
        "priority": {"band": band, "total": 78, "why": "Strong on basic quals.",
                     "components": []},
        "spec": {"basic_qualifications": [
                     "4+ years of product marketing experience",
                     "Experience with SQL and Tableau for funnel analytics",
                     "Segmentation and positioning experience",
                     "Bachelor's degree"],
                 "preferred_qualifications": ["MBA", "Experience with ABM"],
                 "responsibilities": ["Own go-to-market messaging", "Run campaign analytics"],
                 "required_years": 4, "salary": "$120,000 - $150,000"},
    }


def _facts(*statuses):
    out = []
    acts = ["Modeled the lead-to-demo funnel in SQL and Tableau, lifting conversion 30%.",
            "Owned product marketing messaging and positioning for a B2B SaaS launch."]
    for i, st in enumerate(statuses):
        out.append(fb.blank_fact(fact_id=f"f{i}", company="Prev", role="Analyst",
                                 action=acts[i % len(acts)], verification_status=st))
    return out


# 1) All 14 items are present in the packet.
def test_all_14_items_present():
    p = pk.build(_job(), verified_facts=_facts("verified", "verified"), candidate_skills=SKILLS)
    for k in ("summary", "key_requirements", "requirement_evidence_matrix", "genuine_gaps",
              "immigration", "recommended_template", "proposed_resume_changes",
              "validation_preview", "outreach_drafts", "sponsorship_verification",
              "checklist", "follow_up_date"):
        assert k in p, k
    d = p["outreach_drafts"]
    assert {"employee_alumni", "recruiter", "referral_followup"} <= set(d)  # items 9,10,11
    assert p["auto_send"] is False


# 2) Five key requirements, basic quals first (critical/high).
def test_key_requirements_ranked():
    p = pk.build(_job(), candidate_skills=SKILLS)
    kr = p["key_requirements"]
    assert len(kr) == 5
    assert kr[0]["importance"] == "critical"
    assert any(r["importance"] == "medium" for r in kr)  # a preferred qual padded in


# 3) The matrix cites ONLY verified facts (Phase 4 guarantee).
def test_matrix_cites_only_verified_facts():
    # one verified fact that matches the SQL/Tableau requirement, one not-verified
    facts = _facts("verified", "needs_clarification")
    p = pk.build(_job(), verified_facts=facts, candidate_skills=SKILLS)
    cited = {fid for r in p["requirement_evidence_matrix"] for fid in r["candidate_fact_ids"]}
    assert "f0" in cited            # the verified funnel/SQL fact is cited
    assert "f1" not in cited        # the needs_clarification fact is never cited


# 4) Strength classes appear: a matching requirement is strong, an off-topic one is a gap.
def test_strength_and_gaps():
    p = pk.build(_job(), verified_facts=_facts("verified"), candidate_skills=SKILLS)
    strengths = {r["strength"] for r in p["requirement_evidence_matrix"]}
    assert "strong" in strengths
    # the degree requirement has no skill/fact evidence -> gap
    assert any(r["strength"] == "gap" for r in p["requirement_evidence_matrix"])
    assert p["genuine_gaps"]  # populated from gap rows


# 5) With no verified facts, the matrix cites none but still classifies via skills.
def test_no_verified_facts_cites_none():
    p = pk.build(_job(), verified_facts=_facts("needs_clarification"), candidate_skills=SKILLS)
    cited = {fid for r in p["requirement_evidence_matrix"] for fid in r["candidate_fact_ids"]}
    assert cited == set()
    assert p["validation_preview"]["verified_facts_available"] == 0


# 6) Template recommendation keys off the lane.
def test_template_recommendation():
    assert pk.build(_job(title="Marketing Data Analyst"), candidate_skills=SKILLS
                    )["recommended_template"]["key"] == "analytics_growth"
    assert pk.build(_job(title="Technical Product Marketing Manager, AI Platform"),
                    candidate_skills=SKILLS)["recommended_template"]["key"] == "enterprise_ai_pmm"
    assert pk.build(_job(title="Customer Marketing & Enablement Manager"),
                    candidate_skills=SKILLS)["recommended_template"]["key"] == "customer_portfolio"


# 7) Sponsorship wording adapts to immigration risk.
def test_sponsorship_wording_by_risk():
    assert "sponsoring" in pk.build(_job(risk="yellow"), candidate_skills=SKILLS)["sponsorship_verification"]
    assert "in writing" in pk.build(_job(risk="green"), candidate_skills=SKILLS)["sponsorship_verification"]
    assert "Do not pursue" in pk.build(_job(risk="red"), candidate_skills=SKILLS)["sponsorship_verification"]


# 8) Checklist encodes the qualified-application gates; immigration reflects risk.
def test_checklist_reflects_state():
    ok = pk.build(_job(risk="yellow"), verified_facts=_facts("verified"), candidate_skills=SKILLS)["checklist"]
    imm_row = [c for c in ok if "immigration" in c["item"]][0]
    assert imm_row["done"] is True
    red = pk.build(_job(risk="red"), candidate_skills=SKILLS)["checklist"]
    assert [c for c in red if "immigration" in c["item"]][0]["done"] is False
    # template/validation/submission are not done until the user acts
    assert all(not c["done"] for c in ok if "template" in c["item"] or "Validation" in c["item"])


# 9) Completion-time target follows the band (A 30–40, B 10–15).
def test_completion_time_by_band():
    assert "30" in pk.build(_job(band="A"), candidate_skills=SKILLS)["target_time"]
    assert "10" in pk.build(_job(band="B"), candidate_skills=SKILLS)["target_time"]


# 10) Follow-up date is one week out from generation.
def test_follow_up_date():
    import datetime as dt
    p = pk.build(_job(), candidate_skills=SKILLS, today=dt.date(2026, 9, 14))
    assert p["follow_up_date"] == "2026-09-21"


def _run():
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    failed = 0
    for name, fn in tests:
        try:
            fn(); print(f"  ok   {name}")
        except AssertionError as e:
            failed += 1; print(f"  FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
