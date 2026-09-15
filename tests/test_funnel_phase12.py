#!/usr/bin/env python3
"""Phase 12 tests — funnel analytics.

Covers per-application dimension extraction, the call/interview/final/offer stage
distinctions, overall + by-dimension conversions, the 30-application report trigger,
the baseline comparison, and the diagnostic. Zero-dependency (pytest-collectable too).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import funnel as fn  # noqa: E402


def _ev(job, et, ts, **meta):
    return {"job_id": job, "event_type": et, "timestamp": ts, "metadata": meta}


def _job(jid, company="Acme", title="Product Marketing Manager", source="greenhouse",
         risk="yellow", posted="2026-09-01", band="B", basic_frac=0.8, years=4):
    return {"id": jid, "company": company, "title": title, "source": source,
            "posted_at": posted, "immigration": {"risk": risk},
            "spec": {"required_years": years},
            "priority": {"band": band, "components": [
                {"name": "basic_qualifications", "points": basic_frac * 25, "max": 25}]}}


JOBS = {
    "a": _job("a", title="Product Marketing Manager", risk="green"),
    "b": _job("b", title="Marketing Analyst"),
    "c": _job("c", title="Product Marketing Manager", risk="red"),  # hard-stop role
}


# 1) An application record carries the tracked dimensions.
def test_application_dimensions():
    events = [_ev("a", "outreach_sent", "2026-09-05T00:00:00", type="referral"),
              _ev("a", "applied", "2026-09-08T00:00:00")]
    apps = fn.build_applications(events, JOBS)
    assert len(apps) == 1
    r = apps[0]
    assert r["company"] == "Acme" and r["lane"] == "pmm" and r["source"] == "greenhouse"
    assert r["sponsorship"] == "green" and r["warm"] is True and r["referral_type"] == "referral"
    assert r["posting_age_days"] == 7 and r["age_bucket"] == "0-7"
    assert r["exp_bucket"] == "3-6" and r["qualified"] is True


# 2) Only jobs with an 'applied' event become applications.
def test_only_applied_counts():
    events = [_ev("a", "saved", "t"), _ev("b", "applied", "2026-09-08T00:00:00")]
    apps = fn.build_applications(events, JOBS)
    assert {a["job_id"] for a in apps} == {"b"}


# 3) Stage distinctions: call vs substantive interview vs final vs offer.
def test_stage_distinctions():
    events = [_ev("a", "applied", "2026-09-08T00:00:00"),
              _ev("a", "recruiter_screen", "2026-09-10T00:00:00"),
              _ev("a", "interview", "2026-09-12T00:00:00", round="final"),
              _ev("a", "offer", "2026-09-15T00:00:00")]
    r = fn.build_applications(events, JOBS)[0]
    assert r["reached_call"] and r["reached_interview"] and r["reached_final"] and r["offer"]
    # a call-only application is not an interview
    e2 = [_ev("b", "applied", "t"), _ev("b", "recruiter_screen", "t")]
    r2 = fn.build_applications(e2, JOBS)[0]
    assert r2["reached_call"] and not r2["reached_interview"] and not r2["reached_final"]


# 4) Overall conversions compute correctly.
def test_overall_conversions():
    events = []
    for i in range(10):                     # 10 applied
        events.append(_ev(f"j{i}", "applied", "2026-09-08T00:00:00"))
    for i in range(4):                      # 4 calls
        events.append(_ev(f"j{i}", "recruiter_screen", "2026-09-10T00:00:00"))
    for i in range(2):                      # 2 interviews
        events.append(_ev(f"j{i}", "interview", "2026-09-12T00:00:00"))
    events.append(_ev("j0", "offer", "2026-09-15T00:00:00"))
    jobs = {f"j{i}": _job(f"j{i}") for i in range(10)}
    ov = fn.conversions(fn.build_applications(events, jobs))["overall"]
    assert ov["applications"] == 10 and ov["app_to_call"] == 40.0
    assert ov["app_to_interview"] == 20.0 and ov["interview_to_offer"] == 50.0


# 5) By-dimension breakdowns exist for every listed dimension.
def test_by_dimension_breakdowns():
    events = [_ev("a", "applied", "2026-09-08T00:00:00"),
              _ev("b", "applied", "2026-09-08T00:00:00")]
    conv = fn.conversions(fn.build_applications(events, JOBS))
    for k in ("by_lane", "by_resume", "by_source", "by_warm_cold", "by_sponsorship",
              "by_age", "by_experience"):
        assert k in conv
    assert set(conv["by_lane"]) == {"pmm", "analyst"}


# 6) The 30-application report trigger.
def test_report_trigger():
    assert fn.should_report(30) and fn.should_report(60)
    assert not fn.should_report(0) and not fn.should_report(29) and not fn.should_report(31)


# 7) Diagnostic answers the brief's questions, incl. the baseline comparison.
def test_diagnostic():
    events = []
    for i in range(6):
        events.append(_ev(f"p{i}", "applied", "2026-09-08T00:00:00"))
        if i < 3:
            events.append(_ev(f"p{i}", "interview", "2026-09-12T00:00:00"))
    for i in range(4):
        events.append(_ev(f"n{i}", "applied", "2026-09-08T00:00:00"))
    jobs = {**{f"p{i}": _job(f"p{i}", title="Product Marketing Manager") for i in range(6)},
            **{f"n{i}": _job(f"n{i}", title="Marketing Analyst") for i in range(4)}}
    apps = fn.build_applications(events, jobs)
    d = fn.diagnostic(apps)
    for q in ("what_is_converting", "what_is_not_converting", "where_funnel_fails",
              "which_lane_more_volume", "which_resume_needs_revision",
              "immigration_filtered_early_enough", "applications_fast_enough", "vs_baseline"):
        assert q in d and d[q]
    assert "pmm" in d["what_is_converting"]          # pmm converts, analyst doesn't
    assert "baseline" in d["vs_baseline"]


# 8) A red (hard-stop) application is not counted qualified and is flagged by the diagnostic.
def test_red_application_flagged():
    events = [_ev("c", "applied", "2026-09-08T00:00:00")]
    apps = fn.build_applications(events, JOBS)
    assert apps[0]["qualified"] is False
    d = fn.diagnostic(apps)
    assert "red" in d["immigration_filtered_early_enough"].lower()


def _run():
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    failed = 0
    for name, fn_ in tests:
        try:
            fn_(); print(f"  ok   {name}")
        except AssertionError as e:
            failed += 1; print(f"  FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
