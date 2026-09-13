#!/usr/bin/env python3
"""Phase 6 tests — transparent priority score with hard gates.

Covers the band thresholds, the immigration hard gate (always Reject), the weighted
components, referral/recency scaling, the uncertainty signal, and that the total is
the sum of the components. Zero-dependency (pytest-collectable too).
"""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import priority as pr  # noqa: E402

TODAY = dt.date(2026, 9, 13)
CTX = {
    "skills": {"sql", "tableau", "product marketing", "gtm", "segmentation",
               "funnel", "analytics", "positioning", "messaging", "excel"},
    "experience_terms": {"campaign", "conversion", "cohort", "dashboard", "pipeline"},
    "domains": pr.DEFAULT_DOMAINS,
}

STRONG_JOB = {
    "title": "Product Marketing Manager",
    "location": "Boston, MA",
    "posted_at": "2026-09-10",
    "excerpt": ("We build B2B SaaS analytics for enterprise marketing teams.\n"
                "Responsibilities:\n- Own go-to-market positioning and messaging\n"
                "- Run funnel and campaign analytics in SQL and Tableau\n"
                "Basic Qualifications:\n- 4+ years of product marketing experience\n"
                "- Experience with SQL and Tableau\n- Segmentation and positioning experience"),
}
GREEN = {"risk": "green"}
RED = {"risk": "red", "hard_stop_reason": "citizenship"}


def _s(job, **kw):
    return pr.score(job, ctx=CTX, today=TODAY, **kw)


# 1) A strong, sponsor-friendly, recent, local role with referrals -> Priority A, no hard stop.
def test_strong_role_is_priority_a():
    rep = _s(STRONG_JOB, immigration=GREEN, referral_count=3)
    assert rep["band"] == "A", rep
    assert rep["total"] >= 75 and not rep["hard_stops"]


# 2) Immigration red is ALWAYS Reject, even with everything else strong.
def test_immigration_red_forces_reject():
    rep = _s(STRONG_JOB, immigration=RED, referral_count=3)
    assert rep["band"] == "Reject"
    assert rep["hard_stops"] == ["citizenship"]


# 3) A weak role (no matching skills, no referral, stale, unknown sponsorship) is low.
def test_weak_role_is_low():
    weak = {"title": "Marketing Manager", "location": "Austin, TX",
            "posted_at": "2026-06-01",
            "excerpt": ("Basic Qualifications:\n- 5+ years of field marketing and event "
                        "logistics\n- Experience with trade shows and booth management")}
    rep = pr.score(weak, ctx={"skills": set(), "experience_terms": set()},
                   today=TODAY, immigration={"risk": "yellow"}, referral_count=0)
    assert rep["band"] in ("C", "Reject")
    assert rep["total"] < 60


# 4) All seven components are present, each within [0, max].
def test_components_present_and_bounded():
    rep = _s(STRONG_JOB, immigration=GREEN)
    names = {c["name"] for c in rep["components"]}
    assert names == {"basic_qualifications", "direct_experience", "immigration",
                     "product_customer", "referral_access", "recency", "location"}
    for c in rep["components"]:
        assert 0 <= c["points"] <= c["max"]
        assert c["confidence"] in ("low", "medium", "high")
        assert c["evidence"]


# 5) The total is the (rounded) sum of the component points.
def test_total_is_sum_of_components():
    rep = _s(STRONG_JOB, immigration=GREEN, referral_count=2)
    assert rep["total"] == round(sum(c["points"] for c in rep["components"]))


# 6) Band thresholds.
def test_band_thresholds():
    assert pr._band(80, False) == "A"
    assert pr._band(74, False) == "B"
    assert pr._band(60, False) == "B"
    assert pr._band(59, False) == "C"
    assert pr._band(45, False) == "C"
    assert pr._band(44, False) == "Reject"
    assert pr._band(95, True) == "Reject"      # hard stop overrides


# 7) Referral access scales 0 -> 5 -> 10.
def test_referral_scaling():
    def ref_pts(n):
        rep = _s(STRONG_JOB, immigration=GREEN, referral_count=n)
        return next(c["points"] for c in rep["components"] if c["name"] == "referral_access")
    assert ref_pts(0) == 0.0 and ref_pts(1) == 5.0 and ref_pts(4) == 10.0


# 8) Recency scales with posting age.
def test_recency_scaling():
    def rec_pts(posted):
        j = dict(STRONG_JOB, posted_at=posted)
        rep = _s(j, immigration=GREEN)
        return next(c["points"] for c in rep["components"] if c["name"] == "recency")
    assert rec_pts("2026-09-10") == 5.0        # ~3d
    assert rec_pts("2026-08-28") == 3.0        # ~16d
    assert rec_pts("2026-06-01") == 0.0        # stale


# 9) No JD body -> basic-quals low confidence -> high uncertainty (score is a guide).
def test_uncertainty_high_without_jd():
    j = {"title": "Product Marketing Manager", "location": "Boston, MA",
         "posted_at": "2026-09-10", "excerpt": ""}
    rep = pr.score(j, ctx=CTX, today=TODAY, immigration={"risk": "yellow"})
    assert rep["uncertainty"] == "high"


# 10) 'why' explains the classification and names the band.
def test_why_explains():
    rep = _s(STRONG_JOB, immigration=GREEN, referral_count=3)
    assert rep["band"] in rep["why"]
    red = _s(STRONG_JOB, immigration=RED)
    assert "hard stop" in red["why"].lower()


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
