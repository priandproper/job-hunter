#!/usr/bin/env python3
"""Phase 16 tests — public-payload privacy guard + log redaction + .gitignore coverage.

Verifies (as the brief requires) that public payload generation excludes restricted
fields, that a real worker-shaped public job is clean, that leaks are caught, that
secrets/PII are redacted from log text, and that generated private artifacts are
git-ignored. Zero-dependency (pytest-collectable too).
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lib import privacy as pv  # noqa: E402


def _public_job():
    """A public job dict shaped like worker's output (email/phone stripped, JD present)."""
    return {
        "id": "j1", "company": "Acme", "title": "Product Marketing Manager",
        "location": "Boston, MA", "url": "https://acme.com/jobs/1",
        "excerpt": "Own GTM. Accommodation requests: accommodation@acme.com.",  # employer email in JD is OK
        "resume_core": {"label": "PMM", "contact": {
            "fullName": "Priyanka Tambe", "location": "Boston, MA",
            "linkedin": "linkedin.com/in/x", "github": "github.com/x",
            "email": "", "phone": ""}, "summary": "B2B product marketer.",
            "experience": [{"company": "Prev", "highlights": ["Did things."]}]},
        "immigration": {"risk": "yellow"}, "priority": {"band": "A"},
    }


# 1) A correctly-built public job (email/phone empty) is safe.
def test_clean_public_job_passes():
    assert pv.scan_public(_public_job()) == []
    assert pv.assert_public_safe(_public_job()) is True


# 2) Employer email inside JD text is allowed (job-market info, not candidate PII).
def test_jd_employer_email_allowed():
    j = _public_job()
    j["excerpt"] += " Contact security@acme.com to report issues."
    assert pv.scan_public(j) == []


# 3) A candidate email leaking into resume_core.contact.email is caught.
def test_leaked_candidate_email_caught():
    j = _public_job()
    j["resume_core"]["contact"]["email"] = "priyankatambe910@gmail.com"
    v = pv.scan_public(j)
    assert v and any("email" in x for x in v)
    _raises(lambda: pv.assert_public_safe(j), ValueError)


# 4) A leaked phone is caught.
def test_leaked_phone_caught():
    j = _public_job()
    j["resume_core"]["contact"]["phone"] = "617-555-1234"
    assert any("phone" in x for x in pv.scan_public(j))


# 5) Third-party contact data (referrers/connections) leaking in is caught.
def test_forbidden_keys_caught():
    j = _public_job()
    j["referrers"] = [{"name": "Jane Doe"}]
    v = pv.scan_public(j)
    assert any("referrers" in x for x in v)
    # an empty forbidden key is not a violation
    j2 = _public_job(); j2["referrers"] = []
    assert not any("referrers" in x for x in pv.scan_public(j2))


# 6) Secret tokens anywhere are caught.
def test_secret_token_caught():
    j = _public_job()
    j["meta_note"] = "debug key sk-ant-abcdefghijklmnopqrstuvwx"
    assert any("secret" in x for x in pv.scan_public(j))


# 7) redact() masks emails, phones, and secret tokens in log text.
def test_redact():
    s = pv.redact("mail me at a.b@x.com or 617-555-1234 with key ghp_" + "a" * 30)
    assert "a.b@x.com" not in s and "617-555-1234" not in s and "ghp_" not in s
    assert "[REDACTED-EMAIL]" in s and "[REDACTED-PHONE]" in s and "[REDACTED-SECRET]" in s


# 8) Generated private artifacts are git-ignored.
def test_private_artifacts_gitignored():
    checks = ["data/state.local.json", "data/facts.local.json", "data/funnel-report.md",
              "data/packets/x.md", "data/people.local.json", ".secrets.json",
              "data/private.local.json", "data/coach_history.json"]
    r = subprocess.run(["git", "check-ignore", *checks], cwd=ROOT,
                       capture_output=True, text=True)
    ignored = set(r.stdout.split())
    missing = [c for c in checks if c not in ignored]
    assert not missing, f"not git-ignored: {missing}"


def _raises(fn, exc):
    try:
        fn(); raise AssertionError(f"expected {exc.__name__}")
    except exc:
        pass


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
