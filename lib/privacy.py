"""Phase 16 — privacy guards for the PUBLIC payload and logs.

The dashboard's data files under docs/ are published (GitHub Pages), so they must carry
only job-market information plus the candidate's chosen PUBLIC contact fields
(name / city / LinkedIn / GitHub). This module provides the programmatic checks the
brief requires: scan the public payload for restricted PII before it ships, and redact
secrets/PII from anything written to a log.

Deterministic, stdlib-only, testable. It does NOT touch git history or user data.
"""

import re

# Contact sub-fields the candidate has chosen to make public (contact_public). Anything
# else under a contact block — notably email/phone — must never reach a public file.
PUBLIC_CONTACT_FIELDS = frozenset({"fullName", "headline", "location", "linkedin", "github", "website"})
RESTRICTED_CONTACT_FIELDS = frozenset({"email", "phone"})

# Keys whose presence in a public payload means third-party / private data leaked in.
FORBIDDEN_KEYS = frozenset({"referrers", "connections", "contact_pii", "private",
                            "email", "phone", "apollo_people", "people"})

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)")
_SECRET = re.compile(r"sk-ant-[A-Za-z0-9_\-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|"
                     r"AKIA[0-9A-Z]{16}|BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY", re.I)

# Employer contact addresses legitimately appear inside JD text (accommodation@…,
# security@…). Those are job-market info, not the candidate's PII, so JD/excerpt fields
# are exempt from the e-mail check.
_JD_FIELDS = frozenset({"excerpt", "full_cleaned_jd", "jd", "description"})


def redact(text) -> str:
    """Mask emails, phone numbers and secret tokens for safe logging."""
    s = str(text or "")
    s = _SECRET.sub("[REDACTED-SECRET]", s)
    s = _EMAIL.sub("[REDACTED-EMAIL]", s)
    s = _PHONE.sub("[REDACTED-PHONE]", s)
    return s


def _walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def _leaf_key(path):
    return re.split(r"[.\[]", path)[-1].rstrip("]")


def _forbidden_keys(obj, path=""):
    """Yield (path, key) for any FORBIDDEN_KEY that holds a non-empty value, at any
    dict level (checks containers, not just leaves)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else str(k)
            if k in FORBIDDEN_KEYS and v not in (None, "", [], {}):
                yield p, k
            yield from _forbidden_keys(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _forbidden_keys(v, f"{path}[{i}]")


def scan_public(payload) -> list:
    """Return a list of restricted-PII violations found in a payload intended to be
    PUBLIC. Empty list == safe to publish. Checks:
      • forbidden keys (referrers/connections/contact_pii/email/phone/…) with a value
      • a contact block carrying a non-empty email/phone
      • secret tokens anywhere
    """
    violations = [f"forbidden key '{k}' with a value at {p}" for p, k in _forbidden_keys(payload)]
    for path, val in _walk(payload):
        if not isinstance(val, str) or not val.strip():
            continue
        key = _leaf_key(path)
        if _SECRET.search(val):
            violations.append(f"secret token at {path}")
        # skip JD text for the email check (employer addresses are job-market info)
        if key not in _JD_FIELDS and key in RESTRICTED_CONTACT_FIELDS and \
                (_EMAIL.search(val) or _PHONE.search(val)):
            violations.append(f"restricted contact field '{key}' populated at {path}")
    return violations


def assert_public_safe(payload):
    """Raise ValueError if the payload isn't safe to publish (for use as a hard gate)."""
    v = scan_public(payload)
    if v:
        raise ValueError("public payload contains restricted PII: " + "; ".join(v[:5]))
    return True
