#!/usr/bin/env python3
"""Phase 9 tests — ingesting exported application state (Option A).

Covers the event fold (latest-wins), the skip set (applied/dismissed/closed/snoozed),
reversal (dismissed then re-viewed re-surfaces), snapshot fallback, and tolerance of a
missing/garbled file. Zero-dependency (pytest-collectable too).
"""

import datetime as dt
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import state as st  # noqa: E402


def _ev(job, et, ts, **meta):
    return {"job_id": job, "event_type": et, "timestamp": ts, "metadata": meta}


def _data(*events):
    return {"events": list(events), "snapshot": {}}


# 1) Applied / rejected / closed / dismissed are skipped; saved / in-progress are not.
def test_skip_set_basics():
    data = _data(
        _ev("a", "applied", "2026-09-10T10:00:00"),
        _ev("b", "dismissed", "2026-09-10T10:00:00"),
        _ev("c", "saved", "2026-09-10T10:00:00"),
        _ev("d", "resume_generated", "2026-09-10T10:00:00"),
        _ev("e", "rejected", "2026-09-10T10:00:00"),
        _ev("f", "closed", "2026-09-10T10:00:00"),
    )
    skip = st.skip_ids(data)
    assert skip == {"a", "b", "e", "f"}
    assert "c" not in skip and "d" not in skip   # saved / in-progress still surface


# 2) Interview/offer/recruiter_screen (applied and beyond) are skipped.
def test_pipeline_stages_skipped():
    data = _data(_ev("a", "recruiter_screen", "t"), _ev("b", "interview", "t"),
                 _ev("c", "offer", "t"), _ev("d", "outreach_sent", "t"))
    skip = st.skip_ids(data)
    assert {"a", "b", "c"} <= skip
    assert "d" not in skip     # outreach in progress, keep visible


# 3) Dismissed is reversible (a later 'viewed' re-surfaces), but a COMMITTED stage
#    (applied/interview/…) is STICKY — a later 'saved'/'viewed' never un-skips it.
def test_reversal_and_sticky_committed():
    # dismissed then re-viewed -> re-surfaces
    assert "a" not in st.skip_ids(_data(_ev("a", "dismissed", "2026-09-01T00:00:00"),
                                        _ev("a", "viewed", "2026-09-05T00:00:00")))
    # saved then applied -> skipped
    assert "b" in st.skip_ids(_data(_ev("b", "saved", "2026-09-01T00:00:00"),
                                    _ev("b", "applied", "2026-09-05T00:00:00")))
    # applied THEN saved (the real ordering bug) -> STILL skipped (applied is sticky)
    assert "c" in st.skip_ids(_data(_ev("c", "applied", "2026-09-01T00:00:00"),
                                    _ev("c", "saved", "2026-09-05T00:00:00")))
    # interviewed then saved -> still skipped
    assert "d" in st.skip_ids(_data(_ev("d", "interview", "2026-09-01T00:00:00"),
                                    _ev("d", "saved", "2026-09-05T00:00:00")))


# 4) Snooze skips only until its date passes.
def test_snooze_respects_date():
    data = _data(_ev("a", "snoozed", "2026-09-10T00:00:00", until="2026-09-20"))
    assert "a" in st.skip_ids(data, now=dt.date(2026, 9, 14))   # still snoozed
    assert "a" not in st.skip_ids(data, now=dt.date(2026, 9, 21))  # snooze expired


# 5) status_by_job / summary reflect the folded latest state.
def test_status_and_summary():
    data = _data(_ev("a", "saved", "t1"), _ev("a", "applied", "t2"),
                 _ev("b", "dismissed", "t1"))
    assert st.status_by_job(data) == {"a": "applied", "b": "dismissed"}
    assert st.summary(data) == {"applied": 1, "dismissed": 1}


# 6) Snapshot fallback works when there is no event log.
def test_snapshot_fallback():
    data = {"events": [], "snapshot": {
        "statuses": {"a": {"status": "applied"}, "b": {"status": "new"}},
        "dismissed": ["c"]}}
    assert st.skip_ids(data) == {"a", "c"}


# 7) A missing or garbled file yields empty state, never an error.
def test_missing_and_garbled_file():
    assert st.load("/nonexistent/path/xyz.json") == {"events": [], "snapshot": {}}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write("{not json"); p = f.name
    assert st.load(p) == {"events": [], "snapshot": {}}
    assert st.skip_ids(st.load(p)) == set()


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
