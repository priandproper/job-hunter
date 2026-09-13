#!/usr/bin/env python3
"""Phase 4 tests — the verified career fact bank.

Covers the schema/validation, status gating (only `verified` is usable;
`needs_clarification` shown but not auto-used; `do_not_use` blocked), bullet-citation
enforcement, immutability of dates/numbers, the reconciliation queue, and the
guarantee that the fact bank is never written under docs/.

Zero-dependency: `python3 tests/test_factbank_phase4.py` (pytest-collectable too).
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib import factbank as fb  # noqa: E402


def _fact(fid, status="verified", **kw):
    return fb.blank_fact(fact_id=fid, company=kw.pop("company", "Acme"),
                         role=kw.pop("role", "Analyst"),
                         verification_status=status, **kw)


# 1) blank_fact has every schema field with typed defaults; validation passes.
def test_blank_fact_schema_and_validation():
    f = fb.blank_fact(fact_id="x1", company="Acme", role="PMM")
    for field in ("fact_id", "company", "role", "metric_value", "allowed_claims",
                  "prohibited_claims", "ownership_level", "verification_status"):
        assert field in f
    assert isinstance(f["allowed_claims"], list)
    assert fb.validate_fact(f) == []


# 2) Bad status / missing fields are caught.
def test_validation_catches_problems():
    assert any("fact_id" in e for e in fb.validate_fact(fb.blank_fact(company="A", role="R")))
    bad = fb.blank_fact(fact_id="x", company="A", role="R", verification_status="approved")
    assert any("verification_status" in e for e in fb.validate_fact(bad))


# 3) Only verified facts are usable; needs_clarification & do_not_use are not.
def test_status_gating():
    facts = [_fact("a", "verified"), _fact("b", "needs_clarification"),
             _fact("c", "do_not_use")]
    assert {f["fact_id"] for f in fb.usable(facts)} == {"a"}
    assert {f["fact_id"] for f in fb.needs_clarification(facts)} == {"b"}
    assert {f["fact_id"] for f in fb.blocked(facts)} == {"c"}
    assert fb.verified_ids(facts) == {"a"}


# 4) A bullet must cite >=1 fact_id, and every cited id must be verified.
def test_citation_enforcement():
    facts = [_fact("a", "verified"), _fact("b", "needs_clarification")]
    assert fb.citations_ok(["a"], facts)[0] is True
    assert fb.citations_ok([], facts)[0] is False            # no citation
    assert fb.citations_ok(["b"], facts)[0] is False         # cites a non-verified fact
    assert fb.citations_ok(["a", "zzz"], facts)[0] is False  # cites an unknown fact


# 5) Numbers are extracted for immutability checks.
def test_numbers_extracted():
    f = _fact("a", action="Lifted conversion 30% on a $1.2M pipeline across 40+ accounts")
    nums = fb.fact_numbers(f)
    assert "30%" in nums and "40+" in nums
    assert any("1.2m" in n for n in nums)


# 6) Immutability: changing a date or a metric value on a verified fact is flagged.
def test_immutable_fields_flagged():
    old = _fact("a", "verified", start_date="Jun 2024", metric_value="30%")
    new = dict(old, metric_value="45%")                       # tampered figure
    assert "metric_value" in fb.changed_immutables(old, new)
    new2 = dict(old, start_date="Jan 2024")                   # tampered date
    assert "start_date" in fb.changed_immutables(old, new2)
    # editing a non-immutable field (skills) is fine
    assert fb.changed_immutables(old, dict(old, skills=["sql"])) == []


# 7) Altering the numbers inside a verified fact's action text is flagged.
def test_action_number_tampering_flagged():
    old = _fact("a", "verified", action="Lifted conversion 30%")
    new = dict(old, action="Lifted conversion 60%")
    assert "action(numbers)" in fb.changed_immutables(old, new)


# 8) Reconciliation queue flags conflicting dates and conflicting metric values.
def test_reconciliation_finds_contradictions():
    facts = [
        _fact("a", company="Acme", role="Analyst", start_date="Jun 2023", end_date="Aug 2024"),
        _fact("b", company="Acme", role="Analyst", start_date="Jun 2023", end_date="Dec 2024"),
        _fact("c", company="Acme", metric_name="conversion lift", metric_value="30%"),
        _fact("d", company="Acme", metric_name="conversion lift", metric_value="45%"),
    ]
    kinds = {c["kind"] for c in fb.find_contradictions(facts)}
    assert "dates" in kinds and "metric" in kinds


# 9) do_not_use facts are excluded from reconciliation.
def test_reconciliation_ignores_do_not_use():
    facts = [
        _fact("a", "verified", company="Acme", role="Analyst", start_date="Jun 2023"),
        _fact("b", "do_not_use", company="Acme", role="Analyst", start_date="Jan 2020"),
    ]
    assert fb.find_contradictions(facts) == []


# 10) The fact bank refuses to be written under docs/ (privacy guarantee).
def test_refuses_to_write_under_docs():
    with tempfile.TemporaryDirectory() as d:
        docs = Path(d) / "docs" / "facts.json"
        try:
            fb.save(docs, [_fact("a")])
            assert False, "expected save() to refuse a docs/ path"
        except ValueError:
            pass
        ok = Path(d) / "data" / "facts.local.json"
        fb.save(ok, [_fact("a")])
        assert ok.exists() and fb.load(ok)


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
