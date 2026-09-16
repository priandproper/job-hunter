#!/usr/bin/env python3
"""Phase 17 — coverage for items the brief names that weren't already pinned down:
closed/stale-job handling, the atomic-publish guard, recruiting false positives, and
event-state migration via the snapshot fallback. (The rest of the brief's list is
covered by the per-phase suites: title/seniority/quals=phase1, immigration=phase2,
requirement-evidence=phase8, fact bank=phase4, résumé validation=phase5, dedup=phase11,
ranking fallback/invalid-LLM=phase15, public/private=phase16.)

Zero-dependency (pytest-collectable too).
"""

import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import worker  # noqa: E402
from lib import match, ranking, state  # noqa: E402

TODAY = dt.date(2026, 9, 15)


# --- closed / stale job handling (Phase 10 freshness bands as the closure proxy) -----
def test_stale_and_expired_jobs_deprioritized_then_excluded():
    # 31–45d = archive (near-zero weight unless manually active); >45d = expired.
    assert ranking.freshness(40)["band"] == "archive" and ranking.freshness(40)["weight"] <= 0.15
    assert ranking.freshness(60)["weight"] == 0.0
    fresh = ranking.rank_score({"priority": {"total": 80}, "posted_at": "2026-09-14"}, {}, today=TODAY)
    stale = ranking.rank_score({"priority": {"total": 80}, "posted_at": "2026-08-01"}, {}, today=TODAY)
    assert fresh["score"] > stale["score"]      # a fresh role outranks an equally-strong stale one


def test_closed_job_leaves_active_queue():
    # a job marked closed/rejected in the event state is dropped from the active queue
    folded = {"j1": {"stage": "closed", "stages": {"applied", "closed"}, "ts": "", "snooze_until": None}}
    q = ranking.order_active_queue([{"id": "j1", "title": "PMM", "company": "X",
                                     "priority": {"total": 90}, "posted_at": "2026-09-14"}],
                                   folded, today=TODAY)
    assert q == []


# --- atomic-publish guard (never overwrite a good board with an empty one) -----------
def test_publish_guard():
    assert worker._should_publish(200, 190) is True     # normal
    assert worker._should_publish(5, 0) is True         # fresh run, nothing to protect
    assert worker._should_publish(0, 0) is True         # genuinely empty, nothing lost
    assert worker._should_publish(0, 200) is False      # 0 now but 200 existed -> DON'T overwrite


# --- recruiting / sales false positives are excluded, real analyst titles kept -------
def test_recruiting_and_sales_false_positives_excluded():
    excl = match.DEFAULT_EXCLUDE_TITLE_TERMS
    # quota-carrying / senior sales titles are excluded
    assert match.excluded_title("Account Executive, Mid-Market", excl)
    assert match.excluded_title("Enterprise Sales Manager", excl)
    # entry-level BDR/SDR are NOW allowed (candidate is open to them) — not excluded, on-target
    assert not match.excluded_title("Sales Development Representative", excl)
    assert match.on_target("Sales Development Representative")
    assert match.on_target("Business Development Representative")
    # the analyst lane is unaffected by the sales exclusions and is on-target
    assert not match.excluded_title("Sales Operations Analyst", excl)
    assert match.on_target("Sales Operations Analyst")


# --- event-state migration: snapshot fallback when there's no event log --------------
def test_event_state_snapshot_migration():
    # a pre-event-log export (snapshot only) still yields the right skip set
    data = {"events": [], "snapshot": {
        "statuses": {"a": {"status": "applied"}, "b": {"status": "new"}},
        "dismissed": ["c"]}}
    assert state.skip_ids(data) == {"a", "c"}
    # and the event log takes precedence when present
    data2 = {"events": [{"job_id": "b", "event_type": "applied", "timestamp": "t"}],
             "snapshot": {}}
    assert state.skip_ids(data2) == {"b"}


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
