#!/usr/bin/env python3
"""Phase 2 tests — immigration viability as a first-class, evidence-backed signal.

Covers the deterministic JD-text classifier, the hard-stop rules (explicit
prohibit, citizenship, clearance), the green/yellow/red risk logic, and the
non-negotiable rule that historical company H-1B use is evidence — never proof —
so it can never lift risk to green.

Zero-dependency: `python3 tests/test_immigration_phase2.py` (pytest-collectable too).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import immigration as im  # noqa: E402
from lib import match as m  # noqa: E402

CFG = {"min_fit_score": 30, "exclude_sponsorship": ["No"],
       "experience": {"exclude_at_years": 8}, "immigration_hard_stop": True}
HIGH_FIT = {"fit_score": 99}


def _job(excerpt, title="Product Marketing Manager", sponsorship="Unknown", note=""):
    return {"title": title, "excerpt": excerpt, "location": "Boston, MA",
            "url": "https://example.com/job", "sponsorship": sponsorship,
            "sponsorship_note": note}


# 1) Explicit "unable to sponsor" -> prohibits + red + hard stop.
def test_explicit_prohibit_is_red_hardstop():
    j = _job("Great role. We are unable to sponsor an employment visa for this role.")
    r = im.classify(j)
    assert r["job_text"] == "prohibits"
    assert r["risk"] == "red"
    assert im.hard_stop(j)[0] is True
    assert not m.passes_filters(j, HIGH_FIT, CFG)   # filtered out end-to-end


# 2) "visa sponsorship is not available" phrasing also caught.
def test_not_available_phrasing_is_red():
    j = _job("Compensation is competitive. Please note visa sponsorship is not available for this position.")
    assert im.classify(j)["risk"] == "red"


# 3) Citizenship / export-control requirement -> hard stop (F-1/H-1B excluded).
def test_citizenship_requirement_is_hardstop():
    j = _job("Due to export control laws, candidates must be a U.S. citizen or permanent resident.")
    r = im.classify(j)
    assert r["risk"] == "red"
    assert r["hard_stop_reason"] == "citizenship"
    assert not m.passes_filters(j, HIGH_FIT, CFG)


# 4) Security-clearance requirement -> hard stop.
def test_clearance_requirement_is_hardstop():
    j = _job("This role requires an active TS/SCI security clearance.")
    assert im.classify(j)["risk"] == "red"
    assert im.hard_stop(j)[0] is True


# 5) EEO boilerplate mentioning citizenship is NOT a hard stop (guarded).
def test_eeo_boilerplate_citizenship_not_hardstop():
    j = _job("We are an equal opportunity employer and consider all applicants "
             "regardless of citizenship, race, or national origin.")
    r = im.classify(j)
    assert r["risk"] != "red", r
    assert im.hard_stop(j)[0] is False


# 6) Explicit support -> green; unknown/silent -> yellow with a verify action.
def test_support_is_green_and_unknown_is_yellow():
    green = im.classify(_job("We are happy to sponsor visas for this role."))
    assert green["job_text"] == "supports" and green["risk"] == "green"
    yellow = im.classify(_job("Own our GTM launches and messaging across the funnel."))
    assert yellow["job_text"] == "not_stated" and yellow["risk"] == "yellow"
    assert "verify" in yellow["action"].lower()


# 7) Hedged sponsorship -> ambiguous, and stays yellow (not green).
def test_hedged_sponsorship_is_ambiguous_yellow():
    r = im.classify(_job("Visa sponsorship may be available for exceptional candidates."))
    assert r["job_text"] == "ambiguous"
    assert r["risk"] == "yellow"


# 8) Historical company H-1B use is EVIDENCE, never proof — cannot make risk green.
def test_company_h1b_history_never_makes_green():
    j = _job("Own our product marketing narrative.")   # JD says nothing about sponsorship
    company = {"name": "Acme", "h1b": True, "h1b_note": "public LCA rows found (h1bdata, 2024)"}
    r = im.classify(j, company)
    assert r["company_h1b_history"] == "some"
    assert r["risk"] == "yellow", "history must not lift risk to green"
    kinds = {e["type"] for e in r["evidence"]}
    assert "company_h1b_history" in kinds   # recorded as evidence
    # ...and E-Verify is tracked separately from H-1B history.
    assert r["company_everify"] == "unknown"


# 9) Evidence snippet actually contains the matched phrase (regression: abbreviations
#    like "U.S." and bullet lists used to run the snippet away from the match).
def test_evidence_snippet_centers_on_match():
    long_jd = ("Key responsibilities: own Marketo administration, tokenization, and "
               "channel architecture across the marketing org, plus reporting. " * 4 +
               "Requirement: candidates must be a U.S. citizen or national.")
    r = im.classify(_job(long_jd))
    assert r["risk"] == "red"
    assert "citizen" in r["evidence"][0]["text"].lower(), r["evidence"][0]["text"]


# 10) Enrichment "No" still hard-stops; the object preserves the enrichment evidence.
def test_enrichment_no_hardstop_with_evidence():
    j = _job("Own GTM.", sponsorship="No", note="JD: no visa sponsorship available")
    r = im.classify(j)
    assert r["risk"] == "red"
    assert any(e["type"] == "enrichment" for e in r["evidence"])


# 11) Conditional sponsorship (both a support AND a restriction phrase) -> ambiguous,
#     never a clean green and never a hard stop. Real case: an employer that sponsors
#     generally but "aren't able to sponsor" in some situations.
def test_conditional_sponsorship_is_ambiguous():
    j = _job("Visa sponsorship: We do sponsor visas! However, we aren't able to "
             "successfully sponsor visas for every role or situation.")
    r = im.classify(j)
    assert r["job_text"] == "ambiguous", r["job_text"]
    assert r["risk"] == "yellow"
    assert im.hard_stop(j)[0] is False


# 12) A company on the candidate's confirmed won't-sponsor list is a hard stop even
#     when the JD is silent (the "no sponsorship" lived only in the application form).
def test_confirmed_no_sponsor_company():
    j = _job("Own GTM.", title="Marketing Manager")   # JD says nothing about sponsorship
    j["company"] = "1Password"
    ns = {"1password"}
    assert im.hard_stop(j, ns)[0] is True
    r = im.classify(j, no_sponsor=ns)
    assert r["risk"] == "red" and r["hard_stop_reason"] == "confirmed_no_sponsorship"
    assert not m.passes_filters(j, HIGH_FIT, dict(CFG, no_sponsor_companies=ns))
    assert im.hard_stop(dict(j, company="Acme"), ns)[0] is False   # not on the list -> unaffected


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
