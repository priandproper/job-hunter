#!/usr/bin/env python3
"""Phase 1 targeting tests — the 3–6-year band + no-blanket-seniority rules.

The candidate has 4+ years of relevant experience. Targeting shifted FROM
"non-senior, under ~4 yrs (analyst 0–3)" TO "roles asking for ~3–6 years
(stretch 7; 8+ excluded)", with title seniority no longer a hard filter and
Basic (eligibility) vs Preferred (ranking) qualifications distinguished.

Zero-dependency: run directly with `python3 tests/test_match_phase1.py`
(also collected by pytest if it's installed — every check is a test_* function).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import match as m  # noqa: E402

# A permissive config: fit is stubbed high so these tests isolate the title /
# experience rules, and the default exclude terms apply.
CFG = {"min_fit_score": 30, "exclude_sponsorship": ["No"],
       "experience": {"exclude_at_years": 8}}
HIGH_FIT = {"fit_score": 99}


def _job(title, excerpt="", location="Boston, MA", sponsorship="Unknown"):
    return {"title": title, "excerpt": excerpt, "location": location,
            "sponsorship": sponsorship}


def _passes(title, excerpt=""):
    return m.passes_filters(_job(title, excerpt), HIGH_FIT, CFG)


# 1) The primary band (3–6 yrs) is kept.
def test_primary_band_3_to_6_years_kept():
    for n in (3, 4, 5, 6):
        jd = f"Basic qualifications: {n}+ years of marketing experience."
        assert m.experience_ok(_job("Marketing Manager", jd), CFG), f"{n} yrs should pass"
        assert _passes("Marketing Manager", jd), f"{n} yrs should pass end-to-end"


# 2) The stretch year (7) is kept, but 8+ is excluded (the hard boundary).
def test_seven_years_stretch_kept_eight_excluded():
    kept = "Minimum 7 years of product marketing experience required."
    gone = "Minimum 8 years of product marketing experience required."
    assert m.experience_ok(_job("Product Marketing Manager", kept), CFG)
    assert not m.experience_ok(_job("Product Marketing Manager", gone), CFG)
    assert not _passes("Product Marketing Manager", gone)


# 3) A low bar (0–2 yrs) is still kept — over-qualification is a ranking concern,
#    not a reason to hard-drop the role.
def test_low_experience_kept():
    for n in (0, 1, 2):
        jd = f"Requires {n}+ years of experience in a marketing role."
        assert m.experience_ok(_job("Marketing Analyst", jd), CFG)


# 4) A "Senior" title is NOT hard-filtered any more — kept when years are in band.
def test_senior_title_not_blanket_excluded():
    jd = "Basic qualifications: 5+ years of product marketing experience."
    assert not m.excluded_title("Senior Product Marketing Manager", m.DEFAULT_EXCLUDE_TITLE_TERMS)
    assert _passes("Senior Product Marketing Manager", jd)


# 5) "Lead" / "Principal" / "Director" are no longer on the seniority BLOCKLIST
#    (the exact rule Phase 1 relaxed), so they're evaluated by function + years.
def test_lead_principal_director_not_on_blocklist():
    for title in ("Lead Product Marketing Manager",
                  "Principal Product Marketing Manager",
                  "Marketing Director", "Director of Product Marketing"):
        assert not m.excluded_title(title, m.DEFAULT_EXCLUDE_TITLE_TERMS), title


# 5b) A senior/lead/principal/director title that also names a target FUNCTION
#     passes end-to-end when its required-years bar is in band.
def test_senior_and_director_target_function_pass_end_to_end():
    jd = "Basic qualifications: 6+ years of relevant product marketing experience."
    for title in ("Lead Product Marketing Manager",
                  "Principal Product Marketing Manager",
                  "Director of Product Marketing"):
        assert _passes(title, jd), title


# 6) Clearly-executive titles ARE still excluded (VP / C-level / President / Head of).
def test_executive_titles_still_excluded():
    jd = "Basic qualifications: 5+ years of marketing experience."
    for title in ("VP of Marketing", "Chief Marketing Officer",
                  "President, Marketing", "Head of Marketing"):
        assert m.excluded_title(title, m.DEFAULT_EXCLUDE_TITLE_TERMS), title
        assert not _passes(title, jd), title


# 7) Basic vs Preferred: an 8+ figure that appears ONLY in the Preferred section is
#    a ranking signal, not eligibility — the role is kept.
def test_preferred_years_do_not_gate_eligibility():
    jd = ("Basic qualifications: 4+ years of product marketing experience. "
          "Preferred qualifications: 8+ years of experience leading GTM launches.")
    assert m.required_years(_job("Product Marketing Manager", jd)) == 4
    assert m.experience_ok(_job("Product Marketing Manager", jd), CFG)
    assert _passes("Product Marketing Manager", jd)


# 8) Total vs role-relevant: a large "total years" figure alongside a smaller
#    role-relevant one gates on the smaller (relevant) bar.
def test_total_vs_role_relevant_years():
    jd = ("Basic qualifications: 8+ years of overall professional experience, "
          "including 3+ years of experience in marketing.")
    assert m.required_years(_job("Marketing Manager", jd)) == 3
    assert m.experience_ok(_job("Marketing Manager", jd), CFG)
    assert _passes("Marketing Manager", jd)


# --- tiny zero-dependency runner (used when pytest isn't installed) -------------
# --- 2026-09-16 policy: hard 1–4yr target, senior titles removed, entry BDR/SDR in ---
STRICT = {"min_fit_score": 30, "exclude_sponsorship": ["No"],
          "experience": {"exclude_at_years": 5}, "exclude_senior_titles": True}


def test_strict_policy_experience_and_seniority():
    # 5+ years is excluded now; <=4 kept
    assert not m.passes_filters(_job("Marketing Manager",
                                     "Basic qualifications: 5+ years of marketing experience."), HIGH_FIT, STRICT)
    assert m.passes_filters(_job("Marketing Manager",
                                 "Basic qualifications: 4+ years of marketing experience."), HIGH_FIT, STRICT)
    # senior-titled roles are dropped even with no stated years
    for t in ("Senior Marketing Manager", "Sr. Product Marketing Manager",
              "Principal PMM", "Director of Marketing", "Head of Growth Marketing"):
        assert not m.passes_filters(_job(t, "Own GTM."), HIGH_FIT, STRICT), t
    # non-senior marketing + entry sales-dev pass
    for t in ("Marketing Manager", "Product Marketing Manager", "Marketing Analyst",
              "Business Development Representative", "Sales Development Representative (SDR)"):
        assert m.passes_filters(_job(t, "Own GTM."), HIGH_FIT, STRICT), t
    # "lead generation" is NOT treated as senior (word-boundary guard)
    assert not m.too_senior("Lead Generation Manager")
    assert m.too_senior("Senior Marketing Manager")


def _run():
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
