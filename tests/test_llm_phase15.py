#!/usr/bin/env python3
"""Phase 15 tests — robust, optional Claude.

Covers the provider abstraction (availability, envelope parsing, timeout/exit/empty
errors, PII-safe error messages) and coach_rank's schema validation + deterministic
fallback. Zero-dependency (pytest-collectable too).
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lib import llm  # noqa: E402
from lib import ranking  # noqa: E402

_spec = importlib.util.spec_from_file_location("coach_rank", ROOT / "scripts" / "coach_rank.py")
cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr)


class _Proc:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _job(jid, band="B", risk="yellow", posted="2026-09-13"):
    return {"id": jid, "title": "Product Marketing Manager", "company": "Acme",
            "posted_at": posted, "immigration": {"risk": risk},
            "priority": {"total": 70, "band": band}}


# 1) availability + unavailable raises the right type.
def test_availability_and_unavailable():
    assert llm.available("python3") is True
    try:
        llm.run_json("x", cmd="no-such-cli-zzz")
        assert False
    except llm.LLMUnavailable:
        pass


# 2) Envelope is parsed and JSON extracted (fake CLI via monkeypatched subprocess).
def test_envelope_parse(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda cmd=llm.DEFAULT_CMD: True)
    monkeypatch.setattr(llm.subprocess, "run",
                        lambda *a, **k: _Proc(out='{"result":"```json\\n{\\"ok\\":1}\\n```"}'))
    assert llm.run_json("p") == {"ok": 1}


# 3) Non-zero exit, empty output, and no-JSON all raise LLMError.
def test_error_paths(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda cmd=llm.DEFAULT_CMD: True)
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: _Proc(rc=1, err="boom"))
    _raises(lambda: llm.run_json("p"), llm.LLMError)
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: _Proc(out=""))
    _raises(lambda: llm.run_json("p"), llm.LLMError)
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: _Proc(out="no json here"))
    _raises(lambda: llm.run_json("p"), llm.LLMError)


# 4) Timeout raises LLMError, not a crash.
def test_timeout(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda cmd=llm.DEFAULT_CMD: True)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
    monkeypatch.setattr(llm.subprocess, "run", boom)
    _raises(lambda: llm.run_json("p", timeout=1), llm.LLMError)


# 5) Error messages never contain the prompt (PII-safe).
def test_pii_safe_errors(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda cmd=llm.DEFAULT_CMD: True)
    monkeypatch.setattr(llm.subprocess, "run", lambda *a, **k: _Proc(rc=2, err="stderr line"))
    secret = "RESUME_PII_MUST_NOT_LEAK_42"
    try:
        llm.run_json(secret)
        assert False
    except llm.LLMError as e:
        assert secret not in str(e)


# 6) validate_report drops hallucinated ids and rows missing required fields.
def test_validate_report():
    ids = {"a", "b"}
    rep = {"briefing": {"headline": "h", "focus": "f", "top_ids": ["a", "HALLUC"]},
           "ranked": [{"id": "a", "tier": "top", "priority": 90, "why": "real", "flag": ""},
                      {"id": "HALLUC", "tier": "top", "priority": 99, "why": "fake", "flag": ""},
                      {"id": "b", "tier": "top", "priority": 50}],  # no 'why' -> dropped
           "flagged": [{"id": "a", "reason": "x"}, {"id": "ZZZ", "reason": "y"}]}
    v = cr.validate_report(rep, ids)
    assert [r["id"] for r in v["ranked"]] == ["a"]
    assert v["briefing"]["top_ids"] == ["a"]
    assert [f["id"] for f in v["flagged"]] == ["a"]


# 7) validate_report raises when there is nothing usable (so caller can fall back).
def test_validate_report_raises_when_empty():
    _raises(lambda: cr.validate_report({"ranked": [{"id": "ZZZ", "why": "x"}]}, {"a"}), ValueError)
    _raises(lambda: cr.validate_report("not a dict", {"a"}), ValueError)


# 8) Deterministic fallback produces a valid, id-clean ranking from the active queue.
def test_deterministic_fallback():
    jobs = [_job("a", "A"), _job("b", "B"), _job("c", "C")]
    ordered = ranking.order_active_queue(jobs, {})
    rep = cr.deterministic_report(ordered, jobs)
    ids = {j["id"] for j in jobs}
    assert rep["ranked"] and all(r["id"] in ids for r in rep["ranked"])
    assert all(r["tier"] in ("top", "strong", "maybe") and r["why"] for r in rep["ranked"])
    assert rep["briefing"]["top_ids"] and "Claude unavailable" in rep["briefing"]["headline"]
    # a yellow-immigration role is flagged as a sponsorship risk to verify
    assert any(r["flag"] == "sponsorship-risk" for r in rep["ranked"])


def _raises(fn, exc):
    try:
        fn(); raise AssertionError(f"expected {exc.__name__}")
    except exc:
        pass


# --- monkeypatch shim so this runs without pytest ----------------------------
class _MP:
    def __init__(self): self._undo = []
    def setattr(self, obj, name, val):
        self._undo.append((obj, name, getattr(obj, name))); setattr(obj, name, val)
    def undo(self):
        for obj, name, old in reversed(self._undo):
            setattr(obj, name, old)


def _run():
    import inspect
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    failed = 0
    for name, fn in tests:
        mp = _MP()
        try:
            fn(mp) if "monkeypatch" in inspect.signature(fn).parameters else fn()
            print(f"  ok   {name}")
        except AssertionError as e:
            failed += 1; print(f"  FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"  ERROR {name}: {type(e).__name__}: {e}")
        finally:
            mp.undo()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run())
