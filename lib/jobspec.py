"""Phase 3 — structured job representation (requirement-to-evidence foundation).

The ranker used to see ~320 characters of each JD, which is far too little to tell
a genuine qualification from keyword overlap. This module preprocesses a job's full
cleaned JD (already captured in `excerpt`) into the structured representation the
brief specifies — sectioning the text into responsibilities / basic qualifications /
preferred qualifications and pulling out required years, locations, salary, and the
sponsorship-related sentences.

Deterministic and stdlib-only (no network, no LLM) so it always works as the
fallback; the LLM ranker is then handed these structured requirements PLUS the full
source text (see scripts/coach_rank.py) rather than a truncated blob. Parsing is
best-effort: JDs vary wildly, so unrecognized structure degrades to empty sections
while the full cleaned JD is always preserved.
"""

import re

from lib import match as _match

# --- section headers ----------------------------------------------------------
# (section, header-phrase). Ordered specific -> general so "preferred qualifications"
# wins over bare "qualifications". A header is recognized only when it sits ~alone on
# a line (optionally bulleted, optionally colon-terminated) — the common ATS format —
# which keeps ordinary prose containing these words from being mistaken for a header.
_HEADER_ALTS = [
    ("preferred", r"preferred\s+qualifications?"),
    ("preferred", r"preferred\s+(?:skills|experience)"),
    ("preferred", r"nice[-\s]?to[-\s]?haves?"),
    ("preferred", r"bonus\s+(?:points|qualifications?|skills)?"),
    ("preferred", r"desired\s+(?:qualifications?|skills)"),
    ("preferred", r"(?:it'?s\s+a\s+)?pluses?"),
    ("preferred", r"even\s+better\s+if"),
    ("preferred", r"what\s+would\s+set\s+you\s+apart"),
    ("basic", r"basic\s+qualifications?"),
    ("basic", r"minimum\s+qualifications?"),
    ("basic", r"(?:required|key)\s+qualifications?"),
    ("basic", r"what\s+you'?ll\s+need"),
    ("basic", r"what\s+we'?re\s+looking\s+for"),
    ("basic", r"who\s+you\s+are"),
    ("basic", r"about\s+you"),
    ("basic", r"skills\s+(?:and|&)\s+experience"),
    ("basic", r"requirements?"),
    ("basic", r"qualifications?"),
    ("responsibilities", r"key\s+responsibilities"),
    ("responsibilities", r"responsibilities"),
    ("responsibilities", r"what\s+you'?ll\s+(?:do|be\s+doing)"),
    ("responsibilities", r"what\s+you\s+will\s+do"),
    ("responsibilities", r"your\s+impact"),
    ("responsibilities", r"the\s+(?:role|opportunity)"),
    ("responsibilities", r"day[-\s]to[-\s]day"),
    ("responsibilities", r"in\s+this\s+role"),
    ("responsibilities", r"about\s+the\s+role"),
    ("responsibilities", r"role\s+overview"),
]

_HEADER_RE = re.compile(
    r"(?im)(?:^|\n)[ \t]*[••\-\*]?[ \t]*("
    + "|".join(f for _, f in _HEADER_ALTS)
    + r")[ \t]*:?[ \t]*(?=\n|$)")

# Split on bullet glyphs, newlines, line-leading "- "/"* ", and Amazon-style inline
# " - " separators ("3+ years X - 5+ years Y - Bachelor's degree"). The lookbehind
# excludes a preceding digit/comma/period so numeric ranges ("$82,700 - $130,100",
# "5 - 7 years") are not torn apart.
_BULLET_SPLIT = re.compile(
    r"[••▪◦]|\n|(?:^|\n)[ \t]*[-\*][ \t]+|(?<=[^\d.,\s])[ ]+[-–—][ ]+(?=\S)")

# Legal / EEO / accommodation boilerplate that trails many JDs — never a real bullet.
_BOILERPLATE = re.compile(
    r"equal\s+opportunity|does\s+not\s+discriminate|reasonable\s+accommodation|"
    r"protected\s+(?:veteran|status|class)|inclusive\s+culture|"
    r"e-?verify|background\s+check|to\s+all\s+qualified\s+applicants|"
    r"without\s+regard\s+to|pursuant\s+to|fair\s+chance", re.I)

_SALARY_RE = re.compile(
    r"\$\s?\d{2,3}(?:,\d{3})(?:\s?(?:-|–|—|to)\s?\$?\s?\d{2,3}(?:,\d{3}))?"
    r"|\$\s?\d{2,3}\s?[kK]\b(?:\s?(?:-|–|—|to)\s?\$?\s?\d{2,3}\s?[kK]\b)?")

# A pay figure written WITHOUT a "$" ("82,700.00 - 130,100.00 USD annually").
_PAY_NUM = re.compile(r"\d{2,3},\d{3}(?:\.\d{2})?")
_PAY_CONTEXT = re.compile(
    r"usd|per\s+year|per\s+hour|annually|annualized|salary\s+range|base\s+pay|"
    r"compensation|/\s?yr|/\s?hour", re.I)
_PAY_RANGE = re.compile(
    r"\$?\s?\d{2,3},\d{3}(?:\.\d{2})?\s*(?:-|–|—|to)\s*\$?\s?\d{2,3},\d{3}(?:\.\d{2})?"
    r"\s*(?:usd|per\s+year|annually)?", re.I)


def _is_comp_line(p: str) -> bool:
    return bool(_PAY_NUM.search(p) and _PAY_CONTEXT.search(p))

_SPONS_KEYWORDS = re.compile(
    r"sponsor|visa|h-?1b|work\s+authorization|authorized\s+to\s+work|"
    r"citizen|green\s+card|clearance|export\s+control", re.I)


def _which_section(header_text: str) -> str:
    h = header_text.strip().lower()
    for section, frag in _HEADER_ALTS:
        if re.fullmatch(frag, h):
            return section
    return "basic"


def _bullets(chunk: str, limit: int = 25) -> list[str]:
    items, seen = [], set()
    for part in _BULLET_SPLIT.split(chunk or ""):
        p = re.sub(r"\s+", " ", part).strip(" .;:•-\t")
        if len(p) < 6 or _BOILERPLATE.search(p):
            continue
        if _is_comp_line(p) and len(p) < 90:   # a stray compensation line, not a qual
            continue
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        items.append(p[:320])
        if len(items) >= limit:
            break
    return items


def _sections(jd: str, max_bullets: int = 25) -> dict:
    """Bucket the JD into responsibilities / basic / preferred by header position."""
    out = {"responsibilities": [], "basic": [], "preferred": []}
    if not jd:
        return out
    marks = [(m.start(), m.end(), _which_section(m.group(1))) for m in _HEADER_RE.finditer(jd)]
    if not marks:
        return out
    for i, (_, end, section) in enumerate(marks):
        nxt = marks[i + 1][0] if i + 1 < len(marks) else len(jd)
        out[section].extend(_bullets(jd[end:nxt]))
    for k in out:                       # bound each section
        out[k] = out[k][:max_bullets]
    return out


def _locations(location: str | None) -> list[str]:
    if not location:
        return []
    parts = re.split(r"[;/|]|,\s*(?![A-Z]{2}\b)", location)  # keep "Boston, MA" together-ish
    locs, seen = [], set()
    for p in parts:
        p = p.strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            locs.append(p)
    return locs[:12]


def _salary(jd: str, salary_raw: str = "") -> str | None:
    if salary_raw:
        return salary_raw
    m = _SALARY_RE.search(jd or "")
    if m:
        return m.group(0).strip()
    m = _PAY_RANGE.search(jd or "")     # "$"-less form: "82,700.00 - 130,100.00 USD"
    if m and _PAY_CONTEXT.search(jd or ""):
        return re.sub(r"\s+", " ", m.group(0)).strip()
    return None


def _sponsorship_text(jd: str) -> list[str]:
    """The sentences that mention sponsorship / visa / citizenship — the source text
    behind the immigration classification, preserved for review."""
    out = []
    for sent in re.split(r"(?<=[.!?])\s+", jd or ""):
        s = re.sub(r"\s+", " ", sent).strip()
        if s and _SPONS_KEYWORDS.search(s):
            out.append(s[:300])
        if len(out) >= 6:
            break
    return out


def structure(job: dict, include_full: bool = True, max_bullets: int = 25) -> dict:
    """Build the structured representation of a job from its cleaned JD (`excerpt`).

    `product_or_service` and `customer` are left for the LLM preprocessor to fill
    (they need semantic reading); everything else is extracted deterministically.
    `include_full=False` omits full_cleaned_jd (the caller already stores `excerpt`);
    `max_bullets` bounds each section (the stored public spec uses a small cap)."""
    jd = job.get("excerpt") or ""
    sec = _sections(jd, max_bullets)
    basic = sec["basic"]
    # Single source of truth with the filter, so a surfaced role never displays a
    # required-years number that would have excluded it.
    req_years = _match.required_years(job)

    spec = {
        "title": job.get("title", ""),
        "company": job.get("company", ""),
        "job_id": job.get("id", ""),
        "product_or_service": "",
        "customer": [],
        "responsibilities": sec["responsibilities"],
        "basic_qualifications": basic,
        "preferred_qualifications": sec["preferred"],
        "required_years": req_years,
        "locations": _locations(job.get("location")),
        "salary": _salary(jd, job.get("salary_raw", "")),
        "sponsorship_text": _sponsorship_text(jd),
        "date_posted": job.get("posted_at", "") or None,
        "full_cleaned_jd": jd if include_full else "",
    }
    return spec
