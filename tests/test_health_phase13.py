#!/usr/bin/env python3
"""Phase 13 tests — daily-run health record.

Covers status classification (success/partial/failed), the health-record schema,
last_full_success carry-forward, staleness detection, atomic save/load, and the
coach_status update. Zero-dependency (pytest-collectable too).
"""

import datetime as dt
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import health as h  # noqa: E402


# 1) Status classification.
def test_classify():
    assert h.classify(200, 5, []) == "success"
    assert h.classify(0, 5, []) == "failed"                 # nothing matched
    assert h.classify(200, 0, ["a", "b"]) == "failed"       # every source failed
    assert h.classify(5, 3, [], min_jobs=10) == "partial"   # below floor
    assert h.classify(200, 1, ["a", "b"]) == "partial"      # >50% sources failed


# 2) build_record has the full schema and correct duration.
def test_record_schema_and_duration():
    r = h.build_record("run1", "2026-09-14T10:00:00", "2026-09-14T10:02:30",
                       jobs_fetched=500, jobs_matched=200, jobs_ranked=45,
                       sources_succeeded=4, sources_failed=["lever"])
    for k in ("run_id", "started_at", "completed_at", "status", "jobs_fetched",
              "jobs_matched", "jobs_ranked", "sources_succeeded", "sources_failed",
              "duration_seconds", "coach_status", "last_full_success"):
        assert k in r
    assert r["duration_seconds"] == 150
    # 1 failed of 5 total = 20% (not >50%) and 200 jobs >= floor -> success
    assert r["status"] == "success"


# 3) last_full_success is stamped on success and carried forward otherwise.
def test_last_full_success_carry_forward():
    ok = h.build_record("r1", "2026-09-14T10:00:00", "2026-09-14T10:01:00",
                        jobs_matched=200, sources_succeeded=4)
    assert ok["last_full_success"] == "2026-09-14T10:01:00"
    bad = h.build_record("r2", "2026-09-15T10:00:00", "2026-09-15T10:01:00",
                         jobs_matched=0, sources_succeeded=0, prior=ok)
    assert bad["status"] == "failed"
    assert bad["last_full_success"] == "2026-09-14T10:01:00"   # carried, not overwritten


# 4) Staleness: success within window fresh; old / non-success / missing = stale.
def test_is_stale():
    now = dt.datetime(2026, 9, 14, 12, 0, 0, tzinfo=dt.timezone.utc)
    fresh = h.build_record("r", "2026-09-14T10:00:00", "2026-09-14T11:00:00",
                           jobs_matched=200, sources_succeeded=4)
    assert h.is_stale(fresh, now=now) is False
    old = h.build_record("r", "2026-09-10T10:00:00", "2026-09-10T11:00:00",
                         jobs_matched=200, sources_succeeded=4)
    assert h.is_stale(old, now=now) is True                    # >36h old
    partial = h.build_record("r", "2026-09-14T10:00:00", "2026-09-14T11:00:00",
                             jobs_matched=5, sources_succeeded=4)
    assert h.is_stale(partial, now=now) is True                # non-success
    assert h.is_stale({}, now=now) is True                     # missing


# 5) Atomic save/load round-trips.
def test_save_load_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "docs" / "health.json"
        rec = h.build_record("r", "2026-09-14T10:00:00", "2026-09-14T10:01:00",
                             jobs_matched=200, sources_succeeded=4)
        h.save(p, rec)
        assert h.load(p)["run_id"] == "r"
        assert not any(x.name.endswith(".tmp") for x in p.parent.iterdir())  # no temp left


# 6) coach_status is updated in place without touching the rest.
def test_set_coach_status():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "health.json"
        h.save(p, h.build_record("r", "2026-09-14T10:00:00", "2026-09-14T10:01:00",
                                 jobs_matched=200, sources_succeeded=4))
        h.set_coach_status(p, "fallback")
        rec = h.load(p)
        assert rec["coach_status"] == "fallback" and rec["jobs_matched"] == 200
        assert h.set_coach_status(Path(d) / "nope.json", "success") == {}  # no file -> no-op


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
