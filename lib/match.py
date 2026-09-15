"""Stage 2 — match a job to the profile and pick the best resume variant.

Two questions per job:
  1. Is this job a fit worth preparing? -> a fit score (0..100-ish, uncapped).
  2. Which of the profile's prebuilt resume variants fits it best?

Scoring is transparent and keyword-based (no ML, stdlib only), mirroring how the
existing scanner/tailor tools already reason about Priyanka's lanes. The score
combines: keyword hits in the job title (heavily weighted — titles are the
strongest signal), keyword hits in the job description excerpt, and how many of
the winning variant's own skill terms appear in the posting.
"""

import re
import unicodedata

from lib import immigration as _immigration


def _strip_accents(s: str) -> str:
    """Fold diacritics so 'São Paulo'/'Kraków'/'Montréal' match their ASCII terms."""
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))

# Curated vocabulary for the candidate's target lanes. Reused from the scanner's
# tailor vocabulary so scoring stays consistent with the rest of the toolchain.
LANE_TERMS = [
    "product marketing", "go-to-market", "go to market", "gtm", "pmm",
    "positioning", "messaging", "product launch", "launch",
    "competitive intelligence", "competitive analysis", "sales enablement",
    "buyer persona", "segmentation", "value proposition", "thought leadership",
    "demand generation", "demand gen", "growth marketing", "lifecycle",
    "crm", "email marketing", "campaign", "nurture", "pipeline", "funnel",
    "conversion", "retention", "brand", "field marketing", "partner marketing",
    "customer marketing", "marketing analytics", "marketing analyst",
    "business analyst", "growth analyst", "data analyst", "insights analyst",
    "product analyst", "reporting analyst", "revenue operations", "revops",
    "marketing operations", "marketing ops",
    # Analyst lane (targeted 0–3 yrs): sales / ops / revenue analyst variants.
    "sales analyst", "sales operations analyst", "sales operations", "sales ops",
    "operations analyst", "revenue analyst", "revenue operations analyst",
    "gtm analyst", "strategy analyst",
    "analytics", "data-driven", "a/b testing", "experimentation", "kpi", "roi",
    "reporting", "dashboard", "insights", "market research", "attribution",
    "sql", "tableau", "power bi", "looker", "ga4", "excel", "abm",
    "account-based marketing", "salesforce", "hubspot", "eloqua",
]

_TAG_RE = re.compile(r"<[^>]+>")

# Titles carrying any of these are dropped: too senior for a manager-level
# candidate, or a different function entirely (engineering/design). Mirrors the
# seniority/role exclusions the upstream scanner already used. Config can
# override via match.exclude_title_terms.
# Only CLEARLY-EXECUTIVE titles are hard-excluded here (Phase 1). Senior / Sr /
# Staff / Lead / Principal and plain "Director" are NOT on this list any more:
# title seniority is no longer a hard filter — level is judged by REQUIRED YEARS
# and scope (see experience_ok), then ranked. A "Senior Product Marketing
# Manager" or "Marketing Director" asking for ~5 years is on-target; a VP / C-level
# / President / Head-of role is genuinely out of band and stays excluded.
DEFAULT_EXCLUDE_TITLE_TERMS = [
    "vp", "v.p.", "vice president", "svp", "evp", "head of",
    "chief", "cmo", "president", "intern", "internship",
    "co-op", "co op", "apprentice", "fellow", "trainee",
    "engineer", "engineering", "software", "data scientist", "designer",
    "architect",
    # Quota-carrying / sales roles — not what the candidate is targeting.
    # (These are substring-matched, so "business analyst" is unaffected by the
    # "business development" entries.)
    "account executive", "sales representative", "sales rep",
    "sales development", "sdr", "bdr", "business development representative",
    "business development manager", "sales manager", "sales executive",
    "inside sales", "outside sales", "enterprise sales", "channel sales",
    "territory", "quota carrying", "quota-carrying",
    "customer development representative", "customer development rep",
    # Legal / tax / finance / accounting functions — out of scope (also unlikely to
    # sponsor). Uses PHRASES like "financial analyst" (not bare "finance") so a
    # marketing/analyst role in the finance INDUSTRY, e.g. "Marketing Analyst,
    # Financial Services", is kept.
    "legal", "counsel", "paralegal", "attorney", "compliance",
    "tax", "accounting", "accountant", "bookkeep", "payroll", "auditor", "audit",
    "controller", "treasury", "financial analyst", "finance manager", "fp&a",
    # bare "finance" catches "Finance Business Analyst" etc. — and does NOT match
    # "financial" (different substring), so "Marketing Analyst, Financial Services" stays.
    "finance",
]


def excluded_title(title: str | None, terms) -> bool:
    t = (title or "").lower()
    return any(term in t for term in terms)


# Allowlist: a job is shown ONLY if its TITLE matches one of these target families.
# Marketing lane is broad (product marketing + GTM/go-to-market + growth + generic
# "marketing manager" and the marketing sub-functions); analyst lane is the specific
# types the candidate wants (marketing / business / sales analyst). Everything not on
# this list is dropped. Overridable via config match.target_role_terms.
DEFAULT_TARGET_ROLE_TERMS = [
    # marketing lane
    "product marketing", "product marketer", "pmm",
    "go-to-market", "go to market", "gtm",
    "marketing operations", "marketing ops",
    "growth marketing", "demand generation", "demand gen", "lifecycle marketing",
    "content marketing", "brand marketing", "field marketing", "campaign manager",
    "marketing manager", "marketing lead", "marketing specialist",
    "marketing coordinator", "marketing associate", "marketing analyst",
    # analyst lane (the specific types wanted)
    "business analyst", "sales analyst", "sales operations analyst", "sales ops analyst",
    # analytics/ops analyst lane (added 2026-09-15) — the candidate's SQL/Tableau/funnel
    # wheelhouse that the old allowlist dropped. Kept tight to marketing/GTM/revenue/ops
    # flavors (bare "data analyst" deliberately NOT added, to avoid off-lane data roles).
    "marketing data analyst", "marketing analytics", "revenue operations analyst",
    "revenue operations", "revops", "growth analyst", "gtm analyst",
    "operations analyst", "analytics manager",
]


def on_target(title: str | None, terms=None) -> bool:
    """True only when the title matches an allowed target role family."""
    t = (title or "").lower()
    return any(term in t for term in (terms or DEFAULT_TARGET_ROLE_TERMS))


# Location filtering (candidate needs US / US-remote roles for H-1B sponsorship).
# A role is dropped only if it names a non-US location AND has no US marker — so
# multi-location roles like "SF, NYC, Toronto, Remote in the US" are kept.
#
# Full country/city NAMES are matched as substrings (long enough to be safe).
# Two-letter state / country CODES are matched only as whole comma/space tokens —
# matching them as substrings caused false positives (", co" inside "county",
# "uk" inside "milwaukee"). "ca" is intentionally not a US code: it is
# indistinguishable from Canada's country code, so California relies on its city
# names instead.
NONUS_LOCATION_TERMS = [
    "india", "london", "united kingdom", "ireland", "dublin", "emea",
    "apac", "germany", "berlin", "munich", "france", "paris", "spain", "madrid",
    "barcelona", "portugal", "lisbon", "poland", "krakow", "warsaw", "netherlands",
    "amsterdam", "australia", "sydney", "melbourne", "singapore", "tokyo", "japan",
    "korea", "seoul", "toronto", "canada", "vancouver", "ontario", "british columbia",
    "quebec", "montreal", "mexico", "guadalajara", "brazil", "sao paulo", "israel",
    "tel aviv", "dubai", "uae", "latam", "philippines", "manila", "hyderabad",
    "bangalore", "bengaluru", "pune", "delhi", "mumbai", "chennai", "gurgaon", "noida",
]
NONUS_CODES = {"uk", "gb", "ie"}
US_LOCATION_TERMS = [
    "united states", "usa", "u.s.", "u.s.a",
    "massachusetts", "new york", "nyc", "boston", "cambridge", "san francisco",
    "seattle", "chicago", "austin", "denver", "atlanta", "los angeles", "brooklyn",
    "california", "san diego", "san jose", "sacramento", "palo alto",
    "mountain view", "sunnyvale", "oakland", "irvine", "santa clara",
    "remote in the us", "remote - us", "us remote", "remote, us", "remote us",
]
US_STATE_CODES = {
    "us", "usa", "ma", "ny", "wa", "il", "tx", "co", "ga", "dc", "va", "nc",
    "az", "or", "fl", "pa", "mn", "oh", "ut", "md", "nj", "tn",
}


def location_ok(location: str | None, nonus=None, us=None) -> bool:
    if not location:
        return True  # unknown location → keep rather than over-filter
    loc = _strip_accents(location.lower())
    tokens = {t.strip(" .") for t in re.split(r"[,\s/|]+", loc)}
    nonus = nonus if nonus is not None else NONUS_LOCATION_TERMS
    us = us if us is not None else US_LOCATION_TERMS
    has_us = any(t in loc for t in us) or bool(tokens & US_STATE_CODES)
    has_nonus = any(t in loc for t in nonus) or bool(tokens & NONUS_CODES)
    if has_nonus and not has_us:
        return False
    return True


def _clean(text: str | None) -> str:
    if not text:
        return ""
    return _TAG_RE.sub(" ", text).lower()


# Minimum required years of experience, pulled from the JD. Requires an
# "experience" context nearby so we don't match "5 years ago" etc. Returns the
# smallest plausible figure (e.g. "5-7 years" -> 5), or None if not stated.
_YEARS_PATTERNS = [
    r"(\d{1,2})\s*\+?\s*(?:-|to|–|—)\s*\d{1,2}\s*years?\s+(?:of\s+)?(?:[a-z ]{0,24})?experience",
    r"(\d{1,2})\s*\+?\s*years?\s+(?:of\s+)?(?:[a-z ]{0,24})?experience",
    r"(?:minimum|at\s+least|min\.?|at\s+minimum)\s+(?:of\s+)?(\d{1,2})\s*\+?\s*years?",
    r"experience[:\s].{0,20}?(\d{1,2})\s*\+?\s*years?",
]


def extract_years(text: str | None) -> int | None:
    if not text:
        return None
    t = _TAG_RE.sub(" ", text).lower()
    found = []
    for p in _YEARS_PATTERNS:
        for m in re.finditer(p, t):
            n = int(m.group(1))
            if 1 <= n <= 20:
                found.append(n)
    return min(found) if found else None


# Experience band (Phase 1). The candidate has 4+ years of relevant experience,
# so the target is roles asking for roughly 3–6 years (primary), stretching to 7,
# and is comfortably over the bar below that (≤2 yrs = fine, just over-qualified).
# ONLY roles whose ELIGIBILITY bar is substantially higher — 8+ years — are
# filtered out. Two deliberate rules:
#   • Total vs role-relevant: extract_years returns the SMALLEST stated figure, so
#     "8+ years overall, 3+ years in marketing" gates on 3 (the role-relevant bar),
#     not 8. We never exclude on a "total years" number when a smaller relevant one
#     is present.
#   • Basic vs Preferred: years asked for in a "Preferred / nice-to-have" section
#     are ranking signals, NOT eligibility — the gate reads only the eligibility
#     (Basic/Minimum) portion of the JD (see eligibility_text).
# Jobs that state no minimum are kept (unknown → keep), matching the pipeline's
# stance elsewhere. Threshold overridable via config match.experience.exclude_at_years.
EXPERIENCE_EXCLUDE_AT_YEARS = 8

# Headers that begin the "preferred / nice-to-have" part of a JD. Anything from
# such a header onward is a ranking preference, not an eligibility requirement.
_PREFERRED_HEADER_RE = re.compile(
    r"(preferred\s+qualifications?|preferred\s+skills|preferred\s+experience|"
    r"nice[-\s]?to[-\s]?have|bonus\s+(?:points|qualifications?|skills)?|"
    r"desired\s+qualifications?|pluses|it'?s?\s+a\s+plus|"
    r"even\s+better|what\s+would\s+set\s+you\s+apart)", re.I)


def eligibility_text(text: str | None) -> str:
    """The portion of the JD that governs ELIGIBILITY. If the JD has a 'preferred /
    nice-to-have' section, everything from that header onward is ranking-only and
    is dropped, so years asked for there don't gate the role out — the Basic /
    Minimum qualifications are the bar. No such header → the whole text."""
    if not text:
        return ""
    m = _PREFERRED_HEADER_RE.search(text)
    return text[:m.start()] if m else text


def required_years(job: dict) -> int | None:
    """Minimum RELEVANT years the role requires for eligibility, or None if unstated.
    Reads only the eligibility section (preferred years are ranking-only) and takes
    the smallest stated figure (the role-relevant bar, not a larger 'total years')."""
    return extract_years(eligibility_text(job.get("excerpt")))


def experience_exclude_at(cfg_match: dict) -> int:
    exp = (cfg_match or {}).get("experience", {}) if isinstance(cfg_match, dict) else {}
    return int(exp.get("exclude_at_years", EXPERIENCE_EXCLUDE_AT_YEARS))


def experience_ok(job: dict, cfg_match: dict) -> bool:
    """False only when the role's eligibility bar is at/above the exclude threshold
    (default 8 years). Everything from 0 up through the stretch band (7) is kept;
    an unstated minimum is kept. Title seniority is NOT consulted — level is a
    ranking concern, not a gate."""
    yrs = required_years(job)
    if yrs is None:
        return True
    return yrs < experience_exclude_at(cfg_match)


def _count_terms(text: str, terms) -> list[str]:
    return [t for t in terms if t in text]


def match_job(job: dict, profile, extra_terms=None) -> dict:
    """Return a match result: fit score, best variant, and the evidence behind it.

    Fit is normalized to ~0-100 and robust to missing excerpts (many ATS feeds
    omit the description body). A single on-lane job title carries most of the
    signal; the excerpt and variant-skill overlap refine it when present.
      - title keywords:  up to 60 pts (the strongest, most reliable signal)
      - excerpt keywords: up to 25 pts (only when a description body exists)
      - variant overlap:  up to 15 pts (how well the best resume variant fits)
    """
    title = _clean(job.get("title"))
    excerpt = _clean(job.get("excerpt"))
    full = f"{title} {excerpt}"

    lane = LANE_TERMS + list(extra_terms or [])   # config can broaden the lane
    title_hits = _count_terms(title, lane)
    excerpt_hits = _count_terms(excerpt, lane)

    title_pts = min(60, len(title_hits) * 30)
    excerpt_pts = min(25, len(set(excerpt_hits) - set(title_hits)) * 3)

    # Pick the best-fitting resume variant.
    best_variant = None
    best_variant_score = -1
    best_variant_hits: list[str] = []
    for v in profile.variants:
        vt = profile.variant_terms(v)
        hits = _count_terms(full, vt)
        vs = len(hits)
        if vs > best_variant_score:
            best_variant_score = vs
            best_variant = v
            best_variant_hits = hits

    variant_pts = min(15, max(best_variant_score, 0) * 3)
    fit = title_pts + excerpt_pts + variant_pts

    return {
        "fit_score": fit,
        "title_keywords": sorted(set(title_hits)),
        "matched_variant": best_variant.get("label") if best_variant else None,
        "variant_obj": best_variant,
        "variant_matched_terms": sorted(set(best_variant_hits))[:20],
    }


def passes_filters(job: dict, match: dict, cfg_match: dict) -> bool:
    if match["fit_score"] < cfg_match.get("min_fit_score", 30):
        return False
    if (job.get("sponsorship") or "").strip() in cfg_match.get("exclude_sponsorship", []):
        return False
    # Immigration hard stop (Phase 2): the JD explicitly prohibits sponsorship, or
    # requires citizenship / a security clearance — genuinely non-viable for an
    # F-1/H-1B candidate. On by default; disable via match.immigration_hard_stop:false
    # (the full evidence-backed classification is attached to each surfaced job).
    if cfg_match.get("immigration_hard_stop", True) and _immigration.hard_stop(job)[0]:
        return False
    if not on_target(job.get("title"), cfg_match.get("target_role_terms")):
        return False   # allowlist: title must be one of the target role families
    terms = cfg_match.get("exclude_title_terms", DEFAULT_EXCLUDE_TITLE_TERMS)
    if excluded_title(job.get("title"), terms):
        return False
    if not location_ok(job.get("location")):
        return False
    if not experience_ok(job, cfg_match):
        return False
    # NOTE (Phase 1): title seniority is intentionally NOT a hard filter. A "Senior"
    # / "Lead" / "Principal" / "Director" title is kept when its required-years bar
    # is in band (experience_ok above) and it's a target function (on_target); level
    # is then handled by ranking, not by dropping the role on a word in the title.
    return True
