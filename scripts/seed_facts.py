#!/usr/bin/env python3
"""Seed / refresh the verified career fact bank from the resume profile.

Reads data/profile.json (the candidate's canonical experience) and writes a DRAFT
fact bank to data/facts.local.json — one fact per résumé highlight. Every seeded
fact is marked `needs_clarification`: NOTHING is auto-verified. You review the file
(or a future dashboard UI), flip the ones you can stand behind to `verified`, mark
anything you can't to `do_not_use`, and fill allowed/prohibited claims. Only
`verified` facts may ever be used to generate a résumé (see lib/factbank).

Re-running is safe: existing facts (and your edits — verification_status, claims,
metric_name, …) are preserved by fact_id; only genuinely new highlights are added.

  python3 scripts/seed_facts.py             # merge new highlights into facts.local.json
  python3 scripts/seed_facts.py --dry-run   # show what would change, write nothing
  python3 scripts/seed_facts.py --reconcile # just print the contradiction queue
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from lib import factbank  # noqa: E402

PROFILE = ROOT / "data" / "profile.json"
OUT = ROOT / "data" / "facts.local.json"   # git-ignored, private, never under docs/

_TOOLS = ["sql", "tableau", "power bi", "looker", "ga4", "excel", "salesforce",
          "hubspot", "eloqua", "marketo", "jira", "asana", "figma", "python",
          "google analytics", "amplitude", "mixpanel", "segment"]
_SKILLS = ["product marketing", "go-to-market", "gtm", "positioning", "messaging",
           "segmentation", "demand generation", "growth marketing", "lifecycle",
           "campaign", "funnel", "conversion", "retention", "a/b testing",
           "experimentation", "attribution", "market research", "sales enablement",
           "competitive analysis", "abm", "cohort analysis", "forecasting",
           "marketing operations", "revenue operations"]
_OWN_LED = re.compile(r"^\s*(led|owned|drove|built|launched|designed|created|"
                      r"developed|managed|founded|architected)\b", re.I)
_OWN_CONTRIB = re.compile(r"^\s*(contributed|supported|assisted|helped|"
                          r"partnered|collaborated|participated)\b", re.I)
_METRIC = re.compile(r"\$?\s?\d[\d,]*\.?\d*\s?(?:%|k|m|bn|b|x|\+)?", re.I)


def _fact_id(company: str, role: str, highlight: str) -> str:
    return hashlib.sha1(f"{company}|{role}|{highlight}".encode()).hexdigest()[:16]


def _ownership(highlight: str) -> str:
    if _OWN_LED.match(highlight):
        return "led/owned"
    if _OWN_CONTRIB.match(highlight):
        return "contributed"
    return "individual_contributor"


def _present(vocab, text: str) -> list[str]:
    tl = text.lower()
    return [v for v in vocab if v in tl]


def _present_wb(vocab, text: str) -> list[str]:
    """Word-boundary match (for tool names) so 'Segment' doesn't fire on
    'segmentation' and 'excel' doesn't fire inside a longer word."""
    tl = text.lower()
    return [v for v in vocab if re.search(rf"\b{re.escape(v)}\b", tl)]


def _first_metric(highlight: str) -> str:
    """First %/$ figure, preserved VERBATIM (immutable). '' if none — never invented."""
    for m in _METRIC.finditer(highlight):
        tok = m.group(0).strip()
        if "%" in tok or "$" in tok or re.search(r"\d\s?(?:k|m|bn|b|x|\+)\b", tok, re.I):
            return tok
    return ""


def _seed_from_profile(profile: dict) -> list[dict]:
    seen, facts = set(), []
    for exp in profile.get("experiences", []) or []:
        company = exp.get("company", "")
        titles = [t for t in (exp.get("titles") or []) if t] or [exp.get("title", "")]
        role = titles[0] if titles else ""
        # bullets is the canonical list; some profiles use 'highlights'.
        bullets = exp.get("bullets") or exp.get("highlights") or []
        alt = ("; ".join(titles[1:]) if len(titles) > 1 else "")
        for h in bullets:
            h = (h or "").strip()
            if not h:
                continue
            fid = _fact_id(company, role, h)
            if fid in seen:
                continue
            seen.add(fid)
            facts.append(factbank.blank_fact(
                fact_id=fid, company=company, role=role,
                start_date=exp.get("startDate", ""), end_date=exp.get("endDate", ""),
                action=h,                              # verbatim, immutable
                metric_value=_first_metric(h),
                tools=_present_wb(_TOOLS, h), skills=_present(_SKILLS, h),
                ownership_level=_ownership(h),
                evidence_notes=("seeded from data/profile.json (experiences)"
                                + (f"; alt titles: {alt}" if alt else "")),
                verification_status="needs_clarification",   # never auto-verified
            ))
    return facts


def _merge(existing: list[dict], seeded: list[dict]) -> tuple[list[dict], int, int]:
    """Keep every existing fact untouched (preserve human edits); add only new ids."""
    by_id = {f.get("fact_id"): f for f in existing}
    added = 0
    for f in seeded:
        if f["fact_id"] not in by_id:
            by_id[f["fact_id"]] = f
            added += 1
    return list(by_id.values()), added, len(existing)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--reconcile", action="store_true", help="only print contradictions")
    args = ap.parse_args()

    existing = factbank.load(OUT)

    if args.reconcile:
        contradictions = factbank.find_contradictions(existing)
        print(f"reconciliation queue: {len(contradictions)} contradiction(s)")
        for c in contradictions:
            print(f"  [{c['kind']}] {c.get('company','')} — {c['detail']} · facts {c['fact_ids']}")
        return 0

    profile = json.loads(PROFILE.read_text())
    seeded = _seed_from_profile(profile)
    merged, added, kept = _merge(existing, seeded)

    # validate
    problems = [(f.get("fact_id"), validate) for f in merged
                if (validate := factbank.validate_fact(f))]
    counts = {s: len([f for f in merged if f.get("verification_status") == s])
              for s in factbank.STATUSES}
    print(f"fact bank: {len(merged)} fact(s) — +{added} new, {kept} kept unchanged")
    print(f"  status: {counts}")
    if problems:
        print(f"  ⚠ {len(problems)} fact(s) failed validation: {problems[:3]}")
    contradictions = factbank.find_contradictions(merged)
    if contradictions:
        print(f"  reconciliation queue: {len(contradictions)} contradiction(s) "
              f"(run --reconcile to see them)")

    if args.dry_run:
        print("  (dry-run: nothing written)")
        return 0
    factbank.save(OUT, merged, source="data/profile.json")
    print(f"  wrote {OUT.relative_to(ROOT)} (git-ignored, private)")
    print("  NEXT: open it and set verification_status to 'verified' for facts you can "
          "stand behind; 'do_not_use' to block. Only 'verified' facts generate résumé bullets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
