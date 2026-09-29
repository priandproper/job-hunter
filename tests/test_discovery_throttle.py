#!/usr/bin/env python3
"""Phase 18 — JSearch throttle keeps the free RapidAPI quota safe.

The daily worker must not call JSearch every run; it fires at most every
min_interval_hours. Both JSearch calls in a run read the same pre-run timestamp
(run together or not at all), and the worker stamps once after they finish.

Run: `python3 tests/test_discovery_throttle.py` (pytest-collectable too).
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import discovery as d  # noqa: E402


def _cfg(hours):
    return {"discovery": {"min_interval_hours": hours, "enabled": True}}


# 1) Never-run -> due; interval 0 -> always due (throttle disabled).
def test_due_when_never_run_or_disabled():
    root = Path(tempfile.mkdtemp())
    assert d.jsearch_due(_cfg(72), root) is True          # no state file yet
    assert d.jsearch_due(_cfg(0), root) is True           # throttle off


# 2) Right after a run -> not due; after the interger elapses -> due again.
def test_not_due_right_after_run_then_due_after_interval():
    root = Path(tempfile.mkdtemp())
    (root / "data").mkdir()
    d.mark_jsearch_run(root)
    assert d.jsearch_due(_cfg(72), root) is False         # just ran
    # Backdate the stamp to 73h ago -> due again.
    p = d._jsearch_state_path(root)
    import json
    p.write_text(json.dumps({"last_run": time.time() - 73 * 3600}))
    assert d.jsearch_due(_cfg(72), root) is True


# 3) A throttled run is a clean no-op: fetch_postings returns [] and does NOT
#    reset the clock (only the worker's post-run mark does).
def test_throttled_fetch_is_noop_and_preserves_clock():
    root = Path(tempfile.mkdtemp())
    (root / "data").mkdir()
    d.mark_jsearch_run(root)
    import json
    stamp_before = json.loads(d._jsearch_state_path(root).read_text())["last_run"]
    cfg = {"discovery": {"enabled": True, "min_interval_hours": 72,
                         "jsearch_api_key_env": "X", "secrets_file": "",
                         "queries": ["q"], "results_pages": 1}}
    # A key is 'present' via env so we reach the due-check, which should block it.
    import os
    os.environ["X"] = "fake-key"
    try:
        out = d.fetch_postings(cfg, root, log=lambda *_: None)
    finally:
        del os.environ["X"]
    assert out == []                                       # throttled -> no postings
    stamp_after = json.loads(d._jsearch_state_path(root).read_text())["last_run"]
    assert stamp_after == stamp_before                     # clock untouched by a call


# 4) has_jsearch_key reflects the env/secrets state.
def test_has_key_reflects_env():
    import os
    root = Path(tempfile.mkdtemp())
    cfg = {"discovery": {"jsearch_api_key_env": "JS_TEST_KEY", "secrets_file": ""}}
    assert d.has_jsearch_key(cfg, root) is False
    os.environ["JS_TEST_KEY"] = "k"
    try:
        assert d.has_jsearch_key(cfg, root) is True
    finally:
        del os.environ["JS_TEST_KEY"]


# 5) Per-run request budget is a hard cap shared across JSearch calls.
def test_request_budget_hard_caps_spend():
    d.reset_request_budget(20)
    assert d.jsearch_requests_used() == 0
    assert d._budget_room(20) is True and d._budget_room(21) is False
    d._budget_spend(18)
    assert d._budget_room(2) is True and d._budget_room(3) is False   # 18+3 > 20
    d._budget_spend(2)
    assert d._budget_room(1) is False                                  # full at 20
    # cap None (or 0) disables the budget -> always room
    d.reset_request_budget(None)
    assert d._budget_room(9999) is True


# 6) With the budget exhausted, fetch_postings makes ZERO network calls (guard fires
#    before _jsearch), even with a key present.
def test_fetch_postings_stops_at_budget():
    import os
    root = Path(tempfile.mkdtemp())
    (root / "data").mkdir()
    cfg = {"discovery": {"enabled": True, "min_interval_hours": 0,   # not throttled
                         "jsearch_api_key_env": "BUD_KEY", "secrets_file": "",
                         "queries": ["a", "b", "c"], "results_pages": 1}}
    os.environ["BUD_KEY"] = "fake"
    called = {"n": 0}
    orig = d._jsearch
    d._jsearch = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or []
    try:
        d.reset_request_budget(0)                 # no budget at all
        out = d.fetch_postings(cfg, root, log=lambda *_: None)
    finally:
        d._jsearch = orig
        del os.environ["BUD_KEY"]
    assert out == [] and called["n"] == 0          # guard fired, no network attempted


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
