"""Phase 2 — immigration viability as a first-class, evidence-backed signal.

The candidate is on F-1 OPT and will need future H-1B sponsorship, so a role's
immigration posture is a primary constraint, not a footnote. This module reads
the JD text (deterministic, stdlib-only — no network, no LLM) and produces the
structured `immigration` object the dashboard displays and the filter can act on.

Non-negotiable rules baked in here (from the brief):
  • An explicit "no sponsorship now or in the future" is a hard stop (risk=red).
  • A citizenship or security-clearance requirement is a hard stop (risk=red).
  • Historical H-1B sponsorship is EVIDENCE, never proof: company H-1B history is
    recorded in `company_h1b_history` and can never lift risk to green.
  • E-Verify status and future H-1B sponsorship are tracked separately.
  • Unknown sponsorship is not red — it yields risk=yellow plus an explicit action
    to verify with the recruiter before the hiring-manager stage.
  • Manual evidence / recruiter confirmation may override inference (schema carries
    `team_confirmation`, `overridden`, `evidence`, `last_verified` for that workflow).

We do not scrape prohibited sources. Company E-Verify and marketing-specific H-1B
evidence are left "unknown" until a permitted data source is wired up; the schema
and the manual workflow fields exist now so they can be populated later.
"""

import re


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


# --- deterministic JD-text patterns -------------------------------------------
# PROHIBIT is checked first and wins: an explicit "no" is definitive. Negated
# support phrases ("we do NOT offer visa sponsorship") are caught here, before the
# support patterns can misfire.
_PROHIBIT = [
    r"\b(?:not|unable|cannot|can\s?not|won'?t|will\s+not|does\s+not|do\s+not|"
    r"are\s+not\s+able|aren'?t|isn'?t|don'?t|doesn'?t|can'?t|couldn'?t|"
    r"do\s+not\s+offer|not\s+in\s+a\s+position)\b[^.]{0,40}\bsponsor",
    r"\bno\b[^.]{0,20}\b(?:visa\s+)?sponsorship",
    r"\bwithout\b[^.]{0,20}\bsponsorship",
    r"\bsponsorship\b[^.]{0,20}\b(?:is\s+)?not\s+(?:available|offered|provided)",
    r"\bnot\s+eligible\s+for\b[^.]{0,20}\bsponsorship",
    r"\bdo(?:es)?\s+not\s+require\b[^.]{0,30}\bsponsorship\b[^.]{0,30}\b(?:now|future)",
    r"\b(?:authorized|eligible|legally\s+authorized)\s+to\s+work\b[^.]{0,60}\bwithout\b[^.]{0,20}\bsponsor",
    r"\bmust\s+not\s+require\b[^.]{0,20}\bsponsor",
]

# Citizenship / clearance requirements — a hard stop for an F-1/H-1B candidate.
# Guarded against EEO boilerplate ("...regardless of citizenship...").
_CITIZEN = [
    r"\bu\.?s\.?\s+citizen(?:ship)?\b",
    r"\bunited\s+states\s+citizen",
    r"\bmust\s+be\s+a\s+citizen\b",
    r"\bcitizenship\s+(?:is\s+)?required\b",
    r"\bgreen\s+card\s+holders?\s+only\b",
]
_CLEARANCE = [
    r"\bsecurity\s+clearance\b",
    r"\b(?:active|current)\s+clearance\b",
    r"\bts\/sci\b", r"\btop\s+secret\b", r"\bsecret\s+clearance\b",
    r"\b(?:obtain|maintain)\b[^.]{0,30}\bclearance\b",
]
_EEO_GUARD = re.compile(
    r"(equal\s+opportunity|regardless\s+of|without\s+regard\s+to|"
    r"do\s+not\s+discriminate|protected\s+(?:class|status)|diversity)", re.I)

# Hedged / conditional sponsorship -> ambiguous (checked before support).
_AMBIGUOUS = [
    r"\bcase[-\s]?by[-\s]?case\b",
    r"\bsponsorship\b[^.]{0,30}\bmay\b",
    r"\bmay\b[^.]{0,20}\bsponsor",
    r"\bmay\s+be\s+available\b[^.]{0,20}\bsponsor",
    r"\bsponsorship\b[^.]{0,30}\bexceptional\b",
    r"\bsponsorship\b[^.]{0,30}\bconsidered\b",
]

# Explicit support.
_SUPPORT = [
    r"\b(?:will|do|we|happy\s+to|open\s+to|able\s+to)\s+sponsor",
    r"\b(?:visa|h-?1b|immigration)\s+sponsorship\s+(?:is\s+)?(?:available|offered|provided)",
    r"\b(?:offer|provide|offering|providing)\b[^.]{0,20}\b(?:visa\s+)?sponsorship",
    r"\beligible\s+for\b[^.]{0,20}\bsponsorship",
    r"\bsponsorship\s+provided\b",
    r"\bwe\s+sponsor\b",
]


def _sentence_around(text: str, span, window: int = 140) -> str:
    """The sentence containing a regex match, for evidence (source text preserved).
    Clamped to a window around the match so abbreviations ('U.S.') and bullet lists
    with few periods can't run the snippet away from the matched phrase."""
    start, end = span
    left = text.rfind(".", 0, start) + 1
    right = text.find(".", end)
    right = right + 1 if right != -1 else len(text)
    left = max(left, start - window)          # never wander far before the match
    right = min(right, end + window)          # ...or far after it
    snippet = _clean(text[left:right])
    return ("…" + snippet if left > 0 else snippet)[:300]


def _first_match(text: str, patterns):
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return m
    return None


def _citizen_or_clearance(text: str):
    """Return (kind, match) for a citizenship/clearance hard stop, skipping EEO
    boilerplate sentences. kind is 'citizenship' or 'clearance'."""
    for kind, pats in (("clearance", _CLEARANCE), ("citizenship", _CITIZEN)):
        for p in pats:
            for m in re.finditer(p, text, re.I):
                sent = _sentence_around(text, m.span())
                if _EEO_GUARD.search(sent):
                    continue  # "...regardless of citizenship..." is not a requirement
                return kind, m
    return None, None


def classify_job_text(excerpt: str | None) -> dict:
    """Deterministic classification of a JD's sponsorship posture.
    Returns {job_text, hard_stop, hard_stop_reason, evidence:[{text}]}. Pure."""
    text = _clean(excerpt)
    ev = []
    if not text:
        return {"job_text": "not_stated", "hard_stop": False,
                "hard_stop_reason": "", "evidence": ev}

    # Citizenship / clearance requirements are a definitive hard stop, checked first.
    kind, cm = _citizen_or_clearance(text)
    if kind:
        ev.append(_sentence_around(text, cm.span()))
        return {"job_text": "not_stated", "hard_stop": True,
                "hard_stop_reason": kind, "evidence": ev}

    prohibit_m = _first_match(text, _PROHIBIT)
    support_m = _first_match(text, _SUPPORT)

    # BOTH a prohibit and a support phrase present → conditional/nuanced sponsorship
    # ("We do sponsor visas! However, we aren't able to sponsor for some roles."):
    # ambiguous, NOT a hard stop and NOT a clean green — the candidate must verify.
    if prohibit_m and support_m:
        ev.append(_sentence_around(text, prohibit_m.span()))
        return {"job_text": "ambiguous", "hard_stop": False,
                "hard_stop_reason": "", "evidence": ev}

    if prohibit_m:
        ev.append(_sentence_around(text, prohibit_m.span()))
        return {"job_text": "prohibits", "hard_stop": True,
                "hard_stop_reason": "job_text_prohibits", "evidence": ev}

    amb_m = _first_match(text, _AMBIGUOUS)
    if amb_m:
        ev.append(_sentence_around(text, amb_m.span()))
        return {"job_text": "ambiguous", "hard_stop": False,
                "hard_stop_reason": "", "evidence": ev}

    if support_m:
        ev.append(_sentence_around(text, support_m.span()))
        return {"job_text": "supports", "hard_stop": False,
                "hard_stop_reason": "", "evidence": ev}

    return {"job_text": "not_stated", "hard_stop": False,
            "hard_stop_reason": "", "evidence": ev}


def load_no_sponsor(root) -> set:
    """Companies the candidate has CONFIRMED won't sponsor (e.g. learned from an
    application form, which we can't scrape). Git-ignored data/no_sponsor.local.json:
    {"companies": ["1Password", ...]}. Normalized to lowercase for matching."""
    import json as _json
    from pathlib import Path as _Path
    try:
        d = _json.loads((_Path(root) / "data" / "no_sponsor.local.json").read_text())
        return {(c or "").strip().lower() for c in d.get("companies", []) if c}
    except (OSError, _json.JSONDecodeError, TypeError):
        return set()


def _is_known_no_sponsor(job, no_sponsor) -> bool:
    return bool(no_sponsor) and (job.get("company") or "").strip().lower() in no_sponsor


def hard_stop(job: dict, no_sponsor=None) -> tuple[bool, str]:
    """Text-only hard-stop test used by the match filter (no company lookup).
    True when the JD explicitly prohibits sponsorship, requires citizenship or a
    clearance, enrichment marked sponsorship 'No', OR the company is on the
    candidate's confirmed won't-sponsor list."""
    if _is_known_no_sponsor(job, no_sponsor):
        return True, "confirmed_no_sponsorship"
    if (job.get("sponsorship") or "").strip() == "No":
        return True, "enrichment_no"
    r = classify_job_text(job.get("excerpt"))
    return bool(r["hard_stop"]), r["hard_stop_reason"]


def _company_h1b_history(company: dict | None) -> str:
    """Map a company's public DOL-LCA flag to a history bucket. EVIDENCE ONLY —
    this can never lift risk to green (historical sponsorship is not proof the
    current team sponsors)."""
    if not company:
        return "unknown"
    h = company.get("h1b")
    if h is True:
        return "some"
    if h is False:
        return "none"
    return "unknown"


def classify(job: dict, company: dict | None = None, no_sponsor=None) -> dict:
    """Build the full immigration object for a job (schema per the brief).

    risk:
      red    — explicit prohibit, citizenship/clearance requirement, enrichment 'No',
               or the company is on the candidate's confirmed won't-sponsor list.
      green  — explicit support in the JD, or enrichment 'Yes', AND no hard stop.
               (Company H-1B HISTORY never produces green — it is evidence, not proof.)
      yellow — everything else (unknown / ambiguous): proceed but verify first.
    """
    t = classify_job_text(job.get("excerpt"))
    spons = (job.get("sponsorship") or "").strip()
    confirmed_no = _is_known_no_sponsor(job, no_sponsor)
    is_hard = t["hard_stop"] or spons == "No" or confirmed_no

    evidence = []
    for e in t["evidence"]:
        if e:
            evidence.append({"type": "job_text", "text": e,
                             "url": job.get("url", ""), "source": "job_description"})
    if confirmed_no:
        evidence.append({"type": "confirmed", "text": "You recorded that this company won't sponsor.",
                         "source": "candidate (data/no_sponsor.local.json)"})
    if job.get("sponsorship_note"):
        evidence.append({"type": "enrichment", "text": job["sponsorship_note"],
                         "source": "llm_enrichment"})
    hist = _company_h1b_history(company)
    if hist in ("some", "none") and company and company.get("h1b_note"):
        # Recorded as EVIDENCE only; explicitly not proof of current sponsorship.
        evidence.append({"type": "company_h1b_history", "text": company["h1b_note"],
                         "source": "DOL LCA (public, h1bdata.info)",
                         "note": "historical — not proof the current team sponsors"})

    if is_hard:
        risk = "red"
    elif t["job_text"] == "supports" or spons == "Yes":
        risk = "green"
    else:
        risk = "yellow"

    action = {
        "red": ("Hard stop for an F-1/H-1B candidate — do not pursue unless you can "
                "manually confirm this is wrong (override with evidence)."),
        "yellow": "Verify sponsorship with the recruiter before the hiring-manager stage.",
        "green": ("Posting indicates sponsorship — still get it confirmed in writing "
                  "(a posting is evidence, not a guarantee)."),
    }[risk]

    return {
        "job_text": t["job_text"],              # supports | prohibits | not_stated | ambiguous
        "company_everify": "unknown",           # not integrated (no permitted source yet)
        "company_h1b_history": hist,            # strong | some | none | unknown (EVIDENCE only)
        "marketing_h1b_evidence": "unknown",    # function-specific LCA — not integrated yet
        "team_confirmation": "not_asked",       # confirmed | rejected | pending | not_asked (manual)
        "risk": risk,                           # green | yellow | red
        "hard_stop_reason": ("confirmed_no_sponsorship" if confirmed_no else
                             t["hard_stop_reason"] or ("enrichment_no" if spons == "No" else "")),
        "action": action,
        "evidence": evidence,                   # source text/URL preserved for every classification
        "overridden": False,                    # manual override flag (dashboard workflow)
        "last_verified": None,                  # set when a human verifies (staleness tracked in UI)
    }
