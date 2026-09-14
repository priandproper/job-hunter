#!/usr/bin/env python3
"""Phase 10 tests — ranking memory & freshness.

Covers the freshness bands (incl. the strong-only 15–30 and archive 31–45 rules),
lifecycle staging from the Phase 9 event state, the rule that a STRONG role is not
demoted for repeat exposure, inactive jobs leaving the active queue, and company/lane
diversity in the ordered queue. Zero-dependency (pytest-collectable too).
"""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import ranking as rk  # noqa: E402

TODAY = dt.date(2026, 9, 14)


def _job(jid, band_total, posted, title="Product Marketing Manager", company="Acme", ref=False):
    j = {"id": jid, "title": title, "company": company, "posted_at": posted,
         "priority": {"total": band_total}}
    if ref:
        j["_has_referral"] = True
    return j


def _fold(**stages):
    # stages: job_id -> (latest_stage, {stages}, ts)
    out = {}
    for jid, (latest, sset, ts) in stages.items():
        out[jid] = {"stage": latest, "stages": set(sset), "ts": ts, "snooze_until": None}
    return out


# 1) Freshness bands map age -> weight, newest highest.
def test_freshness_bands():
    assert rk.freshness(2)["band"] == "highest"
    assert rk.freshness(6)["band"] == "high"
    assert rk.freshness(12)["band"] == "moderate"
    assert rk.freshness(40)["band"] == "archive"
    assert rk.freshness(60)["band"] == "expired" and rk.freshness(60)["weight"] == 0.0


# 2) 15–30d counts only when the role is strong; archive needs manual verification.
def test_aging_and_archive_exceptions():
    assert rk.freshness(20, strong=False)["weight"] == 0.30
    assert rk.freshness(20, strong=True)["weight"] == 0.45      # full aging weight
    assert rk.freshness(40, manually_active=False)["weight"] == 0.12
    assert rk.freshness(40, manually_active=True)["weight"] == 0.20


# 3) Lifecycle staging from the event state.
def test_lifecycle_stages():
    f = _fold(a=("saved", {"saved"}, "2026-09-13T00:00:00"),
              b=("resume_generated", {"saved", "resume_generated"}, "2026-09-13T00:00:00"),
              c=("outreach_sent", {"outreach_sent"}, "2026-09-05T00:00:00"),
              d=("applied", {"saved", "applied"}, "2026-09-10T00:00:00"),
              e=("dismissed", {"dismissed"}, "2026-09-10T00:00:00"))
    assert rk.lifecycle_stage(_job("a", 60, "2026-09-13"), f, TODAY) == "saved_not_applied"
    assert rk.lifecycle_stage(_job("b", 60, "2026-09-13"), f, TODAY) == "application_started"
    assert rk.lifecycle_stage(_job("c", 60, "2026-09-05"), f, TODAY) == "follow_up_due"  # >3d old
    assert rk.lifecycle_stage(_job("d", 60, "2026-09-10"), f, TODAY) == "applied"        # sticky
    assert rk.lifecycle_stage(_job("e", 60, "2026-09-10"), f, TODAY) == "dismissed"
    assert rk.lifecycle_stage(_job("z", 60, "2026-09-13"), {}, TODAY) == "new_unreviewed"


# 4) Inactive jobs (applied/dismissed/snoozed/…) drop out of the active queue.
def test_inactive_dropped():
    f = _fold(d=("applied", {"applied"}, "t"))
    r = rk.rank_score(_job("d", 90, "2026-09-13"), f, today=TODAY)
    assert r["active"] is False and r["score"] is None


# 5) A STRONG role is NOT demoted for repeat exposure; a weak one is (capped).
def test_strong_role_not_demoted_by_exposure():
    strong = rk.rank_score(_job("s", 85, "2026-09-13"), {}, times_shown=20, today=TODAY)
    assert strong["decay"] == 0                       # waived for strong
    weak = rk.rank_score(_job("w", 50, "2026-09-13"), {}, times_shown=20, today=TODAY)
    assert weak["decay"] == 5.0                       # capped at 5, not 20
    # same weak role shown 0 times scores higher than shown many times
    weak0 = rk.rank_score(_job("w", 50, "2026-09-13"), {}, times_shown=0, today=TODAY)
    assert weak0["score"] > weak["score"]


# 6) An application-started role gets the biggest lifecycle nudge (finish it).
def test_started_nudge_beats_new():
    f = _fold(b=("resume_generated", {"resume_generated"}, "t"))
    started = rk.rank_score(_job("b", 60, "2026-09-13"), f, today=TODAY)
    new = rk.rank_score(_job("n", 60, "2026-09-13"), {}, today=TODAY)
    assert started["nudge"] > new["nudge"]
    assert started["score"] > new["score"]


# 7) Company/lane diversity re-orders the queue so one employer can't dominate the top.
def test_diversity_reordering():
    jobs = [
        _job("a1", 90, "2026-09-13", company="BigCo", title="Product Marketing Manager"),
        _job("a2", 88, "2026-09-13", company="BigCo", title="Product Marketing Lead"),
        _job("a3", 86, "2026-09-13", company="BigCo", title="Senior PMM"),
        _job("b1", 80, "2026-09-13", company="OtherCo", title="Marketing Analyst"),
    ]
    order = rk.order_active_queue(jobs, {}, today=TODAY)
    ids = [x["job"]["id"] for x in order]
    # OtherCo (80) should appear before BigCo's 3rd role despite lower base score
    assert ids.index("b1") < ids.index("a3"), ids
    # the very top is still BigCo's best
    assert ids[0] == "a1"


# 8) order_active_queue drops inactive jobs entirely.
def test_queue_excludes_inactive():
    f = _fold(x=("dismissed", {"dismissed"}, "t"))
    jobs = [_job("x", 95, "2026-09-13"), _job("y", 60, "2026-09-13")]
    ids = [o["job"]["id"] for o in rk.order_active_queue(jobs, f, today=TODAY)]
    assert ids == ["y"]


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
