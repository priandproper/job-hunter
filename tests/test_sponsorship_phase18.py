#!/usr/bin/env python3
"""Phase 18 — verified sponsorship signal (USCIS H-1B Data Hub lookup).

The network fetch is injected so these run offline (zero-dependency harness).
The one rule under test above all: a wrong/unknown legal name or a network failure
must NEVER be reported as "does not sponsor" (NOT_FOUND is not proof).

Run: `python3 tests/test_sponsorship_phase18.py` (pytest-collectable too).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import sponsorship as sp  # noqa: E402

_HDR = "Employer (Petitioner) Name,Rank,Total Approvals"


def _fake(db):
    """Build a fetch(name, fy) that returns a Data Hub CSV only for exact keys in db.
    db maps EXACT uppercase name -> approvals (mirrors the real exact-match endpoint)."""
    def fetch(name, fy):
        if name in db:
            return True, f'{_HDR}\n{name},1,"{db[name]}"\n'
        return True, f"{_HDR}\n"   # 200 with header only = a real "no rows" answer
    return fetch


# 1) Exact legal name FOUND -> approvals parsed (including quoted thousands).
def test_found_parses_approvals():
    r = sp.lookup_h1b("STRIPE INC", fetch=_fake({"STRIPE INC": "63"}))
    assert r["status"] == "FOUND" and r["approvals"] == 63 and r["matched"] == "STRIPE INC"
    r2 = sp.lookup_h1b("INFOSYS LIMITED", fetch=_fake({"INFOSYS LIMITED": "4,113"}))
    assert r2["approvals"] == 4113   # quoted thousands separator handled


# 2) name_variants tries case/suffix forms so 'Stripe' can reach 'STRIPE INC'.
def test_variants_reach_common_forms():
    vs = sp.name_variants("Stripe")
    assert "STRIPE INC" in vs and "STRIPE" in vs
    # a name already carrying a suffix also gets the comma'd form
    vs2 = sp.name_variants("Ramp Business Corporation")
    assert any("," in v for v in vs2) or "RAMP BUSINESS CORPORATION" in vs2


# 3) A real not-found for a KNOWN (mapped) legal name stays NOT_FOUND — honest.
def test_mapped_miss_is_not_found():
    r = sp.verdict("Stripe", legal_map={"stripe": "STRIPE INC"}, fetch=_fake({}))
    assert r["legal_name_source"] == "map"
    assert r["h1b_status"] == "NOT_FOUND"


# 4) An UNMAPPED brand that misses is UNRESOLVED_NAME, never NOT_FOUND (the forbidden
#    error: we can't imply "no sponsor" when we never confirmed the legal entity).
def test_unmapped_miss_is_unresolved_not_notfound():
    r = sp.verdict("Obscure Startup", legal_map={}, fetch=_fake({}))
    assert r["legal_name_source"] == "guess"
    assert r["h1b_status"] == "UNRESOLVED_NAME"
    assert "NOT proof" in r["summary"] or "not evidence" in r["summary"].lower()


# 5) A network/endpoint failure is SEARCH_ERROR, never NOT_FOUND.
def test_network_failure_is_search_error():
    def boom(name, fy):
        return False, ""
    r = sp.lookup_h1b("STRIPE INC", fetch=boom)
    assert r["status"] == "SEARCH_ERROR"
    # a JSON error blob returned with HTTP 200 is also an error, not "no rows"
    r2 = sp.lookup_h1b("STRIPE INC", fetch=lambda n, f: (True, '{"error":"boom"}'))
    assert r2["status"] == "SEARCH_ERROR"


# 6) verdict merges a browser-supplied E-Verify result; absent -> not_checked.
def test_verdict_merges_everify():
    fetch = _fake({"OPENAI OPCO LLC": "304"})
    lm = {"openai": "OPENAI OPCO LLC"}
    base = sp.verdict("OpenAI", legal_map=lm, fetch=fetch)
    assert base["h1b_status"] == "FOUND" and base["h1b_approvals"] == 304
    assert base["e_verify"] == "not_checked"
    withev = sp.verdict("OpenAI", legal_map=lm, everify={"status": "OPEN", "records": [{}]}, fetch=fetch)
    assert withev["e_verify"] == "OPEN" and "E-Verify: enrolled" in withev["summary"]


# 7) load_legal_names + resolve_legal_name round-trip through the JSON file.
def test_legal_name_map_roundtrip(tmp_path=None):
    import json, tempfile, os
    d = Path(tempfile.mkdtemp())
    (d / "data").mkdir()
    (d / "data" / "legal_names.json").write_text(json.dumps(
        {"legal_names": {"Stripe": "STRIPE INC"}}))
    m = sp.load_legal_names(d)
    assert m.get("stripe") == "STRIPE INC"          # keys lowercased
    assert sp.resolve_legal_name("stripe", m) == ("STRIPE INC", "map")
    assert sp.resolve_legal_name("Nobody", m) == ("Nobody", "guess")


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
