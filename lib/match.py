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
DEFAULT_EXCLUDE_TITLE_TERMS = [
    "vp", "v.p.", "vice president", "svp", "evp", "head of", "director",
    "chief", "cmo", "president", "principal", "intern", "internship",
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
]


def excluded_title(title: str | None, terms) -> bool:
    t = (title or "").lower()
    return any(term in t for term in terms)


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


# Experience caps by role class. A posting is dropped when its JD states a
# minimum ABOVE the cap for its role class. The candidate's target ranges:
#   analyst (marketing / business / data / insights analyst): 0–2 years
#   product marketing & other marketing roles:                1–4 years
# Jobs that don't state a minimum are kept (we don't guess), matching the rest of
# the pipeline's "unknown → keep" stance. Caps are overridable via config
# match.experience.{analyst_max_years, default_max_years}.
ANALYST_MAX_YEARS = 2
DEFAULT_MAX_YEARS = 4


def role_class(title: str | None) -> str:
    """'analyst' for analyst titles, else 'marketing' (the general lane)."""
    return "analyst" if "analyst" in (title or "").lower() else "marketing"


# Analyst roles are targeted at 0–2 years, so a senior-titled analyst is out of
# range even when the JD omits a year count. Applied to analyst-class titles only
# (senior marketing/PMM roles are left to the year-based cap).
_ANALYST_SENIORITY_RE = re.compile(r"\b(senior|sr|staff|lead|principal|expert)\b")


def too_senior_analyst(title: str | None) -> bool:
    t = (title or "").lower()
    return role_class(t) == "analyst" and bool(_ANALYST_SENIORITY_RE.search(t))


def years_cap(title: str | None, cfg_match: dict) -> int:
    exp = (cfg_match or {}).get("experience", {}) if isinstance(cfg_match, dict) else {}
    if role_class(title) == "analyst":
        return int(exp.get("analyst_max_years", ANALYST_MAX_YEARS))
    return int(exp.get("default_max_years", DEFAULT_MAX_YEARS))


def experience_ok(job: dict, cfg_match: dict) -> bool:
    """False when the JD demands more years than the role class allows."""
    yrs = extract_years(job.get("excerpt"))
    if yrs is None:
        return True
    return yrs <= years_cap(job.get("title"), cfg_match)


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
    terms = cfg_match.get("exclude_title_terms", DEFAULT_EXCLUDE_TITLE_TERMS)
    if excluded_title(job.get("title"), terms):
        return False
    if not location_ok(job.get("location")):
        return False
    if not experience_ok(job, cfg_match):
        return False
    if too_senior_analyst(job.get("title")):
        return False
    return True
