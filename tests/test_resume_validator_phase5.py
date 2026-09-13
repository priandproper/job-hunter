#!/usr/bin/env python3
"""Phase 5 tests — the programmatic résumé validator.

Enforces the brief's fail-generation rules: an unsupported number, altered
dates/employers/titles, an unsupported technology, an ownership upgrade, leftover
company/product from another application, and an untraceable bullet. Also checks
the pass case, requirement coverage, and the change log.

Zero-dependency: `python3 tests/test_resume_validator_phase5.py` (pytest too).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import resume_validator as rv  # noqa: E402
from lib import factbank as fb  # noqa: E402

BASE = {
    "summary": "Product marketer with 4 years of B2B SaaS experience.",
    "contact": {"fullName": "Test User"},
    "experience": [
        {"company": "Glidely Inc.", "title": "Marketing Analyst",
         "location": "SF", "startDate": "Jun 2023", "endDate": "Aug 2024",
         "highlights": [
             "Modeled the lead-to-demo funnel in SQL and Tableau, lifting conversion 30%.",
             "Contributed to A/B tests on outbound messaging that improved acceptance rates.",
             "Ran cohort analysis to prioritize high-opportunity segments."]},
    ],
    "education": [{"school": "Babson"}],
    "projects": [],
    "skills": [{"name": "Analytics", "items": ["SQL", "Tableau", "A/B testing"]}],
}

JOB = {"company": "Acme", "title": "Marketing Analytics Manager",
       "excerpt": ("Basic Qualifications:\n- 3+ years in marketing analytics\n"
                   "- Experience with SQL and Tableau\n- A/B testing experience")}


def _tailored(**over):
    import copy
    t = copy.deepcopy(BASE)
    t.update(over)
    return t


def _passes(tailored, **kw):
    return rv.validate(tailored, BASE, JOB, **kw)["passed"]


# 1) A faithful rephrase (same facts, reordered/reworded) passes.
def test_clean_rephrase_passes():
    t = _tailored(summary="Analytics-focused marketer; 4 years in B2B SaaS.")
    t["experience"][0]["highlights"] = [
        "Built lead-to-demo funnel models in SQL and Tableau, lifting conversion 30%.",
        "Ran cohort analysis to prioritize high-opportunity segments.",
        "Contributed to outbound A/B tests that improved acceptance rates."]
    rep = rv.validate(t, BASE, JOB)
    assert rep["passed"], rep["hard_failures"]


# 2) An invented number fails.
def test_unsupported_number_fails():
    t = _tailored()
    t["experience"][0]["highlights"][0] = "Lifted conversion 65% via SQL/Tableau funnel work."
    rep = rv.validate(t, BASE, JOB)
    assert not rep["passed"]
    assert any(k == "number" for k, _ in rep["hard_failures"])


# 3) Altered employment dates / title / employer fail.
def test_date_title_employer_drift_fails():
    for field, val in (("startDate", "Jan 2020"), ("title", "Senior Manager"),
                       ("company", "Google")):
        t = _tailored()
        t["experience"][0][field] = val
        rep = rv.validate(t, BASE, JOB)
        assert not rep["passed"], f"{field} change should fail"
        assert any(k == "dates/employer/title" for k, _ in rep["hard_failures"])


# 4) An unsupported technology fails.
def test_unsupported_technology_fails():
    t = _tailored()
    t["experience"][0]["highlights"][0] = "Built Kubernetes pipelines and SQL funnel models."
    rep = rv.validate(t, BASE, JOB)
    assert not rep["passed"]
    assert any(k == "technology" for k, _ in rep["hard_failures"])


# 5) Upgrading ownership (contributed -> led) fails.
def test_ownership_upgrade_fails():
    t = _tailored()
    # base bullet 2 was "Contributed to A/B tests..."; rewrite as "Led ..."
    t["experience"][0]["highlights"][1] = "Led A/B tests on outbound messaging that improved acceptance rates."
    rep = rv.validate(t, BASE, JOB)
    assert not rep["passed"]
    assert any(k == "ownership" for k, _ in rep["hard_failures"])


# 6) A leftover company from another application fails.
def test_cross_application_contamination_fails():
    t = _tailored()
    t["experience"][0]["highlights"][0] = "Drove SQL/Tableau funnel work tailored for Datadog's APM buyers."
    rep = rv.validate(t, BASE, JOB, other_names=["Datadog", "Snowflake"])
    assert not rep["passed"]
    assert any(k == "contamination" for k, _ in rep["hard_failures"])


# 7) A wholly-invented bullet (no basis in the base resume) is untraceable and fails.
def test_untraceable_bullet_fails():
    t = _tailored()
    t["experience"][0]["highlights"].append(
        "Managed a global partner ecosystem across three continents.")
    rep = rv.validate(t, BASE, JOB)
    assert not rep["passed"]
    assert any(k == "traceability" for k, _ in rep["hard_failures"])


# 8) Requirement coverage + gaps are reported.
def test_requirement_coverage_reported():
    rep = rv.validate(_tailored(), BASE, JOB)
    cov = rep["requirement_coverage"]
    assert cov["coverage_ratio"] is not None
    assert isinstance(cov["covered"], list) and isinstance(cov["gaps"], list)


# 9) With verified facts, fact_ids_used is populated and tracing enforces on facts.
def test_fact_ids_used_when_verified():
    facts = [fb.blank_fact(
        fact_id="glidely1", company="Glidely Inc.", role="Marketing Analyst",
        action="Modeled the lead-to-demo funnel in SQL and Tableau, lifting conversion 30%.",
        verification_status="verified")]
    t = _tailored()
    t["experience"][0]["highlights"] = [
        "Modeled the lead-to-demo funnel in SQL and Tableau, lifting conversion 30%."]
    rep = rv.validate(t, BASE, JOB, facts=facts)
    assert "glidely1" in rep["fact_ids_used"]
    assert rep["trace_enforcing_on_facts"] is True


# 10) Change log records what was altered.
def test_change_log():
    t = _tailored(summary="A different summary sentence.")
    rep = rv.validate(t, BASE, JOB)
    assert any("summary" in c for c in rep["change_log"])


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
