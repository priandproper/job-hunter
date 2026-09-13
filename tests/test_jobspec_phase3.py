#!/usr/bin/env python3
"""Phase 3 tests — structured job representation (requirement-to-evidence foundation).

Covers JD sectioning (responsibilities / basic vs preferred qualifications), the
Amazon-style inline " - " bullet split, required-years from the basic section,
salary extraction ($ and USD forms), boilerplate filtering, and the no-headers
fallback that still preserves the full cleaned JD.

Zero-dependency: `python3 tests/test_jobspec_phase3.py` (pytest-collectable too).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import jobspec  # noqa: E402

_JD = """About the role
We build analytics products for marketing teams.

Responsibilities:
- Own go-to-market messaging for the analytics suite
- Partner with sales on enablement and competitive positioning
- Run A/B tests and report on funnel conversion

Basic Qualifications:
- 4+ years of product marketing experience
- Bachelor's degree or equivalent
- Experience with SQL and Tableau

Preferred Qualifications:
- MBA or equivalent
- Experience in B2B SaaS

The salary range for this role is $120,000 - $160,000 per year.
We are an equal opportunity employer and do not discriminate.
"""


def _job(excerpt, title="Product Marketing Manager", company="Acme",
         location="Boston, MA / Remote, US", posted="2026-09-01"):
    return {"id": "abc123", "title": title, "company": company, "excerpt": excerpt,
            "location": location, "posted_at": posted}


# 1) Headers split the JD into the right buckets.
def test_sections_bucketed_correctly():
    s = jobspec.structure(_job(_JD))
    assert any("messaging" in r for r in s["responsibilities"])
    assert any("product marketing experience" in b for b in s["basic_qualifications"])
    assert any("MBA" in p for p in s["preferred_qualifications"])


# 2) Preferred quals are separated from basic (not merged).
def test_preferred_not_in_basic():
    s = jobspec.structure(_job(_JD))
    basic_text = " ".join(s["basic_qualifications"]).lower()
    assert "mba" not in basic_text
    assert "b2b saas" not in basic_text


# 3) required_years comes from the basic section (Phase 1 semantics).
def test_required_years_from_basic():
    assert jobspec.structure(_job(_JD))["required_years"] == 4


# 4) Salary is extracted ($ range form).
def test_salary_dollar_form():
    assert jobspec.structure(_job(_JD))["salary"] == "$120,000 - $160,000"


# 5) EEO boilerplate is not captured as a qualification bullet.
def test_boilerplate_filtered():
    s = jobspec.structure(_job(_JD))
    allb = " ".join(s["basic_qualifications"] + s["preferred_qualifications"]).lower()
    assert "equal opportunity" not in allb


# 6) Amazon-style inline " - " separators split into distinct bullets, and a
#    "$"-less USD pay range is captured as salary (and kept out of the sections).
def test_amazon_inline_split_and_usd_salary():
    jd = ("Basic qualifications:\n3+ years of analytics experience - 5+ years of Excel "
          "experience - Bachelor's degree\nPreferred qualifications:\nMBA\n"
          "82,700.00 - 130,100.00 USD annually")
    s = jobspec.structure(_job(jd))
    assert len(s["basic_qualifications"]) >= 3, s["basic_qualifications"]
    assert any(b.startswith("3+ years of analytics") for b in s["basic_qualifications"])
    assert any(b.startswith("5+ years of Excel") for b in s["basic_qualifications"])
    assert s["salary"] and "130,100" in s["salary"]
    assert not any("USD annually" in b for b in s["preferred_qualifications"])


# 7) A numeric range is NOT torn apart by the inline-dash split.
def test_numeric_range_not_split():
    jd = "Basic Qualifications:\n- Compensation is $120,000 - $160,000 for this role"
    s = jobspec.structure(_job(jd))
    # the one bullet should keep the whole range together, not split into "$120,000"/"$160,000"
    joined = " ".join(s["basic_qualifications"])
    assert "$120,000 - $160,000" in joined or s["salary"] == "$120,000 - $160,000"


# 8) No recognizable headers -> empty sections, but full JD preserved + years still work.
def test_no_headers_fallback():
    jd = "We need someone with 5+ years of experience to own our marketing narrative."
    s = jobspec.structure(_job(jd))
    assert s["responsibilities"] == [] and s["basic_qualifications"] == []
    assert s["full_cleaned_jd"] == jd            # preserved
    assert s["required_years"] == 5              # eligibility fallback (Phase 1)


# 9) Locations parse into a list; date_posted carried through.
def test_locations_and_date():
    s = jobspec.structure(_job(_JD))
    assert "Boston, MA" in s["locations"] or "Boston" in " ".join(s["locations"])
    assert s["date_posted"] == "2026-09-01"


# 10) sponsorship_text captures the sponsorship-related sentence(s) as source text.
def test_sponsorship_text_captured():
    jd = ("Own GTM launches. Note: we are unable to sponsor employment visas for this role. "
          "Great benefits.")
    s = jobspec.structure(_job(jd))
    assert any("sponsor" in t.lower() for t in s["sponsorship_text"])


# 11) include_full=False omits the full JD (the caller already stores `excerpt`).
def test_include_full_false_omits_jd():
    s = jobspec.structure(_job(_JD), include_full=False)
    assert s["full_cleaned_jd"] == ""


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
