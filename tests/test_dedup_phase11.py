#!/usr/bin/env python3
"""Phase 11 tests — duplicate & repost detection.

Covers the six signals and five classes: exact id/url, normalized company+title+
location, JD fingerprint, JD similarity, same-team/different-id, cross-company =
unrelated, pool repost annotation (canonical stays clean), and the previously-rejected
resemblance signal. Zero-dependency (pytest-collectable too).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import dedup as dd  # noqa: E402

JD = ("We are hiring a product marketing manager to own go-to-market messaging, "
      "positioning, competitive analysis, sales enablement and launch strategy for "
      "our B2B SaaS analytics platform across enterprise segments. " * 3)
JD_OTHER = ("Seeking a data analyst to build SQL and Tableau dashboards, run cohort "
            "and funnel analysis, and partner with revenue operations on reporting. " * 3)


def _j(jid, company="Acme", title="Product Marketing Manager", loc="Boston, MA",
       url="", excerpt=JD, dept="", first=""):
    return {"id": jid, "company": company, "title": title, "location": loc,
            "url": url, "excerpt": excerpt, "department": dept, "_first_seen": first}


# 1) Same id / same url -> exact duplicate.
def test_exact_id_and_url():
    assert dd.classify(_j("x"), _j("x"))["classification"] == "exact_duplicate"
    a = _j("a1", url="https://acme.com/jobs/1"); b = _j("b1", url="https://acme.com/jobs/1")
    r = dd.classify(a, b)
    assert r["classification"] == "exact_duplicate" and r["confidence"] >= 0.95


# 2) Same normalized company+title+location (different id) -> exact duplicate.
def test_normalized_company_title_location():
    a = _j("a1", title="Sr. Product Marketing Manager (Remote) #4471")
    b = _j("b1", title="Product Marketing Manager", loc="Remote")
    # titles normalize equal (seniority/req/remote stripped); location both remote
    r = dd.classify(a, b)
    assert r["classification"] in ("exact_duplicate", "likely_repost")


# 3) Same title, different location -> likely repost.
def test_same_title_diff_location_repost():
    r = dd.classify(_j("a1", loc="Boston, MA"), _j("b1", loc="New York, NY"))
    assert r["classification"] == "likely_repost"


# 4) Identical JD fingerprint with different title -> likely repost.
def test_jd_fingerprint_repost():
    a = _j("a1", title="Marketing Manager, Growth")
    b = _j("b1", title="Growth Marketing Manager")   # different normalized title
    assert dd.fingerprint(a) == dd.fingerprint(b) and dd.fingerprint(a) != ""
    assert dd.classify(a, b)["classification"] == "likely_repost"


# 5) Same team, shared template JD, different role -> separate headcount.
def test_separate_headcount_same_template():
    base = ("Join the marketing team. Responsibilities include campaign execution, "
            "reporting, and stakeholder coordination across programs. ")
    a = _j("a1", title="Campaign Manager", excerpt=base + "Focus on email campaigns.", dept="Marketing")
    b = _j("b1", title="Events Manager", excerpt=base + "Focus on field events.", dept="Marketing")
    r = dd.classify(a, b)
    assert r["classification"] in ("separate_headcount", "same_role_family")


# 6) Same company, same lane, unrelated product -> same_role_family; different company -> unrelated.
def test_role_family_and_unrelated():
    a = _j("a1", title="Product Marketing Manager, Payments", excerpt=JD)
    b = _j("b1", title="Product Marketing Manager, Security", excerpt=JD[:120] + " security compliance zero trust endpoint")
    assert dd.classify(a, b)["classification"] in ("same_role_family", "separate_headcount", "likely_repost")
    # different company is unrelated regardless of similar JD
    assert dd.classify(_j("a1", company="Acme"), _j("b1", company="Globex"))["classification"] == "unrelated"


# 7) Pool annotation: canonical (earliest) stays clean; the repost gets a `duplicate`.
def test_annotate_reposts():
    jobs = [
        _j("first", loc="Boston, MA", first="2026-09-01"),
        _j("repost", loc="New York, NY", first="2026-09-10"),
        _j("other", company="Globex", title="Data Analyst", excerpt=JD_OTHER, first="2026-09-05"),
    ]
    n = dd.annotate_reposts(jobs)
    assert n == 1
    canon = [j for j in jobs if j["id"] == "first"][0]
    rep = [j for j in jobs if j["id"] == "repost"][0]
    assert "duplicate" not in canon
    assert rep["duplicate"]["of"] == "first"
    assert rep["duplicate"]["classification"] == "likely_repost"
    assert rep["duplicate"]["evidence"]           # evidence retained


# 8) Previously-rejected resemblance (signal 6).
def test_resembles_rejected():
    rejected = [_j("rej1", title="Product Marketing Manager", excerpt=JD)]
    new = _j("new1", title="Product Marketing Manager", loc="Remote", excerpt=JD)
    m = dd.resembles_rejected(new, rejected)
    assert m and m["of"] == "rej1" and m["confidence"] >= 0.5
    # an unrelated new role resembles nothing
    unrel = _j("new2", title="Data Analyst", excerpt=JD_OTHER)
    assert dd.resembles_rejected(unrel, rejected) is None


# 9) JD similarity is symmetric-ish and bounded [0,1]; empty JD -> 0.
def test_similarity_bounds():
    s = dd.jd_similarity(_j("a"), _j("b"))
    assert 0.0 <= s <= 1.0 and s > 0.9      # identical JD
    assert dd.jd_similarity(_j("a", excerpt=""), _j("b")) == 0.0


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
