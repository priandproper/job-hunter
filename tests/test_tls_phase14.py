#!/usr/bin/env python3
"""Phase 14 tests — TLS is always verified; it can't be silently disabled.

Guards: the shared context requires certificates and checks hostnames; the source
contains no unverified-context escape hatch; a cert failure is raised (never
downgraded); transient errors retry then raise. Zero-dependency (pytest-collectable).
"""

import ssl
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import ats  # noqa: E402


# 1) The shared TLS context verifies (CERT_REQUIRED + hostname check).
def test_context_is_verifying():
    ctx = ats._verified_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert ats._CTX.verify_mode == ssl.CERT_REQUIRED and ats._CTX.check_hostname is True


# 2) The module has NO unverified-context escape hatch (guards accidental reintroduction).
def test_no_unverified_fallback_in_source():
    src = (Path(ats.__file__)).read_text()
    assert "_create_unverified_context" not in src
    assert "CERT_NONE" not in src


# 3) A certificate failure is raised, NOT downgraded, and NOT retried.
def test_cert_failure_raises_not_downgraded(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None, context=None):
        calls["n"] += 1
        raise urllib.error.URLError(ssl.SSLError("CERTIFICATE_VERIFY_FAILED"))

    monkeypatch.setattr(ats.urllib.request, "urlopen", fake_urlopen)
    try:
        ats._open("req", 5)
        assert False, "expected the cert failure to propagate"
    except urllib.error.URLError:
        pass
    assert calls["n"] == 1, "cert failure must not be retried or downgraded"


# 4) A transient (non-cert) error is retried with backoff, then raised.
def test_transient_error_retries_then_raises(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None, context=None):
        calls["n"] += 1
        raise urllib.error.URLError("temporary failure in name resolution")

    monkeypatch.setattr(ats.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(ats.time, "sleep", lambda *_: None)   # no real backoff wait
    try:
        ats._open("req", 5, retries=2)
        assert False, "expected the transient error to raise after retries"
    except urllib.error.URLError:
        pass
    assert calls["n"] == 3, "should try initial + 2 retries"


# 5) A successful open returns the response and uses the VERIFIED context.
def test_success_uses_verified_context(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None, context=None):
        seen["ctx"] = context
        return "RESP"

    monkeypatch.setattr(ats.urllib.request, "urlopen", fake_urlopen)
    assert ats._open("req", 5) == "RESP"
    assert seen["ctx"] is ats._CTX
    assert seen["ctx"].verify_mode == ssl.CERT_REQUIRED


# --- tiny monkeypatch shim so this runs without pytest ------------------------
class _MP:
    def __init__(self): self._undo = []
    def setattr(self, obj, name, val):
        self._undo.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)
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
