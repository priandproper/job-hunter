"""Phase 4 — the verified career fact bank (private/local, never published).

The single guard behind the whole "never fabricate" principle. Résumé generation
(Phase 5) may draw ONLY from facts here that a human has marked `verified`; every
generated bullet must cite the fact_id(s) it rests on. Facts marked
`needs_clarification` may be shown to the user but never auto-inserted;
`do_not_use` facts are blocked outright. Dates and numbers are immutable — the
system may select, reorder, shorten and accurately reframe a fact, but may never
change a figure, upgrade ownership/causality, or turn an output into a downstream
result (e.g. an impression count into revenue).

This module is the data layer: the schema, load/save, validation, status gating,
immutability checks, and a reconciliation queue that flags contradictions (two
sources giving different dates or different figures for the same metric). It lives
in data/facts.local.json — git-ignored, NEVER written under docs/.
"""

import json
import re
from pathlib import Path

STATUSES = ("verified", "needs_clarification", "do_not_use")

# The fact schema (per the brief). blank_fact() fills every field with a typed default.
_FIELDS = {
    "fact_id": "", "company": "", "role": "", "start_date": "", "end_date": "",
    "product": "", "action": "", "audience": list, "scale": "",
    "metric_value": "", "metric_name": "", "baseline": "", "comparison_period": "",
    "outcome": "", "tools": list, "skills": list, "role_tags": list,
    "ownership_level": "", "allowed_claims": list, "prohibited_claims": list,
    "evidence_notes": "", "verification_status": "needs_clarification",
}

# Fields that record what actually happened and must NEVER be altered once verified.
IMMUTABLE_FIELDS = ("start_date", "end_date", "metric_value", "metric_name",
                    "baseline", "comparison_period")

_NUM_RE = re.compile(r"\$?\s?\d[\d,]*\.?\d*\s?(?:%|k|m|bn|b|x|\+)?", re.I)


def blank_fact(**kw) -> dict:
    f = {}
    for name, default in _FIELDS.items():
        f[name] = default() if callable(default) else default
    f.update({k: v for k, v in kw.items() if k in _FIELDS})
    return f


def validate_fact(fact: dict) -> list[str]:
    """Return a list of problems with a fact (empty = valid)."""
    errs = []
    if not fact.get("fact_id"):
        errs.append("missing fact_id")
    if not fact.get("company"):
        errs.append("missing company")
    if not (fact.get("role") or fact.get("action")):
        errs.append("fact needs at least a role or an action")
    st = fact.get("verification_status")
    if st not in STATUSES:
        errs.append(f"verification_status must be one of {STATUSES}, got {st!r}")
    for lf in ("audience", "tools", "skills", "role_tags", "allowed_claims", "prohibited_claims"):
        if lf in fact and not isinstance(fact[lf], list):
            errs.append(f"{lf} must be a list")
    return errs


# --- status gating (what résumé generation is allowed to touch) ----------------
def usable(facts: list[dict]) -> list[dict]:
    """ONLY verified facts may be used to generate résumé content."""
    return [f for f in facts if f.get("verification_status") == "verified"]


def needs_clarification(facts: list[dict]) -> list[dict]:
    """Shown to the user for review, but never auto-inserted into a résumé."""
    return [f for f in facts if f.get("verification_status") == "needs_clarification"]


def blocked(facts: list[dict]) -> list[dict]:
    return [f for f in facts if f.get("verification_status") == "do_not_use"]


def verified_ids(facts: list[dict]) -> set[str]:
    return {f["fact_id"] for f in usable(facts) if f.get("fact_id")}


def citations_ok(fact_ids, facts: list[dict]) -> tuple[bool, list[str]]:
    """A generated bullet must cite ≥1 fact_id, and every cited id must be a
    VERIFIED fact. Returns (ok, problems)."""
    ids = list(fact_ids or [])
    if not ids:
        return False, ["bullet cites no fact_id"]
    ok_ids = verified_ids(facts)
    bad = [i for i in ids if i not in ok_ids]
    return (not bad), ([f"cited fact_id not verified/known: {i}" for i in bad])


# --- immutability ---------------------------------------------------------------
def numbers_in(text: str | None) -> set[str]:
    """Normalized numeric tokens in a piece of text ('30%', '$1.2M', '40+')."""
    return {re.sub(r"\s+", "", m.group(0)).lower() for m in _NUM_RE.finditer(text or "")}


def fact_numbers(fact: dict) -> set[str]:
    """Every immutable number a fact vouches for — used to check that a generated
    bullet introduces no figure the fact doesn't support."""
    nums = set()
    for field in ("metric_value", "baseline", "action", "outcome", "scale"):
        nums |= numbers_in(str(fact.get(field, "")))
    return nums


def changed_immutables(old: dict, new: dict) -> list[str]:
    """Which immutable fields differ between two versions of the same fact."""
    out = []
    for f in IMMUTABLE_FIELDS:
        if str(old.get(f, "")).strip() != str(new.get(f, "")).strip():
            out.append(f)
    # a verified fact's action text must not gain/lose numbers
    if old.get("verification_status") == "verified" and \
            numbers_in(old.get("action")) != numbers_in(new.get("action")):
        out.append("action(numbers)")
    return out


# --- reconciliation queue -------------------------------------------------------
def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def find_contradictions(facts: list[dict]) -> list[dict]:
    """Flag facts that disagree with each other — the reconciliation queue.
    Two kinds:
      • dates:  same (company, role) but different start/end dates.
      • metric: same (company, metric_name) but different metric_value.
    `do_not_use` facts are ignored."""
    live = [f for f in facts if f.get("verification_status") != "do_not_use"]
    out = []

    by_role = {}
    for f in live:
        by_role.setdefault((_norm(f.get("company")), _norm(f.get("role"))), []).append(f)
    for (co, role), group in by_role.items():
        if not co or not role:
            continue
        dates = {(f.get("start_date", ""), f.get("end_date", "")) for f in group}
        if len(dates) > 1:
            out.append({"kind": "dates", "company": co, "role": role,
                        "fact_ids": [f["fact_id"] for f in group],
                        "detail": f"conflicting dates: {sorted(dates)}"})

    by_metric = {}
    for f in live:
        mn = _norm(f.get("metric_name"))
        if mn and f.get("metric_value"):
            by_metric.setdefault((_norm(f.get("company")), mn), []).append(f)
    for (co, mn), group in by_metric.items():
        vals = {_norm(str(f.get("metric_value"))) for f in group}
        if len(vals) > 1:
            out.append({"kind": "metric", "company": co, "metric_name": mn,
                        "fact_ids": [f["fact_id"] for f in group],
                        "detail": f"conflicting values for '{mn}': {sorted(vals)}"})
    return out


# --- load / save ----------------------------------------------------------------
def load(path: Path) -> list[dict]:
    try:
        return json.loads(Path(path).read_text()).get("facts", [])
    except (OSError, json.JSONDecodeError):
        return []


def save(path: Path, facts: list[dict], source: str = "") -> None:
    """Persist the fact bank. Refuses to write anywhere under a docs/ directory —
    the fact bank is private and must never be published."""
    p = Path(path)
    if "docs" in p.parts:
        raise ValueError("fact bank must never be written under docs/ (private data)")
    from datetime import datetime, timezone
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source, "count": len(facts), "facts": facts,
    }, indent=2))
