#!/usr/bin/env python3
"""Age-filter tests — max_age_days auto-tidy and the drop_undated_jobs option.

Default: keep undated/unparseable postings (don't guess). With drop_undated=True
(candidate asked to see only jobs with a confirmed recent post date), drop them too.

Run: `python3 tests/test_age_filter.py` (pytest-collectable too).
"""

import datetime as _dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import worker  # noqa: E402

TODAY = _dt.date(2026, 9, 29)


def _job(posted):
    return {"posted_at": posted}


def _iso(days_ago):
    return (TODAY - _dt.timedelta(days=days_ago)).isoformat()


# 1) Fresh vs stale on the dated path (unchanged behavior).
def test_dated_within_and_beyond_window():
    assert worker._too_old(_job(_iso(3)), 14, TODAY) is False    # 3d old -> keep
    assert worker._too_old(_job(_iso(20)), 14, TODAY) is True     # 20d old -> drop


# 2) Undated/unparseable: kept by default, dropped when drop_undated=True.
def test_undated_default_keep_then_drop():
    for raw in ("", None, "just now", "20 hours ago"):
        assert worker._too_old(_job(raw), 14, TODAY) is False               # default keeps
        assert worker._too_old(_job(raw), 14, TODAY, drop_undated=True) is True  # opt-in drops


# 3) drop_undated never resurrects the disabled filter, and never drops a fresh dated job.
def test_drop_undated_respects_window_and_disable():
    assert worker._too_old(_job(""), 0, TODAY, drop_undated=True) is False   # max_age<=0 disables entirely
    assert worker._too_old(_job(_iso(2)), 14, TODAY, drop_undated=True) is False  # fresh dated stays


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
